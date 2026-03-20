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

import pandas as pd
from django.core.cache import cache
def reversal_chart_view2(request):
    if request.method == 'POST':
        timeframe_val = request.POST.get('timeframe', '5')
        resample_rule = f"{timeframe_val}min" 

        ce_strikes = [request.POST.get(f'ce_{i}') for i in range(1, 4) if request.POST.get(f'ce_{i}')]
        pe_strikes = [request.POST.get(f'pe_{i}') for i in range(1, 4) if request.POST.get(f'pe_{i}')]
        
        # आज की बजाय कल की तारीख (Yesterday) निकालें
        target_date = datetime.now().date() - timedelta(days=1)
        
        # डेटाबेस से कल का डेटा निकालें
        spot_data = OptionChain.objects.filter(
            Time__date=target_date,  # यहाँ target_date का इस्तेमाल किया है
            Time__hour__gte=9,
            Time__hour__lte=15
        ).values('Time', 'Spot_Price')

        df = pd.DataFrame(list(spot_data))
        candlestick_data = []
        
        if not df.empty:
            df['Time'] = pd.to_datetime(df['Time'])
            
            # --- UTC से IST (Indian Standard Time) में कन्वर्ट करने का कोड ---
            if df['Time'].dt.tz is None:
                # अगर टाइमज़ोन सेट नहीं है, तो पहले UTC सेट करें, फिर IST में बदलें
                df['Time'] = df['Time'].dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
            else:
                # अगर पहले से टाइमज़ोन है, तो सीधे IST में बदलें
                df['Time'] = df['Time'].dt.tz_convert('Asia/Kolkata')
            # -------------------------------------------------------------
            
            df.set_index('Time', inplace=True)
            
            ohlc_df = df['Spot_Price'].resample(resample_rule).ohlc().dropna()
            
            for time, row in ohlc_df.iterrows():
                candlestick_data.append({
                    'time': time.strftime('%H:%M'), # अब यह 09:15, 09:20 ऐसे भारतीय समय दिखाएगा
                    'open': row['open'],
                    'high': row['high'],
                    'low': row['low'],
                    'close': row['close'],
                })

        # रिवर्सल प्राइस निकालना (कल के डेटा के आधार पर)
        reversal_lines = []
        
        for strike in ce_strikes:
            # यहाँ भी Time__date=target_date कर दिया है
            latest_ce = OptionChain.objects.filter(Strike_Price=strike, Time__date=target_date).order_by('-Time').first()
            if latest_ce and latest_ce.Reversl_Ce:
                reversal_lines.append({'name': f'CE {strike}', 'price': latest_ce.Reversl_Ce, 'color': 'green'})
                
        for strike in pe_strikes:
            # यहाँ भी Time__date=target_date कर दिया है
            latest_pe = OptionChain.objects.filter(Strike_Price=strike, Time__date=target_date).order_by('-Time').first()
            if latest_pe and latest_pe.Reversl_Pe:
                reversal_lines.append({'name': f'PE {strike}', 'price': latest_pe.Reversl_Pe, 'color': 'red'})

        return JsonResponse({
            'candles': candlestick_data,
            'lines': reversal_lines
        })

    return render(request, 'mystock/reversl_chart.html')

