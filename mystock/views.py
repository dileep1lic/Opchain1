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
from .management.commands.async_live import get_smart_expiry 
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
        if len(ranked) > 0: setattr(ranked[0], base_class, "bg-green")
        if len(ranked) > 1 and (getattr(ranked[1], metric) or 0) >= 75: 
            setattr(ranked[1], base_class, "bg-red")
        if len(ranked) > 2 and (getattr(ranked[2], metric) or 0) >= 65: 
            setattr(ranked[2], base_class, "bg-yellow")

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
    expiry_list = get_smart_expiry(symbol)
    
    # 3. EXPIRY SELECTION LOGIC
    if url_expiry and url_expiry in expiry_list:
        selected_expiry = url_expiry
    else:
        selected_expiry = expiry_list[0] if expiry_list else ''

    # 4. AJAX Check
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    # 5. DATA FETCHING FROM DB (TempOptionChain)
    # हम सीधे DB से डेटा निकालेंगे जो Background Loop ने सेव किया है
    queryset = TempOptionChain.objects.filter(Symbol=symbol).order_by('Strike_Price')
    
    # अगर एक्सपायरी सेलेक्टेड है, तो उससे फिल्टर करें
    if selected_expiry:
        queryset = queryset.filter(Expiry_Date=selected_expiry)

    latest_data = list(queryset)

    spot_price = 0
    latest_time = None
    lot_size = 1
    display_data = []

    if latest_data:
        # बेसिक मेटा-डेटा निकालें (पहले रो से)
        first_row = latest_data[0]
        spot_price = first_row.Spot_Price
        latest_time = first_row.Time
        lot_size = first_row.Lot_size

        # 6. RANKING & COLOR LOGIC (Dashboard जैसा)
        metrics = ['CE_OI_percent', 'CE_Volume_percent', 'CE_COI_percent',
                   'PE_OI_percent', 'PE_Volume_percent', 'PE_COI_percent']

        for metric in metrics:
            # मेट्रिक के हिसाब से सॉर्ट करें (Descending)
            ranked = sorted(latest_data, key=lambda x: getattr(x, metric) or 0, reverse=True)
            base_class = metric.replace('_percent', '_class')
            
            # 1st Rank -> Green
            if len(ranked) > 0: 
                setattr(ranked[0], base_class, "bg-green")
            
            # 2nd Rank -> Red (Only if value >= 75%)
            if len(ranked) > 1:
                val2 = getattr(ranked[1], metric) or 0
                if val2 >= 75: 
                    setattr(ranked[1], base_class, "bg-red")
            
            # 3rd Rank -> Yellow (Only if value >= 65%)
            if len(ranked) > 2:
                val3 = getattr(ranked[2], metric) or 0
                if val3 >= 65: 
                    setattr(ranked[2], base_class, "bg-yellow")

        # 7. WINDOW FILTERING (±15 Strikes around Spot Price)
        # स्पॉट प्राइस के सबसे करीब वाली स्ट्राइक ढूंढें
        closest_obj = min(latest_data, key=lambda x: abs(x.Strike_Price - spot_price))
        closest_idx = latest_data.index(closest_obj)
        
        # रेंज सेट करें (15 ऊपर, 15 नीचे)
        start_idx = max(0, closest_idx - 15)
        end_idx = min(len(latest_data), closest_idx + 16)
        
        display_data = latest_data[start_idx : end_idx]

        # 8. SPOT DIVIDER LOGIC
        # जहाँ स्ट्राइक प्राइस > स्पॉट प्राइस हो, वहां डिवाइडर मार्क करें
        for row in display_data:
            if row.Strike_Price > spot_price:
                row.is_spot_divider = True # Template में इसका इस्तेमाल करें
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
    }

    # अगर AJAX रिक्वेस्ट है (Auto-Refresh) तो सिर्फ टेबल भेजें 
    if is_ajax:
        return render(request, 'mystock/table_partial.html', context)
    
    # अगर पहली बार पेज लोड हो रहा है
    return render(request, 'mystock/search_dashboard.html', context)

def trigger_expiry_update(request):
    symbols_to_update = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX","RELIANCE"]
    
    for symbol in symbols_to_update:
        get_smart_expiry(symbol)
        
    return JsonResponse({"status": "success", "message": "Expiry dates updated successfully!"})

# 1. API View (जो सिर्फ डेटा देगा)
def specific_strike_oi_data(request):
    symbol = request.GET.get('symbol', 'NIFTY')
    strike_price = request.GET.get('strike')

    if not strike_price:
        return JsonResponse({"error": "Strike required"}, status=400)

    ist = pytz.timezone('Asia/Kolkata')
    today = timezone.localdate()

    # डेटा निकालें (सिर्फ जरूरी फील्ड्स)
    data = list(
        OptionChain.objects.filter(
            Symbol=symbol,
            Strike_Price=strike_price,
            Time__date=today
        ).order_by('Time').values(
            'Time', 'CE_OI', 'PE_OI', 'CE_OI_percent', 'PE_OI_percent'
        )
    )

    if not data:
        return JsonResponse({"error": "No data found"}, status=404)

    # Time Format (HH:MM)
    times = [
        timezone.localtime(entry['Time'], ist).strftime("%H:%M")
        for entry in data
    ]

    response = {
        "times": times,
        "ce_oi": [entry['CE_OI'] for entry in data],
        "pe_oi": [entry['PE_OI'] for entry in data],
        "ce_pct": [entry['CE_OI_percent'] for entry in data],
        "pe_pct": [entry['PE_OI_percent'] for entry in data],
    }

    return JsonResponse(response)

# 2. Page View (जो खाली HTML पेज खोलेगा)
@xframe_options_exempt
def render_chart_page(request):
    return render(request, 'mystock/oi_chart_js.html', {
        'symbol': request.GET.get('symbol'),
        'strike': request.GET.get('strike')
    })

# 1. API View (जो सिर्फ COI डेटा देगा)
def specific_strike_coi_data(request):
    symbol = request.GET.get('symbol', 'NIFTY')
    strike_price = request.GET.get('strike')

    if not strike_price:
        return JsonResponse({"error": "Strike required"}, status=400)

    ist = pytz.timezone('Asia/Kolkata')
    today = timezone.localdate()

    # डेटा निकालें (सिर्फ जरूरी फील्ड्स)
    data = list(
        OptionChain.objects.filter(
            Symbol=symbol,
            Strike_Price=strike_price,
            Time__date=today
        ).order_by('Time').values(
            'Time', 'CE_COI', 'PE_COI', 'CE_COI_percent', 'PE_COI_percent'
        )
    )

    if not data:
        return JsonResponse({"error": "No data found"}, status=404)

    # Time Format (HH:MM)
    times = [
        timezone.localtime(entry['Time'], ist).strftime("%H:%M")
        for entry in data
    ]

    response = {
        "times": times,
        "ce_coi": [entry['CE_COI'] for entry in data],
        "pe_coi": [entry['PE_COI'] for entry in data],
        "ce_pct": [entry['CE_COI_percent'] for entry in data],
        "pe_pct": [entry['PE_COI_percent'] for entry in data],
    }

    return JsonResponse(response)

# 2. अपने चार्ट पेज वाले फंक्शन के ऊपर यह लाइन लिखें
@xframe_options_exempt
def render_chart_page_coi(request):
    return render(request, 'mystock/coi_chart_js.html', {
        'symbol': request.GET.get('symbol'),
        'strike': request.GET.get('strike')
    })






