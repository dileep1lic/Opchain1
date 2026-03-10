import requests
import time
from datetime import datetime, time, timedelta, date, time as dt_time
from django.shortcuts import render
from requests.exceptions import SSLError, ConnectionError, Timeout
from .models import OptionChain, SupportResistance, SyncControl, TempOptionChain, LiveSRData
from django.utils import timezone
from django.db.models import OuterRef, Subquery, Q, Sum, F
from django.views.decorators.cache import never_cache
from django.http import HttpResponse, JsonResponse
from django.views.decorators.cache import cache_page
from .management.commands.async_live import get_instrument_from_db, update_instrument_store_bulk 
from .symbol import symbols as ALL_SYMBOLS
from django.views.decorators.clickjacking import xframe_options_exempt
import pytz
from django.utils.timezone import localtime
import json
from django.http import JsonResponse
from django.db.models.functions import Abs
from asgiref.sync import async_to_sync



def safe_get(url, headers=None, params=None, retries=3, timeout=10):
    """
    API call karne ke liye ek surakshit function jo retries handle karta hai.
    """
    for attempt in range(retries):
        try:
            response = requests.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout
            )
            # Check karein ki response sahi hai ya nahi (e.g. 401, 404, 500)
            response.raise_for_status() 
            return response.json()

        except (SSLError, ConnectionError, Timeout) as e:
            if attempt == retries - 1:
                print(f"Final attempt failed: {e}")
                return None
            time.sleep(1)
        except requests.exceptions.HTTPError as e:
            print(f"HTTP Error occurred: {e}")
            return None
    return None

# Constants
TIME_WINDOW = timedelta(seconds=1)
PAST_WINDOW = timedelta(seconds=2)

def calculate_percentage(change, max_change):
    return (change / max_change) * 100 if max_change > 0 and change else 0

def apply_ranking_styles(all_data, metric):
    ranked = sorted(all_data, key=lambda x: getattr(x, metric, 0) or 0, reverse=True)
    base_class = metric.replace('_pct', '_class').replace('_percent', '_class')

    for idx, row in enumerate(ranked[:3]):
        val = getattr(row, metric, 0) or 0
        if idx == 0 and val > 0:
            setattr(row, base_class, "bg-green")
        elif idx == 1:
            if val >= 75:
                setattr(row, base_class, "bg-red")
            elif 65 <= val < 75:
                setattr(row, base_class, "bg-yellow")
        elif idx == 2:
            # ✅ तीसरे पर पीला तभी लगेगा जब दूसरे की वैल्यू ≥ 75 हो
            val_2nd = getattr(ranked[1], metric, 0) or 0
            if val_2nd >= 75 and val >= 65:
                setattr(row, base_class, "bg-yellow")

def option_chain_dashboard(request):
    latest_entry = OptionChain.objects.order_by('-Time').first()
    if not latest_entry:
        return render(request, 'mystock/dashboard.html', {'data': [], 'latest_time': None})

    latest_time = latest_entry.Time
    spot_price = latest_entry.Spot_Price
    expiry_date = latest_entry.Expiry_Date

    # Current chain data
    all_data = list(
        OptionChain.objects.filter(
            Time__range=(latest_time - TIME_WINDOW, latest_time + TIME_WINDOW)
        ).order_by('Strike_Price')
    )

    # Past chain data (1 hour ago)
    time_limit = latest_time - timedelta(hours=1)
    closest_past_entry = OptionChain.objects.filter(
        Time__range=(time_limit, latest_time - timedelta(seconds=1))
    ).order_by('Time').first()

    past_vol_map = {}
    if closest_past_entry:
        past_chain = OptionChain.objects.filter(
            Time__range=(closest_past_entry.Time - PAST_WINDOW, closest_past_entry.Time + PAST_WINDOW)
        ).values('Strike_Price', 'CE_Volume', 'PE_Volume')

        past_vol_map = {
            p['Strike_Price']: {
                'ce_vol': p['CE_Volume'] or 0,
                'pe_vol': p['PE_Volume'] or 0
            }
            for p in past_chain
        }

    # Volume change calculation
    max_ce_chg, max_pe_chg = 0, 0
    for row in all_data:
        past_vols = past_vol_map.get(row.Strike_Price)
        if past_vols:
            row.ce_vol_1h_chg = (row.CE_Volume or 0) - past_vols['ce_vol']
            row.pe_vol_1h_chg = (row.PE_Volume or 0) - past_vols['pe_vol']
        else:
            row.ce_vol_1h_chg = None
            row.pe_vol_1h_chg = None

        if row.ce_vol_1h_chg and row.ce_vol_1h_chg > max_ce_chg:
            max_ce_chg = row.ce_vol_1h_chg
        if row.pe_vol_1h_chg and row.pe_vol_1h_chg > max_pe_chg:
            max_pe_chg = row.pe_vol_1h_chg

    # Percentage calculation
    for row in all_data:
        row.ce_vol_1h_chg_pct = calculate_percentage(row.ce_vol_1h_chg, max_ce_chg)
        row.pe_vol_1h_chg_pct = calculate_percentage(row.pe_vol_1h_chg, max_pe_chg)

    # Ranking logic
    all_metrics = [
        'ce_vol_1h_chg_pct', 'pe_vol_1h_chg_pct',
        'CE_OI_percent', 'CE_Volume_percent', 'CE_COI_percent',
        'PE_OI_percent', 'PE_Volume_percent', 'PE_COI_percent'
    ]
    for metric in all_metrics:
        apply_ranking_styles(all_data, metric)

    # Window filtering (±15 strikes around spot)
    if all_data:
        closest_idx = min(range(len(all_data)), key=lambda i: abs(all_data[i].Strike_Price - spot_price))
        display_data = all_data[max(0, closest_idx - 15): min(len(all_data), closest_idx + 16)]

        # Spot divider
        for row in display_data:
            if row.Strike_Price > spot_price:
                row.is_spot_divider = True
                break
    else:
        display_data = []

    return render(request, 'mystock/dashboard.html', {
        'data': display_data,
        'latest_time': latest_time,
        'spot': spot_price,
        'expiry_date': expiry_date
    })