def reversal_chart_view(request):
    if request.method == 'POST':
        timeframe_val = request.POST.get('timeframe', '5')
        resample_rule = f"{timeframe_val}min" 

        ce_strikes = [request.POST.get(f'ce_{i}') for i in range(1, 4) if request.POST.get(f'ce_{i}')]
        pe_strikes = [request.POST.get(f'pe_{i}') for i in range(1, 4) if request.POST.get(f'pe_{i}')]
        
        target_date = datetime.now().date() # - timedelta(days=1) # (या आज की डेट)

        # 1. एक यूनिक Cache Key बनाएँ
        # ताकि हर अलग-अलग स्ट्राइक प्राइस और टाइमफ्रेम के लिए अलग कैश बने
        ce_str = "_".join(ce_strikes)
        pe_str = "_".join(pe_strikes)
        cache_key = f"chart_data_{target_date}_{timeframe_val}_{ce_str}_{pe_str}"

        # 2. चेक करें कि क्या डेटा पहले से कैश में मौजूद है
        cached_data = cache.get(cache_key)
        if cached_data:
            print("Serving from Cache! 🚀") # टर्मिनल में चेक करने के लिए
            return JsonResponse(cached_data)

        # ---------------------------------------------------------
        # 3. अगर कैश में नहीं है, तो डेटाबेस से निकालें (Heavy Processing)
        # ---------------------------------------------------------
        print("Fetching from Database and Calculating... ⏳")
        spot_data = OptionChain.objects.filter(
            Time__date=target_date,
            Time__hour__gte=9,
            Time__hour__lte=15
        ).values('Time', 'Spot_Price')

        df = pd.DataFrame(list(spot_data))
        candlestick_data = []
        
        if not df.empty:
            df['Time'] = pd.to_datetime(df['Time'])
            
            if df['Time'].dt.tz is None:
                df['Time'] = df['Time'].dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
            else:
                df['Time'] = df['Time'].dt.tz_convert('Asia/Kolkata')
                
            df.set_index('Time', inplace=True)
            
            ohlc_df = df['Spot_Price'].resample(resample_rule).ohlc().dropna()
            
            for time, row in ohlc_df.iterrows():
                candlestick_data.append({
                    # यहाँ वापस पूरा डेट-टाइम भेजें, हम HTML में इसे '09:15' दिखाएंगे
                    'time': time.strftime('%Y-%m-%d %H:%M:%S'), 
                    'open': row['open'],
                    'high': row['high'],
                    'low': row['low'],
                    'close': row['close'],
                })

        reversal_lines = []
        for strike in ce_strikes:
            latest_ce = OptionChain.objects.filter(Strike_Price=strike, Time__date=target_date).order_by('-Time').first()
            if latest_ce and latest_ce.Reversl_Ce:
                reversal_lines.append({'name': f'CE {strike}', 'price': latest_ce.Reversl_Ce, 'color': 'green'})
                
        for strike in pe_strikes:
            latest_pe = OptionChain.objects.filter(Strike_Price=strike, Time__date=target_date).order_by('-Time').first()
            if latest_pe and latest_pe.Reversl_Pe:
                reversal_lines.append({'name': f'PE {strike}', 'price': latest_pe.Reversl_Pe, 'color': 'red'})

        # ---------------------------------------------------------
        # 4. रिस्पॉन्स डेटा तैयार करें और उसे कैश में सेव कर दें
        # ---------------------------------------------------------
        # 1. चार्ट के लिए फिक्स स्टार्ट और एंड टाइम बनाना (target_date के हिसाब से)
        start_time_str = f"{target_date} 09:15:00"
        end_time_str = f"{target_date} 15:30:00"
        
        response_data = {
            'candles': candlestick_data,
            'lines': reversal_lines,
            'start_time': start_time_str,  # <-- नया 
            'end_time': end_time_str
        }

        # डेटा को 60 सेकंड (या अपनी ज़रूरत के हिसाब से) कैश में सेव करें
        # लाइव मार्केट में 30 या 60 सेकंड सही रहता है
        cache.set(cache_key, response_data, timeout=60) 

        return JsonResponse(response_data)

    return render(request, 'mystock/reversl_chart.html')












"""
views.py  — Resistance / Support Live Dashboard API
====================================================
URLs:
    path('api/resistance/', views.resistance_live_api, name='resistance_live_api'),
    path('resistance/',     views.resistance_dashboard, name='resistance_dashboard'),
"""

from datetime import datetime, timedelta, timezone as dt_timezone
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from django.shortcuts import render
from .models import LiveSRData


# ─────────────────────────────────────────────────────
# IST Helper
# ─────────────────────────────────────────────────────
IST = dt_timezone(timedelta(hours=5, minutes=30))

def to_ist(dt_obj) -> str:
    if dt_obj is None:
        return "—"
    if dt_obj.tzinfo is None:
        dt_obj = dt_obj.replace(tzinfo=dt_timezone.utc)
    return dt_obj.astimezone(IST).strftime("%H:%M:%S")

def today_ist():
    return datetime.now(IST).date()

def now_ist_str() -> str:
    return datetime.now(IST).strftime("%H:%M:%S")


