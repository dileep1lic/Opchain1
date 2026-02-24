import requests
import time
from datetime import datetime, timedelta
from django.shortcuts import render
from requests.exceptions import SSLError, ConnectionError, Timeout
from .models import OptionChain, SupportResistance, SyncControl, TempOptionChain
from django.utils import timezone
from django.db.models import OuterRef, Subquery, Q
from django.views.decorators.cache import never_cache
from django.http import HttpResponse, JsonResponse
from django.views.decorators.cache import cache_page
from .management.commands.async_live import get_instrument_from_db, update_instrument_store_bulk 
from .symbol import symbols as ALL_SYMBOLS
from django.views.decorators.clickjacking import xframe_options_exempt
import pytz

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

# Dashboard Views Start Here
def option_chain_dashboard(request):
    # 1. Sabse latest entry ka time nikalna
    latest_entry = OptionChain.objects.order_by('-Time').first()

    if not latest_entry:
        return render(request, 'mystock/dashboard.html', {'data': [], 'latest_time': None})

    # 2. Latest Time aur Spot Price
    latest_time = latest_entry.Time
    spot_price = latest_entry.Spot_Price
    expiry_date = latest_entry.Expiry_Date

    # 3. Time Buffer Logic (Taaki us second ki saari strikes mil jayein)
    all_data = list(
        OptionChain.objects.filter(
            Time__gte=latest_time - timedelta(seconds=1),
            Time__lte=latest_time + timedelta(seconds=1)
        ).order_by('Strike_Price')
    )

    # 4. TOP 3 RANKING LOGIC
    metrics = ['CE_OI_percent', 'CE_Volume_percent', 'CE_COI_percent',
               'PE_OI_percent', 'PE_Volume_percent', 'PE_COI_percent']

    for metric in metrics:
        ranked = sorted(all_data, key=lambda x: getattr(x, metric) or 0, reverse=True)
        base_class = metric.replace('_percent', '_class')
        
        if len(ranked) > 0: 
            setattr(ranked[0], base_class, "bg-green")
        if len(ranked) > 1:
            val2 = getattr(ranked[1], metric) or 0
            if val2 >= 75: setattr(ranked[1], base_class, "bg-red")
        if len(ranked) > 2:
            val3 = getattr(ranked[2], metric) or 0
            if val3 >= 65: setattr(ranked[2], base_class, "bg-yellow")

    # 5. WINDOW FILTERING (±15 Strikes)
    if all_data:
        # Spot ke sabse paas wali index nikalna
        closest_idx = min(range(len(all_data)), key=lambda i: abs(all_data[i].Strike_Price - spot_price))
        
        start = max(0, closest_idx - 15)
        end = min(len(all_data), closest_idx + 16)
        display_data = all_data[start:end]

        # 6. SINGLE SPOT LINE LOGIC (For Dashboard Divider)
        # Pehli strike jo spot price se badi hai, uspar marker lagao
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

 # अगर डेटा न हो तो खाली स्ट्रिंग भेजने के लिए