# Constants for time windows
TIME_WINDOW = timedelta(seconds=1)
PAST_WINDOW = timedelta(seconds=2)

def table_update_api(request):
    latest_entry = OptionChain.objects.order_by('-Time').first()

    if not latest_entry:
        return HttpResponse("")

    latest_time = latest_entry.Time
    spot_price = latest_entry.Spot_Price
    expiry_date = latest_entry.Expiry_Date

    # Current chain data
    all_data = list(
        OptionChain.objects.filter(
            Time__range=(latest_time - TIME_WINDOW, latest_time + TIME_WINDOW)
        ).order_by('Strike_Price')
    )

    # Past chain data (1 hour ago)
    time_limit = latest_time - timedelta(hours=1)
    closest_past_entry = OptionChain.objects.filter(
        Time__range=(time_limit, latest_time - timedelta(seconds=1))
    ).order_by('Time').first()

    past_vol_map = {}
    if closest_past_entry:
        past_chain = OptionChain.objects.filter(
            Time__range=(closest_past_entry.Time - PAST_WINDOW, closest_past_entry.Time + PAST_WINDOW)
        ).values('Strike_Price', 'CE_Volume', 'PE_Volume')

        past_vol_map = {
            p['Strike_Price']: {
                'ce_vol': p['CE_Volume'] or 0,
                'pe_vol': p['PE_Volume'] or 0
            }
            for p in past_chain
        }

    # Volume change calculation
    max_ce_chg, max_pe_chg = 0, 0
    for row in all_data:
        past_vols = past_vol_map.get(row.Strike_Price)
        if past_vols:
            row.ce_vol_1h_chg = (row.CE_Volume or 0) - past_vols['ce_vol']
            row.pe_vol_1h_chg = (row.PE_Volume or 0) - past_vols['pe_vol']
        else:
            row.ce_vol_1h_chg = None
            row.pe_vol_1h_chg = None

        if row.ce_vol_1h_chg and row.ce_vol_1h_chg > max_ce_chg:
            max_ce_chg = row.ce_vol_1h_chg
        if row.pe_vol_1h_chg and row.pe_vol_1h_chg > max_pe_chg:
            max_pe_chg = row.pe_vol_1h_chg

    # Percentage calculation
    for row in all_data:
        row.ce_vol_1h_chg_pct = calculate_percentage(row.ce_vol_1h_chg, max_ce_chg)
        row.pe_vol_1h_chg_pct = calculate_percentage(row.pe_vol_1h_chg, max_pe_chg)

    # Ranking logic
    all_metrics = [
        'ce_vol_1h_chg_pct', 'pe_vol_1h_chg_pct',
        'CE_OI_percent', 'CE_Volume_percent', 'CE_COI_percent',
        'PE_OI_percent', 'PE_Volume_percent', 'PE_COI_percent'
    ]
    for metric in all_metrics:
        apply_ranking_styles(all_data, metric)

    # Totals using aggregate
    totals = OptionChain.objects.filter(
        Time__range=(latest_time - TIME_WINDOW, latest_time + TIME_WINDOW)
    ).aggregate(
        total_ce_oi=Sum('CE_OI'),
        total_pe_oi=Sum('PE_OI'),
        total_ce_coi=Sum('CE_COI'),
        total_pe_coi=Sum('PE_COI')
    )

    # Spot divider logic
    if all_data:
        closest_idx = min(range(len(all_data)), key=lambda i: abs(all_data[i].Strike_Price - spot_price))
        display_data = all_data[max(0, closest_idx - 15): min(len(all_data), closest_idx + 16)]
        for row in display_data:
            if row.Strike_Price > spot_price:
                row.is_spot_divider = True
                break
    else:
        display_data = []

    latest_sr = LiveSRData.objects.filter(Symbol=latest_entry.Symbol).order_by('-Time').first()

    context = {
        'data': display_data,
        'latest_time': latest_time,
        'spot': spot_price,
        'expiry_date': expiry_date,
        # 'is_search_dashboard': True,
        'sr_data': latest_sr,
        **totals
    }

    return render(request, 'mystock/table_partial.html', context)

def toggle_sync(request, loop_name):
    if request.method != "POST":
        return JsonResponse({"error": "Invalid method"}, status=405)

    ctrl = SyncControl.objects.get(name=loop_name)
    ctrl.is_active = not ctrl.is_active
    ctrl.save()

    return JsonResponse({
        "loop": loop_name,
        "is_active": ctrl.is_active
    })

@cache_page(10) 
def all_stocks_dashboard(request):
    # 1. सब-क्वेरी: सबसे पहले हर सिंबल की बिल्कुल लेटेस्ट ID निकालें
    # (यहाँ अभी कोई फिल्टर न लगाएं, ताकि हमें पता चले कि अभी ताज़ा स्थिति क्या है)
    newest = SupportResistance.objects.filter(
        Symbol=OuterRef('Symbol')
    ).order_by('-Time')
    
    # 2. मेन क्वेरी: अब यहाँ उन 'ताज़ा' एंट्रीज को हटा दें जो 0 हैं
    latest_data = SupportResistance.objects.filter(
        id=Subquery(newest.values('id')[:1])
    ).exclude(
        # यह लाइन 0, 0.0, और 0.00 सभी को हटा देगी
        Reversl_Ce__lte=0.01  
    ).exclude(
        # यह लाइन खाली (NULL) डेटा को हटा देगी
        Reversl_Ce__isnull=True
    ).exclude(
        # यह लाइन 0, 0.0, और 0.00 सभी को हटा देगी
        Reversl_Pe__lte=0.01  
    ).exclude(
        # यह लाइन खाली (NULL) डेटा को हटा देगी
        Reversl_Pe__isnull=True
    ).order_by('Symbol')

    context = {
        'stocks_data': latest_data
    }
    
    return render(request, 'mystock/all_stocks.html', context)