# ─────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────
WTT    = "WTT"
WTB    = "WTB"
STRONG = "STRONG"


# ─────────────────────────────────────────────────────
# State Machine:
#
#  NORMAL   → WTT/WTB found        → SHIFTING  (emit "WTT/WTB X")
#  SHIFTING → pS == shift_strike   → IN_SHIFTED (emit "Shifted WTT/WTB")
#  SHIFTING → pS != shift_strike,
#             STR/STRONG           → NORMAL    (emit "Shifted strong")
#  SHIFTING → pS != shift_strike,
#             WTT/WTB              → SHIFTING  (new shift, emit "WTT/WTB Y")
#  IN_SHIFTED → same strike,
#               same 2nd           → IN_SHIFTED (emit "Shifted WTT/WTB")
#  IN_SHIFTED → same strike,
#               2nd changed        → SHIFTING  (emit "WTT/WTB new_2nd")  ← नई जगह
#  IN_SHIFTED → strike changed,
#               WTT/WTB            → SHIFTING  (emit "WTT/WTB p2nd")
#  IN_SHIFTED → strike changed,
#               STR/STRONG         → NORMAL    (emit "strong pS")
#  CASE 1 same strike, STR/neutral → NORMAL    (emit "Both strong")
# ─────────────────────────────────────────────────────

def _fmt(v):
    """Float → int string if whole number, else float string"""
    if v is None:
        return "—"
    return str(int(v)) if v == int(v) else str(v)