def table_update_api(request):
    latest_entry = OptionChain.objects.order_by('-Time').first()

    # अगर डेटाबेस खाली है, तो खाली रिस्पॉन्स भेजें ताकि JS एरर न दे
    if not latest_entry:
        return HttpResponse("") 

    latest_time = latest_entry.Time
    spot_price = latest_entry.Spot_Price
    expiry_date = latest_entry.Expiry_Date
    # टोटल्स के लिए वेरिएबल्स
    total_ce_oi = 0
    total_pe_oi = 0
    total_ce_coi = 0
    total_pe_coi = 0

    all_data = list(
        OptionChain.objects.filter(
            Time__gte=latest_time - timedelta(seconds=1),
            Time__lte=latest_time + timedelta(seconds=1)
        ).order_by('Strike_Price')
    )
    
    # Ranking Logic (बिल्कुल सही है)
    metrics = ['CE_OI_percent', 'CE_Volume_percent', 'CE_COI_percent',
            'PE_OI_percent', 'PE_Volume_percent', 'PE_COI_percent']

    for metric in metrics:
        ranked = sorted(all_data, key=lambda x: getattr(x, metric) or 0, reverse=True)
        base_class = metric.replace('_percent', '_class')
        
        # रैंक 1 के लिए (हमेशा हरा)
        if len(ranked) > 0: 
            setattr(ranked[0], base_class, "bg-green")
            
        val_2nd = 0  # रैंक 2 की वैल्यू को यहाँ सेव करेंगे ताकि रैंक 3 में इसका इस्तेमाल कर सकें
        
        # रैंक 2 के लिए (लाल या पीला)
        if len(ranked) > 1:
            val_2nd = getattr(ranked[1], metric) or 0
            if val_2nd >= 75: 
                setattr(ranked[1], base_class, "bg-red")
            elif 65 <= val_2nd < 75: 
                setattr(ranked[1], base_class, "bg-yellow")
                
        # रैंक 3 के लिए 
        if len(ranked) > 2:
            val_3rd = getattr(ranked[2], metric) or 0
            
            # नया नियम: रैंक 3 को पीला रंग तभी मिलेगा जब वह खुद 65+ हो और रैंक 2 की वैल्यू 75+ हो
            if val_3rd >= 65 and val_2nd >= 75: 
                setattr(ranked[2], base_class, "bg-yellow")

    # 7. TOTAL OI AND COI CALCULATION (पूरे डेटा का टोटल)
    if all_data:
        total_ce_oi = sum(row.CE_OI or 0 for row in all_data)
        total_pe_oi = sum(row.PE_OI or 0 for row in all_data)
        total_ce_coi = sum(row.CE_COI or 0 for row in all_data)
        total_pe_coi = sum(row.PE_COI or 0 for row in all_data)

    # Filtering & Divider logic (बिल्कुल सही है)
    if all_data:
        closest_idx = min(range(len(all_data)), key=lambda i: abs(all_data[i].Strike_Price - spot_price))
        display_data = all_data[max(0, closest_idx - 15) : min(len(all_data), closest_idx + 16)]
        for row in display_data:
            if row.Strike_Price > spot_price:
                row.is_spot_divider = True
                break 
    else:
        display_data = []

    context = {
        'data': display_data,
        'latest_time': latest_time,
        'spot': spot_price,
        'expiry_date': expiry_date,
        # कॉन्टेक्स्ट में पूरे डेटा का टोटल पास कर रहे हैं
        'total_ce_oi': total_ce_oi,
        'total_pe_oi': total_pe_oi,
        'total_ce_coi': total_ce_coi,
        'total_pe_coi': total_pe_coi,
    }
    
    # यह table_partial.html में सिर्फ <tbody> और उसकी Rows होनी चाहिए
    return render(request, 'mystock/table_partial.html', context)
# 
@never_cache  # यह ब्राउज़र को पुराना पेज दिखाने से रोकेगा
# def dashboard(request):
#     # 'get_or_create' का उपयोग करें ताकि अगर रिकॉर्ड न हो तो बन जाए
#     nifty_obj, _ = SyncControl.objects.get_or_create(name="nifty_loop")
#     others_obj, _ = SyncControl.objects.get_or_create(name="others_loop")
    
#     # बाकी डेटा फेच करें
#     data = OptionChain.objects.filter(Symbol="NIFTY").order_by('-Time')[:50]
    
#     context = {
#         'data': data,
#         'nifty_active': nifty_obj.is_active,  # यहाँ से HTML को वैल्यू मिलेगी
#         'others_active': others_obj.is_active,
#         'spot': data[0].Spot_Price if data else 0,
#         'latest_time': data[0].Time if data else None,
#     }
#     return render(request, 'dashboard.html', context)

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

from asgiref.sync import async_to_sync

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
            ranked = sorted(latest_data, key=lambda x: getattr(x, metric) or 0, reverse=True)
            base_class = metric.replace('_percent', '_class')
            
            if len(ranked) > 0: 
                setattr(ranked[0], base_class, "bg-green")
            
            if len(ranked) > 1:
                val2 = getattr(ranked[1], metric) or 0
                if val2 >= 75: 
                    setattr(ranked[1], base_class, "bg-red")
            
            if len(ranked) > 2:
                val3 = getattr(ranked[2], metric) or 0
                if val3 >= 65: 
                    setattr(ranked[2], base_class, "bg-yellow")

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
    }

    if is_ajax:
        return render(request, 'mystock/table_partial.html', context)
    
    return render(request, 'mystock/search_dashboard.html', context)

def trigger_expiry_update(request):
    # symbols_to_update = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX","RELIANCE"]
    
    # for symbol in symbols_to_update:
    update_instrument_store_bulk()
        
    return JsonResponse({"status": "success", "message": "Expiry dates updated successfully!"})

# 1. API View (जो सिर्फ डेटा देगा)
from datetime import time as dt_time

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