def stock_search_view(request):
    """
    Search view with Smart Expiry Logic and Auto-Refresh support.
    Reads data from TempOptionChain table.
    """
    # 1. सिंबल प्राप्त करें (डिफ़ॉल्ट NIFTY)
    symbol = request.GET.get('symbol', 'BANKNIFTY').upper()
    
    # URL से एक्सपायरी (अगर है तो)
    url_expiry = request.GET.get('expiry', '')

    # 2. SMART EXPIRY FETCH
    # expiry_list = async_to_sync(get_smart_expiry)(symbol)
    s_key, lot_size, s_expiries = async_to_sync(get_instrument_from_db)(symbol)
    expiry_list = s_expiries if s_expiries else expiry_list
    expiry_list.sort()  # एक्सपायरी को सॉर्ट कर दें ताकि UI में भी सॉर्टेड दिखे


    # 3. EXPIRY SELECTION LOGIC
    if url_expiry and url_expiry in expiry_list:
        selected_expiry = url_expiry
    else:
        selected_expiry = expiry_list[0] if expiry_list else ''

    # 4. AJAX Check
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    # 5. DATA FETCHING FROM DB (TempOptionChain)
    queryset = TempOptionChain.objects.filter(Symbol=symbol).order_by('Strike_Price')
    
    if selected_expiry:
        queryset = queryset.filter(Expiry_Date=selected_expiry)

    latest_data = list(queryset)

    spot_price = 0
    latest_time = None
    lot_size = 1
    display_data = []
    
    # टोटल्स के लिए वेरिएबल्स
    total_ce_oi = 0
    total_pe_oi = 0
    total_ce_coi = 0
    total_pe_coi = 0

    if latest_data:
        first_row = latest_data[0]
        spot_price = first_row.Spot_Price
        latest_time = first_row.Time
        lot_size = first_row.Lot_size

        # 6. RANKING & COLOR LOGIC (Dashboard जैसा)
        metrics = ['CE_OI_percent', 'CE_Volume_percent', 'CE_COI_percent',
                   'PE_OI_percent', 'PE_Volume_percent', 'PE_COI_percent']
        
        for metric in metrics:
            apply_ranking_styles(latest_data, metric)
        # for metric in metrics:
        #     ranked = sorted(latest_data, key=lambda x: getattr(x, metric) or 0, reverse=True)
        #     base_class = metric.replace('_percent', '_class')
            
        #     if len(ranked) > 0: 
        #         setattr(ranked[0], base_class, "bg-green")
            
        #     if len(ranked) > 1:
        #         val2 = getattr(ranked[1], metric) or 0
        #         if val2 >= 75: 
        #             setattr(ranked[1], base_class, "bg-red")
            
        #     if len(ranked) > 2:
        #         val3 = getattr(ranked[2], metric) or 0
        #         if val3 >= 65: 
        #             setattr(ranked[2], base_class, "bg-yellow")

        # 7. TOTAL OI AND COI CALCULATION (पूरे डेटा का टोटल)
        total_ce_oi = sum(row.CE_OI or 0 for row in latest_data)
        total_pe_oi = sum(row.PE_OI or 0 for row in latest_data)
        total_ce_coi = sum(row.CE_COI or 0 for row in latest_data)
        total_pe_coi = sum(row.PE_COI or 0 for row in latest_data)

        # 8. WINDOW FILTERING (±15 Strikes around Spot Price)
        closest_obj = min(latest_data, key=lambda x: abs(x.Strike_Price - spot_price))
        closest_idx = latest_data.index(closest_obj)
        
        start_idx = max(0, closest_idx - 15)
        end_idx = min(len(latest_data), closest_idx + 16)
        
        display_data = latest_data[start_idx : end_idx]

        # 9. SPOT DIVIDER LOGIC
        for row in display_data:
            if row.Strike_Price > spot_price:
                row.is_spot_divider = True 
                break
    
    context = {
        'data': display_data, 
        'symbol': symbol, 
        'expiry': selected_expiry, 
        'spot': spot_price, 
        'latest_time': latest_time,
        'Lot_size': lot_size,
        'all_symbols': ALL_SYMBOLS,
        'expiry_list': expiry_list,
        # कॉन्टेक्स्ट में पूरे डेटा का टोटल पास कर रहे हैं
        'total_ce_oi': total_ce_oi,
        'total_pe_oi': total_pe_oi,
        'total_ce_coi': total_ce_coi,
        'total_pe_coi': total_pe_coi,
        'is_search_dashboard': True,
    }

    if is_ajax:
        return render(request, 'mystock/table_partial.html', context)
    
    return render(request, 'mystock/search_dashboard.html', context)

def trigger_expiry_update(request):
    # symbols_to_update = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX","RELIANCE"]
    
    # for symbol in symbols_to_update:
    update_instrument_store_bulk()
        
    return JsonResponse({"status": "success", "message": "Expiry dates updated successfully!"})