class ResistanceCalculator:
    def __init__(self): self.reset()

    def reset(self):
        self._prev_label    = None
        # SHIFTING state
        self._shifting      = False   # "WTT/WTB X" emit हुआ, X का इंतज़ार
        self._shift_strike  = None    # X (target strike)
        self._shift_wt      = None    # WTT या WTB
        # IN_SHIFTED state
        self._in_shifted    = False   # X high strike बन गई
        self._shifted_wt    = None    # current Shifted label का WTT/WTB
        self._prev_p2nd     = None    # 2nd strike track (नई जगह detect)
        # source
        self._ptype         = "—"
        self._primary_s     = None

    def calculate(self, row_dict):
        label, source = self._compute(row_dict)
        self._prev_label = label
        return label, source

    # ── Source label ────────────────────────────────
    def _src(self, ptype, pS):
        return f"Resistance ({ptype}){_fmt(pS)}"

    # ── Enter SHIFTING ──────────────────────────────
    def _do_shift(self, shift_to, wt, src):
        """नई shift शुरू — emit WTT/WTB shift_to"""
        self._shifting     = True
        self._in_shifted   = False
        self._shift_strike = shift_to
        self._shift_wt     = wt
        self._prev_p2nd    = None
        return f"Resistance {wt} {_fmt(shift_to)}", src

    # ── Reset all states ────────────────────────────
    def _reset(self):
        self._shifting    = False
        self._shift_strike= None
        self._shift_wt    = None
        self._in_shifted  = False
        self._shifted_wt  = None
        self._prev_p2nd   = None

    # ── Core compute ────────────────────────────────
    def _compute(self, r):
        vs    = r.get("ce_high_vol_strike")
        os_   = r.get("ce_high_oi_strike")
        vStat = (r.get("ce_vol_status") or "").upper()
        oStat = (r.get("ce_oi_status")  or "").upper()

        # ════════════════════════════════════════
        # CASE 1: Same Strike
        # ════════════════════════════════════════
        if vs is not None and os_ is not None and vs == os_:
            pS     = vs
            second = r.get("ce_2nd_high_vol_strike") or r.get("ce_2nd_high_oi_strike")
            src    = self._src("Both", pS)

            # दोनों WTT → shift to 2nd
            if vStat == WTT and oStat == WTT:
                self._reset()
                if second:
                    return self._do_shift(second, WTT, src)
                return f"Resistance strong {_fmt(pS)}", src

            # कोई एक WTB → shift to 2nd
            if vStat == WTB or oStat == WTB:
                self._reset()
                if second:
                    return self._do_shift(second, WTB, src)
                return f"Resistance strong {_fmt(pS)}", src

            # STR / neutral → Both strong (SHIFTING भी reset)
            self._reset()
            return "Resistance Both strong", src

        # ════════════════════════════════════════
        # CASE 2: Different Strikes
        # ════════════════════════════════════════
        if vs is not None and os_ is not None:
            if vs < os_:
                pS, pStat, p2nd, pType = vs,  vStat, r.get("ce_2nd_high_vol_strike"), "Vol"
            else:
                pS, pStat, p2nd, pType = os_, oStat, r.get("ce_2nd_high_oi_strike"),  "OI"
        elif vs is not None:
            pS, pStat, p2nd, pType = vs,  vStat, r.get("ce_2nd_high_vol_strike"), "Vol"
        else:
            pS, pStat, p2nd, pType = os_, oStat, r.get("ce_2nd_high_oi_strike"),  "OI"

        src = self._src(pType, pS)

        # ── IN_SHIFTED state ─────────────────────
        if self._in_shifted:
            if pS == self._shift_strike:
                if pStat in (WTT, WTB):
                    # 2nd strike बदली → नई जगह
                    if p2nd is not None and p2nd != self._prev_p2nd:
                        self._in_shifted = False
                        return self._do_shift(p2nd, pStat, src)
                    # Same 2nd → continue Shifted
                    self._shifted_wt = pStat
                    self._prev_p2nd  = p2nd
                    return f"Resistance Shifted {pStat}", src
                else:
                    # STR at shifted strike → strong
                    self._reset()
                    return f"Resistance strong {_fmt(pS)}", src
            else:
                # Strike बदल गई
                self._in_shifted = False
                if pStat in (WTT, WTB) and p2nd:
                    return self._do_shift(p2nd, pStat, src)
                self._reset()
                return f"Resistance strong {_fmt(pS)}", src

        # ── SHIFTING state ───────────────────────
        if self._shifting:
            if pS == self._shift_strike:
                if pStat in (WTT, WTB):
                    # Shift strike high बन गई → IN_SHIFTED
                    self._shifting    = False
                    self._in_shifted  = True
                    self._shifted_wt  = pStat
                    self._prev_p2nd   = p2nd
                    return f"Resistance Shifted {pStat}", src
                else:
                    # STR at shift strike → Shifted strong
                    self._reset()
                    return "Resistance Shifted strong", src
            else:
                # अलग strike
                if pStat in (WTT, WTB) and p2nd:
                    # New shift (नई जगह)
                    return self._do_shift(p2nd, pStat, src)
                # STR / no 2nd → Shifted strong
                self._reset()
                return "Resistance Shifted strong", src

        # ── NORMAL state ─────────────────────────
        if pStat == WTT and p2nd:
            return self._do_shift(p2nd, WTT, src)
        if pStat == WTB and p2nd:
            return self._do_shift(p2nd, WTB, src)

        self._reset()
        return f"Resistance strong {_fmt(pS)}", src