def specific_strike_oi_data(request):
    symbol = request.GET.get('symbol', 'NIFTY')
    strike_price = request.GET.get('strike')

    if not strike_price:
        return JsonResponse({"error": "Strike required"}, status=400)

    ist = pytz.timezone('Asia/Kolkata')
    today = timezone.localdate()

    # 1. 9:15 से 15:30 तक 1 मिनट के फिक्स टाइम स्लॉट्स बनाना
    master_times = []
    current = datetime.combine(today, dt_time(9, 15))
    end_time = datetime.combine(today, dt_time(15, 30))
    
    while current <= end_time:
        master_times.append(current.strftime("%H:%M"))
        current += timedelta(minutes=1) # 1 मिनट का अंतराल

    # 2. डेटाबेस से डेटा निकालना
    db_data = OptionChain.objects.filter(
        Symbol=symbol,
        Strike_Price=strike_price,
        Time__date=today
    ).order_by('Time')

    if not db_data.exists():
        return JsonResponse({"error": "No data found"}, status=404)

    # डेटा में उपलब्ध सबसे आखिरी समय (ताकि भविष्य को खाली रखा जा सके)
    latest_db_time = timezone.localtime(db_data.last().Time, ist).strftime("%H:%M")

    # 3. डेटा को डिक्शनरी में मैप करना
    data_map = {}
    for entry in db_data:
        t_str = timezone.localtime(entry.Time, ist).strftime("%H:%M")
        data_map[t_str] = {
            "ce_oi": entry.CE_COI,
            "pe_oi": entry.PE_OI,
            "ce_pct": entry.CE_OI_percent, # यहाँ मॉडल के हिसाब से फील्ड नाम चेक कर लें
            "pe_pct": entry.PE_OI_percent,
        }

    # 4. Forward Fill Logic (पिछली वैल्यू भरना)
    ce_oi_list, pe_oi_list, ce_pct_list, pe_pct_list = [], [], [], []
    
    last_val = None # पिछली उपलब्ध वैल्यू स्टोर करने के लिए
    found_first_data = False

    for t in master_times:
        if t in data_map:
            # नया डेटा मिला, इसे सेव करें और लिस्ट में डालें
            last_val = data_map[t]
            found_first_data = True
            ce_oi_list.append(last_val["ce_oi"])
            pe_oi_list.append(last_val["pe_oi"])
            ce_pct_list.append(last_val["ce_pct"])
            pe_pct_list.append(last_val["pe_pct"])
        
        elif found_first_data and t <= latest_db_time:
            # बीच में डेटा गायब है, तो पिछली वैल्यू (Last Known) भरें
            ce_oi_list.append(last_val["ce_oi"])
            pe_oi_list.append(last_val["pe_oi"])
            ce_pct_list.append(last_val["ce_pct"])
            pe_pct_list.append(last_val["pe_pct"])
        
        else:
            # 9:15 से पहले या भविष्य के समय के लिए None भेजें
            ce_oi_list.append(None)
            pe_oi_list.append(None)
            ce_pct_list.append(None)
            pe_pct_list.append(None)

    return JsonResponse({
        "times": master_times,
        "ce_oi": ce_oi_list,
        "pe_oi": pe_oi_list,
        "ce_pct": ce_pct_list,
        "pe_pct": pe_pct_list,
    })
# 2. Page View (जो खाली HTML पेज खोलेगा)
@xframe_options_exempt
def render_chart_page(request):
    return render(request, 'mystock/oi_chart_js.html', {
        'symbol': request.GET.get('symbol'),
        'strike': request.GET.get('strike')
    })

# 1. API View (जो सिर्फ डेटा देगा)
def specific_strike_coi_data(request):
    symbol = request.GET.get('symbol', 'NIFTY')
    strike_price = request.GET.get('strike')

    if not strike_price:
        return JsonResponse({"error": "Strike required"}, status=400)

    ist = pytz.timezone('Asia/Kolkata')
    today = timezone.localdate()

    # 1. 9:15 से 15:30 तक 1 मिनट के फिक्स टाइम स्लॉट्स बनाना
    master_times = []
    current = datetime.combine(today, dt_time(9, 15))
    end_time = datetime.combine(today, dt_time(15, 30))
    
    while current <= end_time:
        master_times.append(current.strftime("%H:%M"))
        current += timedelta(minutes=1) # 1 मिनट का अंतराल

    # 2. डेटाबेस से डेटा निकालना
    db_data = OptionChain.objects.filter(
        Symbol=symbol,
        Strike_Price=strike_price,
        Time__date=today
    ).order_by('Time')

    if not db_data.exists():
        return JsonResponse({"error": "No data found"}, status=404)

    # डेटा में उपलब्ध सबसे आखिरी समय (ताकि भविष्य को खाली रखा जा सके)
    latest_db_time = timezone.localtime(db_data.last().Time, ist).strftime("%H:%M")

    # 3. डेटा को डिक्शनरी में मैप करना
    data_map = {}
    for entry in db_data:
        t_str = timezone.localtime(entry.Time, ist).strftime("%H:%M")
        data_map[t_str] = {
            "ce_coi": entry.CE_COI,
            "pe_coi": entry.PE_COI,
            "ce_pct": entry.CE_OI_percent, # यहाँ मॉडल के हिसाब से फील्ड नाम चेक कर लें
            "pe_pct": entry.PE_OI_percent,
        }

    # 4. Forward Fill Logic (पिछली वैल्यू भरना)
    ce_coi_list, pe_coi_list, ce_pct_list, pe_pct_list = [], [], [], []
    
    last_val = None # पिछली उपलब्ध वैल्यू स्टोर करने के लिए
    found_first_data = False

    for t in master_times:
        if t in data_map:
            # नया डेटा मिला, इसे सेव करें और लिस्ट में डालें
            last_val = data_map[t]
            found_first_data = True
            ce_coi_list.append(last_val["ce_coi"])
            pe_coi_list.append(last_val["pe_coi"])
            ce_pct_list.append(last_val["ce_pct"])
            pe_pct_list.append(last_val["pe_pct"])
        
        elif found_first_data and t <= latest_db_time:
            # बीच में डेटा गायब है, तो पिछली वैल्यू (Last Known) भरें
            ce_coi_list.append(last_val["ce_coi"])
            pe_coi_list.append(last_val["pe_coi"])
            ce_pct_list.append(last_val["ce_pct"])
            pe_pct_list.append(last_val["pe_pct"])
        
        else:
            # 9:15 से पहले या भविष्य के समय के लिए None भेजें
            ce_coi_list.append(None)
            pe_coi_list.append(None)
            ce_pct_list.append(None)
            pe_pct_list.append(None)

    return JsonResponse({
        "times": master_times,
        "ce_coi": ce_coi_list,
        "pe_coi": pe_coi_list,
        "ce_pct": ce_pct_list,
        "pe_pct": pe_pct_list,
    })