# ─────────────────────────────────────────────────────
# Support Calculator (PE) — Same logic, mirrored
# PE में बड़ी (higher) strike primary होती है
# ─────────────────────────────────────────────────────
class SupportCalculator:
    def __init__(self): self.reset()

    def reset(self):
        self._prev_label   = None
        self._shifting     = False
        self._shift_strike = None
        self._shift_wt     = None
        self._in_shifted   = False
        self._shifted_wt   = None
        self._prev_p2nd    = None
        self._ptype        = "—"
        self._primary_s    = None

    def calculate(self, row_dict):
        label, source = self._compute(row_dict)
        self._prev_label = label
        return label, source

    def _src(self, ptype, pS):
        return f"Support ({ptype}){_fmt(pS)}"

    def _do_shift(self, shift_to, wt, src):
        self._shifting     = True
        self._in_shifted   = False
        self._shift_strike = shift_to
        self._shift_wt     = wt
        self._prev_p2nd    = None
        return f"Support {wt} {_fmt(shift_to)}", src

    def _reset(self):
        self._shifting    = False
        self._shift_strike= None
        self._shift_wt    = None
        self._in_shifted  = False
        self._shifted_wt  = None
        self._prev_p2nd   = None

    def _compute(self, r):
        vs    = r.get("pe_high_vol_strike")
        os_   = r.get("pe_high_oi_strike")
        vStat = (r.get("pe_vol_status") or "").upper()
        oStat = (r.get("pe_oi_status")  or "").upper()

        # CASE 1: Same Strike
        if vs is not None and os_ is not None and vs == os_:
            pS     = vs
            second = r.get("pe_2nd_high_vol_strike") or r.get("pe_2nd_high_oi_strike")
            src    = self._src("Both", pS)

            if vStat == WTT and oStat == WTT:
                self._reset()
                if second:
                    return self._do_shift(second, WTT, src)
                return f"Support strong {_fmt(pS)}", src

            if vStat == WTB or oStat == WTB:
                self._reset()
                if second:
                    return self._do_shift(second, WTB, src)
                return f"Support strong {_fmt(pS)}", src

            self._reset()
            return "Support Both strong", src

        # CASE 2: Different Strikes — PE में बड़ी strike primary
        if vs is not None and os_ is not None:
            if vs > os_:
                pS, pStat, p2nd, pType = vs,  vStat, r.get("pe_2nd_high_vol_strike"), "Vol"
            else:
                pS, pStat, p2nd, pType = os_, oStat, r.get("pe_2nd_high_oi_strike"),  "OI"
        elif vs is not None:
            pS, pStat, p2nd, pType = vs,  vStat, r.get("pe_2nd_high_vol_strike"), "Vol"
        else:
            pS, pStat, p2nd, pType = os_, oStat, r.get("pe_2nd_high_oi_strike"),  "OI"

        src = self._src(pType, pS)

        if self._in_shifted:
            if pS == self._shift_strike:
                if pStat in (WTT, WTB):
                    if p2nd is not None and p2nd != self._prev_p2nd:
                        self._in_shifted = False
                        return self._do_shift(p2nd, pStat, src)
                    self._shifted_wt = pStat
                    self._prev_p2nd  = p2nd
                    return f"Support Shifted {pStat}", src
                else:
                    self._reset()
                    return f"Support strong {_fmt(pS)}", src
            else:
                self._in_shifted = False
                if pStat in (WTT, WTB) and p2nd:
                    return self._do_shift(p2nd, pStat, src)
                self._reset()
                return f"Support strong {_fmt(pS)}", src

        if self._shifting:
            if pS == self._shift_strike:
                if pStat in (WTT, WTB):
                    self._shifting    = False
                    self._in_shifted  = True
                    self._shifted_wt  = pStat
                    self._prev_p2nd   = p2nd
                    return f"Support Shifted {pStat}", src
                else:
                    self._reset()
                    return "Support Shifted strong", src
            else:
                if pStat in (WTT, WTB) and p2nd:
                    return self._do_shift(p2nd, pStat, src)
                self._reset()
                return "Support Shifted strong", src

        if pStat == WTT and p2nd:
            return self._do_shift(p2nd, WTT, src)
        if pStat == WTB and p2nd:
            return self._do_shift(p2nd, WTB, src)

        self._reset()
        return f"Support strong {_fmt(pS)}", src


# ─────────────────────────────────────────────────────
# Per-symbol calculator cache
# ─────────────────────────────────────────────────────
_CALC_CACHE = {}

def _get_calculators(symbol: str, today):
    if symbol in _CALC_CACHE:
        cached_date, res_calc, sup_calc = _CALC_CACHE[symbol]
        if cached_date == today:
            return res_calc, sup_calc
    res_calc = ResistanceCalculator()
    sup_calc = SupportCalculator()
    _CALC_CACHE[symbol] = (today, res_calc, sup_calc)
    return res_calc, sup_calc


def _row_to_dict(obj):
    return {
        "ce_high_vol_strike":      obj.ce_high_vol_strike,
        "ce_vol_status":           obj.ce_vol_status,
        "ce_2nd_high_vol_strike":  obj.ce_2nd_high_vol_strike,
        "ce_high_oi_strike":       obj.ce_high_oi_strike,
        "ce_oi_status":            obj.ce_oi_status,
        "ce_2nd_high_oi_strike":   obj.ce_2nd_high_oi_strike,
        "pe_high_vol_strike":      obj.pe_high_vol_strike,
        "pe_vol_status":           obj.pe_vol_status,
        "pe_2nd_high_vol_strike":  obj.pe_2nd_high_vol_strike,
        "pe_high_oi_strike":       obj.pe_high_oi_strike,
        "pe_oi_status":            obj.pe_oi_status,
        "pe_2nd_high_oi_strike":   obj.pe_2nd_high_oi_strike,
    }


# ─────────────────────────────────────────────────────
# API View
# ─────────────────────────────────────────────────────
@require_GET
def resistance_live_api(request):
    symbol = request.GET.get("symbol", "NIFTY").upper()
    limit  = min(int(request.GET.get("limit", 50)), 200)
    today  = today_ist()

    qs = (LiveSRData.objects
          .filter(Time__date=today, Symbol=symbol)
          .order_by("Time"))

    res_calc, sup_calc = _get_calculators(symbol, today)
    res_calc.reset()
    sup_calc.reset()

    all_rows  = list(qs)
    processed = []

    for obj in all_rows:
        rd = _row_to_dict(obj)
        resistance, res_source = res_calc.calculate(rd)
        support,    sup_source = sup_calc.calculate(rd)

        processed.append({
            "time":   to_ist(obj.Time),
            "spot":   obj.Spot_Price,
            "expiry": obj.Expiry_Date or "",
            # CE
            "ce_vol_strike": obj.ce_high_vol_strike,
            "ce_vol_status": obj.ce_vol_status or "",
            "ce_vol_2nd":    obj.ce_2nd_high_vol_strike,
            "ce_oi_strike":  obj.ce_high_oi_strike,
            "ce_oi_status":  obj.ce_oi_status or "",
            "ce_oi_2nd":     obj.ce_2nd_high_oi_strike,
            # PE
            "pe_vol_strike": obj.pe_high_vol_strike,
            "pe_vol_status": obj.pe_vol_status or "",
            "pe_vol_2nd":    obj.pe_2nd_high_vol_strike,
            "pe_oi_strike":  obj.pe_high_oi_strike,
            "pe_oi_status":  obj.pe_oi_status or "",
            "pe_oi_2nd":     obj.pe_2nd_high_oi_strike,
            # Calculated
            "resistance":  resistance,
            "res_source":  res_source,
            "support":     support,
            "sup_source":  sup_source,
        })

    result = list(reversed(processed[-limit:]))

    return JsonResponse({
        "symbol":      symbol,
        "date":        str(today),
        "total_rows":  len(all_rows),
        "rows":        result,
        "latest":      result[0] if result else None,
        "server_time": now_ist_str(),
    })

# ─────────────────────────────────────────────────────
# Dashboard View
# ─────────────────────────────────────────────────────
def resistance_dashboard(request):
    return render(request, "mystock/resistance_dashboard.html")




import requests
from datetime import date, timedelta

from django.shortcuts import render
from django.http import JsonResponse
from .credentials1 import access_token
from .models import InstrumentStore, OptionChain, LiveSRData  # अपने app का नाम use करें

# ─────────────────────────────────────────────
# Helper: Symbol → instrument_key (DB से)
# ─────────────────────────────────────────────
def get_instrument_key(symbol: str):
    """
    InstrumentStore से symbol के basis पर instrument_key लौटाता है।
    symbol case-insensitive match होता है।
    नहीं मिला तो None return करता है।
    """
    try:
        obj = InstrumentStore.objects.get(symbol__iexact=symbol.strip())
        return obj.instrument_key
    except InstrumentStore.DoesNotExist:
        return None