# 2. अपने चार्ट पेज वाले फंक्शन के ऊपर यह लाइन लिखें
@xframe_options_exempt
def render_chart_page_coi(request):
    return render(request, 'mystock/coi_chart_js.html', {
        'symbol': request.GET.get('symbol'),
        'strike': request.GET.get('strike')
    })

def calculate_final_sr(oi_strike, oi_status, vol_strike, vol_status, option_type, prev_strike=None, prev_status=None):
    """
    यह फंक्शन आपके नियमों के अनुसार फाइनल Support/Resistance और उसका Status निकालता है।
    अब यह Shifted WTT/WTB और STRONG के बाद वाले लॉजिक को भी हैंडल करेगा।
    """
    
    if not oi_strike or not vol_strike:
        return None, None

    # ==========================================
    # नियम 1: अगर OI और Volume दोनों एक ही स्ट्राइक पर हैं
    # ==========================================
    if oi_strike == vol_strike:
        final_strike = oi_strike
        
        if option_type == "CE":
            # --- RESISTANCE (CE) ---
            if oi_status == "WTB" or vol_status == "WTB":
                final_status = "BOTH WTB"
            elif oi_status == "WTT" and vol_status == "WTT":
                final_status = "BOTH WTT"
            else:
                final_status = "BOTH STRONG"
                
        elif option_type == "PE":
            # --- SUPPORT (PE) ---
            if oi_status == "WTT" or vol_status == "WTT":
                final_status = "BOTH WTT"
            elif oi_status == "WTB" and vol_status == "WTB":
                final_status = "BOTH WTB"
            else:
                final_status = "BOTH STRONG"

    # ==========================================
    # नियम 2: अगर OI और Volume अलग-अलग स्ट्राइक पर हैं
    # ==========================================
    else:
        if option_type == "CE":
            if oi_strike < vol_strike:
                final_strike = oi_strike
                final_status = "OI " + oi_status
            else:
                final_strike = vol_strike
                final_status = "Vol " + vol_status
                
        elif option_type == "PE":
            if oi_strike > vol_strike:
                final_strike = oi_strike
                final_status = "OI " + oi_status
            else:
                final_strike = vol_strike
                final_status = "Vol " + vol_status

    # ==========================================
    # नियम 3: SHIFTING (शिफ्टेड WTT/WTB लॉजिक)
    # ==========================================
    if prev_strike is not None and prev_status is not None:
        
        # कंडीशन A: अगर स्ट्राइक बदल गई है (Shift हुआ है)
        if final_strike != prev_strike:
            if "WTT" in final_status:
                final_status = final_status.replace("WTT", "Shifted WTT")
            elif "WTB" in final_status:
                final_status = final_status.replace("WTB", "Shifted WTB")
            elif "STRONG" in final_status:
                final_status = final_status.replace("STRONG", "Shifted STRONG")
                
        # कंडीशन B: स्ट्राइक सेम है (अभी उसी स्ट्राइक पर है)
        else:
            # अगर यह पहले Shifted था और अभी तक STRONG नहीं हुआ है, तो इसे Shifted ही रहने दें।
            # (एक बार STRONG होने के बाद "Shifted" का टैग अपने आप हट जाएगा और नॉर्मल WTT/WTB बन जाएगा)
            if "Shifted" in prev_status and "STRONG" not in final_status:
                if "WTT" in final_status and "Shifted" not in final_status:
                    final_status = final_status.replace("WTT", "Shifted WTT")
                elif "WTB" in final_status and "Shifted" not in final_status:
                    final_status = final_status.replace("WTB", "Shifted WTB")
        
        
    return final_strike, final_status