# ─────────────────────────────────────────────
# Helper: OptionChain से Reversal lines fetch
# सिर्फ Support–Resistance के बीच की strikes
# ─────────────────────────────────────────────
def get_reversal_lines(symbol: str, from_date: str, to_date: str):
    """
    1. LiveSRData से latest resistance_strike और supprt_strike लो
    2. OptionChain से उन strikes की rows लो जो उस range में हों
    3. हर strike की Reversl_Ce (लाल) और Reversl_Pe (हरी) line बनाओ
    """
    try:
        # ── Step 1: SR range ──
        sr = LiveSRData.objects.filter(
            Symbol__iexact=symbol,
            Time__date__gte=from_date,
            Time__date__lte=to_date,
        ).order_by('-Time').first()

        if not sr or not sr.resistance_strike or not sr.supprt_strike:
            return []

        low  = min(sr.supprt_strike,    sr.resistance_strike)
        high = max(sr.resistance_strike, sr.supprt_strike)

        # ── Step 2: OptionChain — हर Strike की latest row ──
        # latest_time से exact filter नहीं करते — हर strike की
        # latest entry अलग अलग समय की हो सकती है
        from django.db.models import Max

        oc_qs = OptionChain.objects.filter(
            Symbol__iexact=symbol,
            Time__date__gte=from_date,
            Time__date__lte=to_date,
            Strike_Price__gte=low,
            Strike_Price__lte=high,
        )

        if not oc_qs.exists():
            return []

        # हर Strike_Price की सबसे latest Time लो
        latest_per_strike = (
            oc_qs.values('Strike_Price')
                 .annotate(latest=Max('Time'))
        )

        # उन rows को fetch करो
        rows = []
        for entry in latest_per_strike:
            row = oc_qs.filter(
                Strike_Price=entry['Strike_Price'],
                Time=entry['latest']
            ).first()
            if row:
                rows.append(row)

        # Strike_Price से sort करो
        rows.sort(key=lambda r: r.Strike_Price)

        # ── Step 3: Lines बनाओ ──
        lines    = []
        seen_ce  = set()   # CE के लिए अलग
        seen_pe  = set()   # PE के लिए अलग

        for row in rows:
            strike = row.Strike_Price

            # ── CE Reversal ──
            if row.Reversl_Ce and row.Reversl_Ce not in seen_ce:
                seen_ce.add(row.Reversl_Ce)
                is_top = (strike == sr.resistance_strike)
                lines.append({
                    "price":  row.Reversl_Ce,
                    "strike": strike,
                    "type":   "CE",
                    "color":  "#f85149",
                    "width":  2 if is_top else 1,
                    "dash":   0 if is_top else 2,
                    "label":  f"R {strike:.0f}" if is_top else f"CE {strike:.0f}",
                })

            # ── PE Reversal ──
            if row.Reversl_Pe and row.Reversl_Pe not in seen_pe:
                seen_pe.add(row.Reversl_Pe)
                is_bottom = (strike == sr.supprt_strike)
                lines.append({
                    "price":  row.Reversl_Pe,
                    "strike": strike,
                    "type":   "PE",
                    "color":  "#3fb950",
                    "width":  2 if is_bottom else 1,
                    "dash":   0 if is_bottom else 2,
                    "label":  f"S {strike:.0f}" if is_bottom else f"PE {strike:.0f}",
                })

        # Strike के हिसाब से sort (ऊपर से नीचे)
        lines.sort(key=lambda x: x["price"], reverse=True)
        return lines

    except Exception:
        return []