def test_sr_logic_view(request):
    today = timezone.localdate()

    # 1. डेटा को 'Time' (पुराने से नया) के क्रम में निकालें 
    # ताकि हम शिफ्टिंग के लॉजिक को सुबह 9:15 से लेकर अभी तक सही से टेस्ट कर सकें
    live_data = LiveSRData.objects.filter(
        Symbol="NIFTY", 
        Time__date=today
    ).order_by('Time')
    
    test_data = []
    
    # 2. शुरुआत में कोई पिछला रिकॉर्ड नहीं है (None)
    prev_ce_strike, prev_ce_status = None, None
    prev_pe_strike, prev_pe_status = None, None
    
    for data in live_data:
        # CE (Resistance) के लिए फाइनल कैलकुलेशन
        ce_final_strike, ce_final_status = calculate_final_sr(
            oi_strike=data.ce_high_oi_strike,
            oi_status=data.ce_oi_status,
            vol_strike=data.ce_high_vol_strike,
            vol_status=data.ce_vol_status,
            option_type="CE",
            prev_strike=prev_ce_strike,   # <-- सही सिंटैक्स
            prev_status=prev_ce_status    # <-- सही सिंटैक्स
        )
        
        # PE (Support) के लिए फाइनल कैलकुलेशन
        pe_final_strike, pe_final_status = calculate_final_sr(
            oi_strike=data.pe_high_oi_strike,
            oi_status=data.pe_oi_status,
            vol_strike=data.pe_high_vol_strike,
            vol_status=data.pe_vol_status,
            option_type="PE",
            prev_strike=prev_pe_strike,   # <-- सही सिंटैक्स
            prev_status=prev_pe_status    # <-- सही सिंटैक्स
        )
        
        # 🕒 टाइम को लोकल (IST) में बदलें
        local_time = timezone.localtime(data.Time)

        # डेटा को लिस्ट में डालें
        test_data.append({
            'time': local_time.strftime('%H:%M:%S'),
            'spot': data.Spot_Price,
            
            'ce_oi_strike': data.ce_high_oi_strike,
            'ce_oi_status': data.ce_oi_status,
            'ce_vol_strike': data.ce_high_vol_strike,
            'ce_vol_status': data.ce_vol_status,
            'ce_final_strike': ce_final_strike,
            'ce_final_status': ce_final_status,
            
            'pe_oi_strike': data.pe_high_oi_strike,
            'pe_oi_status': data.pe_oi_status,
            'pe_vol_strike': data.pe_high_vol_strike,
            'pe_vol_status': data.pe_vol_status,
            'pe_final_strike': pe_final_strike,
            'pe_final_status': pe_final_status,
        })
        
        # 🔄 3. असली मैजिक यहाँ है: 
        # अगले लूप के लिए मौजूदा डेटा को "पिछला डेटा" मान लें
        prev_ce_strike, prev_ce_status = ce_final_strike, ce_final_status
        prev_pe_strike, prev_pe_status = pe_final_strike, pe_final_status
        
    # 4. चूँकि हमने पुराने से नया प्रोसेस किया है, 
    # HTML में लेटेस्ट डेटा सबसे ऊपर दिखाने के लिए लिस्ट को उल्टा (Reverse) कर दें
    test_data.reverse()
    
    # अगर आप सिर्फ लेटेस्ट 10 या 20 देखना चाहते हैं:
    # test_data = test_data[:20] 
        
    return render(request, 'mystock/sr_testing.html', {'test_data': test_data})

def get_dynamic_strikes_and_atm(base_qs, spot_price, symbol, start_time, end_time):
    """ स्पॉट प्राइस के आधार पर 15 CE, 15 PE और ATM स्ट्राइक निकालता है (optimized) """

    # DB से nearest strike निकालना (Python loop से बचा)
    atm_record = OptionChain.objects.filter(
        Time__range=(start_time, end_time),
        Symbol=symbol
    ).annotate(
        diff=Abs(F('Strike_Price') - spot_price)
    ).order_by('diff').values('Strike_Price').first()

    if not atm_record:
        return [], [], None

    atm_strike = atm_record['Strike_Price']

    # सिर्फ ATM के आसपास के strikes खींचना (range filter)
    available_strikes = list(
        OptionChain.objects.filter(
            Time__range=(start_time, end_time),
            Symbol=symbol,
            Strike_Price__range=(atm_strike - 500, atm_strike + 500)
        ).values_list('Strike_Price', flat=True).distinct().order_by('Strike_Price')
    )

    if not available_strikes:
        return [], [], None

    atm_idx = available_strikes.index(atm_strike)

    # PE: ATM के नीचे के 15 स्ट्राइक (ATM को भी शामिल किया है)
    start_pe = max(0, atm_idx - 3)
    pe_selected = available_strikes[start_pe:atm_idx+1][::-1]  # reverse

    # CE: ATM के ऊपर के 15 स्ट्राइक (ATM को भी शामिल किया है)
    ce_selected = available_strikes[atm_idx:atm_idx + 4]

    return ce_selected, pe_selected, atm_strike


india_tz = pytz.timezone("Asia/Kolkata")


def generate_timestamps(today):
    start_dt = timezone.make_aware(datetime.combine(today, time(9, 15)))
    end_dt = timezone.make_aware(datetime.combine(today, time(15, 30)))

    timestamps = []
    current = start_dt
    while current <= end_dt:
        timestamps.append(current.astimezone(india_tz).strftime('%H:%M'))
        current += timedelta(minutes=1)
    return timestamps


def option_chart_view(request):
    today = timezone.localdate()
    timestamps = generate_timestamps(today)

    symbol = "NIFTY"

    base_qs = OptionChain.objects.filter(
        Time__range=(timezone.make_aware(datetime.combine(today, time(9, 15))),
                     timezone.make_aware(datetime.combine(today, time(15, 30)))),
        Symbol=symbol
    ).only(
        'Time', 'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe'
    )

    latest_row = base_qs.order_by('-Time').values('Spot_Price').first()
    latest_spot = latest_row['Spot_Price'] if latest_row else None

    available_strikes = sorted(set(base_qs.values_list('Strike_Price', flat=True)))

    ce_selected = request.GET.getlist('ce_strikes')
    pe_selected = request.GET.getlist('pe_strikes')

    ce_selected = list(map(float, ce_selected)) if ce_selected else []
    pe_selected = list(map(float, pe_selected)) if pe_selected else []

    if not ce_selected and not pe_selected and latest_spot:
        ce_selected = [s for s in available_strikes if s >= latest_spot][:10]
        pe_selected = [s for s in reversed(available_strikes) if s <= latest_spot][:10]

    selected_strikes = list(set(ce_selected + pe_selected))

    qs = base_qs.filter(Strike_Price__in=selected_strikes).values(
        'Time', 'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe'
    ).order_by('Time')

    # Map data by time
    data_map = {}
    for row in qs:
        t = row['Time'].astimezone(india_tz).strftime('%H:%M')
        if t not in data_map:
            data_map[t] = {'spot': row['Spot_Price']}
        data_map[t][str(row['Strike_Price'])] = {
            'CE': row['Reversl_Ce'],
            'PE': row['Reversl_Pe']
        }

    spot_prices = []
    ce_data = {str(s): [] for s in ce_selected}
    pe_data = {str(s): [] for s in pe_selected}

    for t in timestamps:
        spot_prices.append(data_map.get(t, {}).get('spot'))
        for s in ce_selected:
            ce_data[str(s)].append(data_map.get(t, {}).get(str(s), {}).get('CE'))
        for s in pe_selected:
            pe_data[str(s)].append(data_map.get(t, {}).get(str(s), {}).get('PE'))

    chart_data = {
        "timestamps": timestamps,
        "spot_prices": spot_prices,
        "ce_reversals": ce_data,
        "pe_reversals": pe_data,
    }

    return render(request, "mystock/chart_template.html", {
        "chart_data": json.dumps(chart_data),
        "available_strikes": available_strikes,
        "ce_selected": ce_selected,
        "pe_selected": pe_selected,
    })