# ─────────────────────────────────────────────
# Helper: Upstox API से candle data fetch
# ─────────────────────────────────────────────
def fetch_candle_data(instrument_key: str, unit: str, interval: str, to_date: str, from_date: str):
    """
    आज की date है  → Intraday endpoint (date params नहीं चाहिए)
    पुरानी date है → Historical endpoint (from/to date जरूरी)
    """
    encoded_key = requests.utils.quote(instrument_key, safe='')
    today_str   = date.today().isoformat()

    if from_date == today_str:
        # ── Intraday (live today's data) ──
        url = (
            f"https://api.upstox.com/v3/historical-candle/intraday/"
            f"{encoded_key}/{unit}/{interval}"
        )
    else:
        # ── Historical (past dates) ──
        url = (
            f"https://api.upstox.com/v3/historical-candle/"
            f"{encoded_key}/{unit}/{interval}/{to_date}/{from_date}"
        )

    headers = {
        "Content-Type": "application/json",
        "Accept":        "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            return {"success": True, "data": response.json()}
        else:
            return {"success": False, "error": f"API Error {response.status_code}: {response.text}"}
    except requests.exceptions.RequestException as e:
        return {"success": False, "error": f"Connection Error: {str(e)}"}


# ─────────────────────────────────────────────
# Helper: Raw candles parse करना
# ─────────────────────────────────────────────
def parse_candles(api_response: dict):
    """
    Upstox v3 response से candle list बनाता है।
    Format: [timestamp, open, high, low, close, volume, oi]
    """
    raw = api_response.get("data", {}).get("candles", [])
    candles = []
    for c in raw:
        candles.append({
            "time":   c[0],
            "open":   c[1],
            "high":   c[2],
            "low":    c[3],
            "close":  c[4],
            "volume": c[5],
            "oi":     c[6] if len(c) > 6 else 0,
        })
    candles.reverse()
    return candles


# ─────────────────────────────────────────────
# View 1: Chart Page (HTML render)
# ─────────────────────────────────────────────
def chart_view(request):
    today     = date.today()
    symbol    = request.GET.get("symbol",    "NIFTY").strip().upper()
    unit      = request.GET.get("unit",      "minutes")
    interval  = request.GET.get("interval",  "5")
    from_date = request.GET.get("from_date", today.isoformat())  # default → आज
    to_date   = request.GET.get("to_date",   today.isoformat())   # default → आज

    candles        = []
    error          = None
    instrument_key = None

    # Step 1: DB से instrument_key लो
    instrument_key = get_instrument_key(symbol)

    if not instrument_key:
        error = f"'{symbol}' symbol DB में नहीं मिला। पहले InstrumentStore में add करें।"
    else:
        # Step 2: Upstox API hit करो
        result = fetch_candle_data(instrument_key, unit, interval, to_date, from_date)

        if not result["success"]:
            error = result["error"]
        else:
            candles = parse_candles(result["data"])
            if not candles:
                error = "इस date range में कोई candle data नहीं मिली।"

    # Step 3: Reversal lines — OptionChain से
    reversal_lines = get_reversal_lines(symbol, from_date, to_date)


    context = {
        "candles":        candles,
        "reversal_lines": reversal_lines,
        "error":          error,
        "symbol":         symbol,
        "instrument_key": instrument_key or "—",
        "unit":           unit,
        "interval":       interval,
        "from_date":      from_date,
        "to_date":        to_date,
    }
    return render(request, "mystock/chart.html", context)


# ─────────────────────────────────────────────
# View 2: AJAX JSON API endpoint
# ─────────────────────────────────────────────
def candle_api(request):
    symbol    = request.GET.get("symbol",    "").strip().upper()
    unit      = request.GET.get("unit",      "minutes")
    interval  = request.GET.get("interval",  "5")
    from_date = request.GET.get("from_date", "")
    to_date   = request.GET.get("to_date",   "")

    if not symbol:
        return JsonResponse({"error": "symbol parameter जरूरी है।"}, status=400)

    # Step 1: DB से instrument_key
    instrument_key = get_instrument_key(symbol)
    if not instrument_key:
        return JsonResponse(
            {"error": f"'{symbol}' symbol DB में नहीं मिला।"},
            status=404
        )

    # Step 2: API call
    result = fetch_candle_data(instrument_key, unit, interval, to_date, from_date)
    if not result["success"]:
        return JsonResponse({"error": result["error"]}, status=400)

    candles        = parse_candles(result["data"])
    reversal_lines = get_reversal_lines(symbol, from_date, to_date)

    return JsonResponse({
        "symbol":         symbol,
        "instrument_key": instrument_key,
        "interval":       interval,
        "from_date":      from_date,
        "to_date":        to_date,
        "count":          len(candles),
        "candles":        candles,
        "reversal_lines": reversal_lines,
    })


# ─────────────────────────────────────────────
# View 3: Symbol Autocomplete Search
# ─────────────────────────────────────────────
def symbol_search(request):
    """
    ?q=REL → DB में RELIANCE, RELINFRA आदि ढूंढता है
    Frontend autocomplete के लिए उपयोगी
    """
    query = request.GET.get("q", "").strip()
    if len(query) < 1:
        return JsonResponse({"results": []})

    results = InstrumentStore.objects.filter(
        symbol__icontains=query
    ).values("symbol", "instrument_key", "lot_size", "expiry_dates")[:20]

    return JsonResponse({"results": list(results)})



 