def option_chart_api(request):
    today = timezone.localdate()
    timestamps = generate_timestamps(today)

    symbol = request.GET.get("symbol", "NIFTY")

    ce_selected = request.GET.getlist('ce_strikes[]')
    pe_selected = request.GET.getlist('pe_strikes[]')

    ce_selected = list(map(float, ce_selected)) if ce_selected else []
    pe_selected = list(map(float, pe_selected)) if pe_selected else []

    selected_strikes = list(set(ce_selected + pe_selected))

    qs = OptionChain.objects.filter(
        Time__range=(timezone.make_aware(datetime.combine(today, time(9, 15))),
                     timezone.make_aware(datetime.combine(today, time(15, 30)))),
        Symbol=symbol,
        Strike_Price__in=selected_strikes
    ).values(
        'Time', 'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe'
    ).order_by('Time')

    data_map = {}
    for row in qs:
        t = row['Time'].astimezone(india_tz).strftime('%H:%M')
        if t not in data_map:
            data_map[t] = {'spot': row['Spot_Price']}
        data_map[t][str(row['Strike_Price'])] = {
            'CE': row['Reversl_Ce'],
            'PE': row['Reversl_Pe']
        }

    spot_prices = []
    ce_data = {str(s): [] for s in ce_selected}
    pe_data = {str(s): [] for s in pe_selected}

    for t in timestamps:
        spot_prices.append(data_map.get(t, {}).get('spot'))
        for s in ce_selected:
            ce_data[str(s)].append(data_map.get(t, {}).get(str(s), {}).get('CE'))
        for s in pe_selected:
            pe_data[str(s)].append(data_map.get(t, {}).get(str(s), {}).get('PE'))

    return JsonResponse({
        "timestamps": timestamps,
        "spot_prices": spot_prices,
        "ce_reversals": ce_data,
        "pe_reversals": pe_data
    })

# ==========================================
# RESISTANCE (CE) LOGIC
# ==========================================
def get_resistance_base_target(row):
    if not row.ce_high_vol_strike or not row.ce_high_oi_strike:
        return None, None, None, "N/A", "N/A"

    vol_strike = row.ce_high_vol_strike
    oi_strike = row.ce_high_oi_strike
    vol_status = str(row.ce_vol_status).lower() if row.ce_vol_status else ""
    oi_status = str(row.ce_oi_status).lower() if row.ce_oi_status else ""
    vol_2nd_strike = row.ce_2nd_high_vol_strike
    oi_2nd_strike = row.ce_2nd_high_oi_strike

    # Condition 1: Volume और OI एक ही स्ट्राइक पर हैं (Both)
    if vol_strike == oi_strike:
        base_strike = vol_strike
        base_type = "Both"
        if "wtt" in vol_status and "wtt" in oi_status:
            return base_strike, oi_2nd_strike, "wtt", f"WTT {oi_2nd_strike}", base_type
        elif "wtb" in vol_status or "wtb" in oi_status:
            target = vol_2nd_strike if "wtb" in vol_status else oi_2nd_strike
            return base_strike, target, "wtb", f"WTB {target}", base_type
        else:
            return base_strike, None, "strong", f"Strong {base_strike}", base_type

    # Condition 2: Volume और OI अलग-अलग स्ट्राइक पर हैं (छोटी स्ट्राइक लेंगे)
    else:
        if vol_strike < oi_strike:
            base_strike = vol_strike
            status_text = vol_status
            target = vol_2nd_strike
            base_type = "Vol"
        else:
            base_strike = oi_strike
            status_text = oi_status
            target = oi_2nd_strike
            base_type = "OI"

        if "wtt" in status_text:
            return base_strike, target, "wtt", f"WTT {target}", base_type
        elif "wtb" in status_text:
            return base_strike, target, "wtb", f"WTB {target}", base_type
        else:
            return base_strike, None, "strong", f"Strong {base_strike}", base_type


# ==========================================
# SUPPORT (PE) LOGIC
# ==========================================
def get_support_base_target(row):
    if not row.pe_high_vol_strike or not row.pe_high_oi_strike:
        return None, None, None, "N/A", "N/A"

    vol_strike = row.pe_high_vol_strike
    oi_strike = row.pe_high_oi_strike
    vol_status = str(row.pe_vol_status).lower() if row.pe_vol_status else ""
    oi_status = str(row.pe_oi_status).lower() if row.pe_oi_status else ""
    vol_2nd_strike = row.pe_2nd_high_vol_strike
    oi_2nd_strike = row.pe_2nd_high_oi_strike

    # Condition 1: Volume और OI एक ही स्ट्राइक पर हैं (Both)
    if vol_strike == oi_strike:
        base_strike = vol_strike
        base_type = "Both"
        # Support में WTT हावी (dominant) होता है
        if "wtb" in vol_status and "wtb" in oi_status:
            return base_strike, oi_2nd_strike, "wtb", f"WTB {oi_2nd_strike}", base_type
        elif "wtt" in vol_status or "wtt" in oi_status:
            target = vol_2nd_strike if "wtt" in vol_status else oi_2nd_strike
            return base_strike, target, "wtt", f"WTT {target}", base_type
        else:
            return base_strike, None, "strong", f"Strong {base_strike}", base_type

    # Condition 2: Volume और OI अलग-अलग स्ट्राइक पर हैं (बड़ी स्ट्राइक लेंगे)
    else:
        if vol_strike > oi_strike:
            base_strike = vol_strike
            status_text = vol_status
            target = vol_2nd_strike
            base_type = "Vol"
        else:
            base_strike = oi_strike
            status_text = oi_status
            target = oi_2nd_strike
            base_type = "OI"

        if "wtt" in status_text:
            return base_strike, target, "wtt", f"WTT {target}", base_type
        elif "wtb" in status_text:
            return base_strike, target, "wtb", f"WTB {target}", base_type
        else:
            return base_strike, None, "strong", f"Strong {base_strike}", base_type


# ==========================================
# MAIN VIEW
# ==========================================
def live_data_view(request):
    today = date.today()
    data_records = LiveSRData.objects.filter(Time__date=today).order_by('Time')

    context_data = []
    
    # Resistance Tracking Variables
    ce_prev_base = None
    ce_prev_target = None
    ce_is_shifted = False
    ce_is_shifted_strong = False

    # Support Tracking Variables
    pe_prev_base = None
    pe_prev_target = None
    pe_is_shifted = False
    pe_is_shifted_strong = False
    
    for row in data_records:
        # ---- 1. Calculate Resistance (CE) ----
        ce_base, ce_target, ce_status, ce_basic_text, ce_base_type = get_resistance_base_target(row)
        res_text = "N/A"

        if ce_base is not None:
            prefix = f"Resistance ({ce_base_type})"
            if ce_prev_base is None:
                ce_prev_base, ce_prev_target = ce_base, ce_target
                res_text = f"{prefix} {ce_basic_text}"
            else:
                if ce_base != ce_prev_base:
                    ce_is_shifted, ce_is_shifted_strong = True, False
                    ce_prev_base, ce_prev_target = ce_base, ce_target
                
                if ce_is_shifted:
                    if ce_status == "strong":
                        ce_is_shifted_strong, ce_is_shifted = True, False
                        res_text = f"{prefix} Shifted Strong {ce_base}"
                    else:
                        if ce_target != ce_prev_target:
                            ce_is_shifted = False
                            res_text = f"{prefix} {ce_basic_text}"
                        else:
                            res_text = f"{prefix} Shifted {ce_status.upper()} {ce_target}"
                elif ce_is_shifted_strong:
                    if ce_status == "strong":
                        res_text = f"{prefix} Shifted Strong {ce_base}"
                    else:
                        ce_is_shifted_strong = False
                        res_text = f"{prefix} {ce_basic_text}"
                else:
                    res_text = f"{prefix} {ce_basic_text}"
                ce_prev_target = ce_target

        # ---- 2. Calculate Support (PE) ----
        pe_base, pe_target, pe_status, pe_basic_text, pe_base_type = get_support_base_target(row)
        sup_text = "N/A"

        if pe_base is not None:
            prefix = f"Support ({pe_base_type})"
            if pe_prev_base is None:
                pe_prev_base, pe_prev_target = pe_base, pe_target
                sup_text = f"{prefix} {pe_basic_text}"
            else:
                if pe_base != pe_prev_base:
                    pe_is_shifted, pe_is_shifted_strong = True, False
                    pe_prev_base, pe_prev_target = pe_base, pe_target
                
                if pe_is_shifted:
                    if pe_status == "strong":
                        pe_is_shifted_strong, pe_is_shifted = True, False
                        sup_text = f"{prefix} Shifted Strong {pe_base}"
                    else:
                        if pe_target != pe_prev_target:
                            pe_is_shifted = False
                            sup_text = f"{prefix} {pe_basic_text}"
                        else:
                            sup_text = f"{prefix} Shifted {pe_status.upper()} {pe_target}"
                elif pe_is_shifted_strong:
                    if pe_status == "strong":
                        sup_text = f"{prefix} Shifted Strong {pe_base}"
                    else:
                        pe_is_shifted_strong = False
                        sup_text = f"{prefix} {pe_basic_text}"
                else:
                    sup_text = f"{prefix} {pe_basic_text}"
                pe_prev_target = pe_target

        # ---- 3. Append to Context ----
        context_data.insert(0, {
            "time": localtime(row.Time).strftime("%H:%M:%S"),
            'symbol': row.Symbol,
            'spot_price': row.Spot_Price,
            'ce_high_vol': row.ce_high_vol_strike,
            'ce_vol': row.ce_vol_status,
            'ce_high_oi': row.ce_high_oi_strike,
            'ce_oi': row.ce_oi_status,
            'resistance_status': res_text,
            'pe_high_vol': row.pe_high_vol_strike,
            'pe_vol': row.pe_vol_status,
            'pe_high_oi': row.pe_high_oi_strike,
            'pe_oi': row.pe_oi_status,
            'support_status': sup_text
        })

    return render(request, 'mystock/live_data.html', {'data': context_data})
       



