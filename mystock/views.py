import requests
import time
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from django.shortcuts import render, redirect
from requests.exceptions import SSLError, ConnectionError, Timeout
from .models import OptionChain, SupportResistance, SyncControl, ExpiryCache, TempOptionChain
from .credentials import access_token
from django.utils import timezone
from django.db.models import OuterRef, Subquery
from django.views.decorators.cache import never_cache
from django.http import HttpResponse, JsonResponse
from asgiref.sync import sync_to_async
from django.views.decorators.cache import cache_page
import asyncio
import aiohttp
from .management.commands.async_live import calculate_data_async_optimized, get_smart_expiry 
from .symbol import symbols as ALL_SYMBOLS


# Token ko ek jagah define karein (Ideally settings.py ya .env mein hona chahiye)
# ACCESS_TOKEN = "your_access_token_here" 
# EXPIRY_DATE = "2026-02-03"

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
# This code not used anywhere else, so defining here for completeness
# def get_instrument_key(symbol):
#     scripts = pd.read_csv('NSE.csv')
#     filtered = scripts[scripts['tradingsymbol'] == symbol]

#     if not filtered.empty:
#         return filtered['instrument_key'].values[0]

#     fallback = {
#         'NIFTY': 'NSE_INDEX|Nifty 50',
#         'BANKNIFTY': 'NSE_INDEX|Nifty Bank',
#         'FINNIFTY': 'NSE_INDEX|Nifty Fin Service',
#         'MIDCPNIFTY': 'NSE_INDEX|NIFTY MID SELECT',
#         'CRUDEOIL': 'MCX_FO|436953'
#     }
#     return fallback.get(symbol, None)

# def get_option_chain(symbol, expiry_Date):
#     key = get_instrument_key(symbol)
#     if not key:
#         print(f"⚠ Instrument key nahi mili: {symbol}")
#         return None

#     url = 'https://api.upstox.com/v2/option/chain'
#     params = {'instrument_key': key, 'expiry_date': expiry_Date}
#     headers = {
#         'Accept': 'application/json',
#         'Authorization': f'Bearer {access_token}'
#     }

#     try:
#         # 1. Timeout ko thoda badhana behtar hai kyunki Option Chain ka data heavy hota hai
#         res = requests.get(url, params=params, headers=headers, timeout=10)

#         # 2. Check karein ki request successful thi ya nahi (e.g., 200 OK)
#         res.raise_for_status() 

#         data = res.json()

#         # 3. Data validation
#         if "data" not in data or not data["data"]:
#             print(f"⚠ Empty option chain received for {symbol} on {expiry_Date}!")
#             return None

#         return data

#     except requests.exceptions.HTTPError as e:
#         # Agar Token expire ho gaya ya galat URL hai
#         print(f"❌ HTTP Error: {e.response.status_code} - {e.response.text}")
#     except requests.exceptions.Timeout:
#         print("❌ Request Timeout: Upstox server response nahi de raha.")
#     except Exception as e:
#         print(f"❌ Unexpected Error: {e}")
    
#     return None

# def get_Name_Lot_size(symbol):
#     key = get_instrument_key(symbol)
#     if not key:
#         return None, None

#     url = "https://api.upstox.com/v2/option/contract"
#     headers = {
#         'Accept': 'application/json',
#         'Authorization': f'Bearer {access_token}'
#     }

#     try:
#         # Aapne pehle 'safe_get' banaya hai, wahi use karein
#         response = safe_get(
#             url,
#             headers=headers,
#             params={'instrument_key': key}
#         )

#         # Agar safe_get ne None return kiya ya data nahi mila
#         if not response or "data" not in response or not response["data"]:
#             print(f"⚠ No contract data found for {symbol}")
#             return None, None

#         # Pehla instrument lein (usually index ya stock contract)
#         contract_data = response["data"][0]
        
#         underlying = contract_data.get("underlying_symbol")
#         lot_size = contract_data.get("lot_size")

#         return underlying, lot_size

#     except Exception as e:
#         print(f"⚠ Lot size fetch failed for {symbol}: {str(e)}")
#         return None, None
    
# def data_to_df(symbol, expiry_Date):
#     response_data = get_option_chain(symbol, expiry_Date)
#     if not response_data:
#         return None

#     symbol_name, lot_size = get_Name_Lot_size(symbol)
    
#     # Lot size 0 ya None nahi hona chahiye (Division error se bachne ke liye)
#     if not symbol_name or not lot_size or lot_size == 0:
#         print(f"⚠ Invalid lot size for {symbol}")
#         return None

#     data = response_data.get("data", [])
#     now = datetime.now() # Django model ke liye object hi rehne dein, string nahi

#     rows = []
#     for entry in data:
#         strike = entry.get("strike_price")

#         # --- Helper function for cleaner code ---
#         def get_market_data(option_type):
#             opt = entry.get(option_type) or {}
#             md = opt.get("market_data") or {}
#             greeks = opt.get("option_greeks") or {}
#             return md, greeks

#         ce_md, ce_g = get_market_data("call_options")
#         pe_md, pe_g = get_market_data("put_options")

#         # Row data structure (Aapke Model ke columns ke hisaab se)
#         current_sync_time = timezone.now()
#         rows.append({
#             "Time": current_sync_time,
#             "Symbol": symbol_name,
#             "Expry_Date": expiry_Date,
#             "Strike_Price": strike,
#             "Spot_Price": response_data.get("underlying_spot_price", 0), # Spot price add kiya
            
#             # CE DATA
#             "CE_Delta": ce_g.get("delta", 0),
#             "CE_IV": ce_g.get("iv", 0),
#             "CE_COI": (ce_md.get("oi", 0) - ce_md.get("prev_oi", 0)) / lot_size,
#             "CE_OI": ce_md.get("oi", 0) / lot_size,
#             "CE_Volume": ce_md.get("volume", 0) / lot_size,
#             "CE_LTP": ce_md.get("ltp", 0),
#             "CE_CLTP": ce_md.get("ltp", 0) - ce_md.get("close_price", 0),

#             # PE DATA
#             "PE_Delta": pe_g.get("delta", 0),
#             "PE_IV": pe_g.get("iv", 0),
#             "PE_COI": (pe_md.get("oi", 0) - pe_md.get("prev_oi", 0)) / lot_size,
#             "PE_OI": pe_md.get("oi", 0) / lot_size,
#             "PE_Volume": pe_md.get("volume", 0) / lot_size,
#             "PE_LTP": pe_md.get("ltp", 0),
#             "PE_CLTP": pe_md.get("ltp", 0) - pe_md.get("close_price", 0),
#         })

#     df = pd.DataFrame(rows)
    
#     # Empty columns jo baad mein calculate honge
#     other_cols = ["CE_RANGE", "CE_COI_percent", "CE_OI_percent", "CE_Volume_percent", 
#                   "Reversl_Ce", "Reversl_Pe", "PE_Volume_percent", "PE_OI_percent", 
#                   "PE_COI_percent", "PE_RANGE"]
    
#     for col in other_cols:
#         df[col] = 0.0

#     df = df.sort_values(by="Strike_Price").reset_index(drop=True)
#     return df

# def calculate_data(symbol, expiry_Date):
#     # 1. Option chain fetch karein
#     response_data = get_option_chain(symbol, expiry_Date)
#     if not response_data or 'data' not in response_data:
#         return None

#     # Spot Price nikalne ka safe tarika
#     try:
#         spot_price = response_data['data'][0].get('underlying_spot_price', 0)
#     except (IndexError, KeyError):
#         spot_price = 0

#     # 2. DataFrame mein convert karein
#     df = data_to_df(symbol, expiry_Date)
#     if df is None or df.empty:
#         return None

#     # Column names ko pehle hi underscore mein badal dete hain 
#     # taaki calculation ke waqt confusion na ho
#     df.columns = df.columns.str.replace(" ", "_")

#     # 3. Reversal Calculations (Vectorized approach)
#     # shift(-1) niche wali row se data leta hai, shift(1) upar wali se
#     df["Reversl_Ce"] = ((df["PE_LTP"] - df["CE_LTP"].shift(-1)) + spot_price).round(2)
#     df["Reversl_Pe"] = ((df["PE_LTP"].shift(1) - df["CE_LTP"]) + spot_price).round(2)

#     # 4. Range aur Percentage Calculations
#     ce_oi = df["CE_OI"].replace(0, np.nan)
#     pe_oi = df["PE_OI"].replace(0, np.nan)

#     df["CE_RANGE"] = ((np.maximum(ce_oi - pe_oi, 0) / ce_oi) * 100).round(2).fillna(0)
#     df["PE_RANGE"] = ((np.maximum(pe_oi - ce_oi, 0) / pe_oi) * 100).round(2).fillna(0)

#     df["Spot_Price"] = spot_price

#     # Percentage helper function
#     def pct(col):
#         maxv = col.max()
#         return ((col / maxv * 100).round(2)) if maxv > 0 else 0.00

#     # Saare percentage columns update karein
#     df["CE_OI_percent"] = pct(df["CE_OI"])
#     df["PE_OI_percent"] = pct(df["PE_OI"])
#     df["CE_Volume_percent"] = pct(df["CE_Volume"])
#     df["PE_Volume_percent"] = pct(df["PE_Volume"])
#     df["CE_COI_percent"] = pct(df["CE_COI"])
#     df["PE_COI_percent"] = pct(df["PE_COI"])

#     # Final cleanup: NaN values ko 0 kar dein taaki Database reject na kare
#     df = df.fillna(0)

#     return df

# def sync_option_chain_to_db(request):
#     """
#     Ye view function API se data lekar database mein save karta hai.
#     """
#     symbol = "NIFTY"
#     expiry = "2026-02-10" # Aap ise dynamic bhi bana sakte hain
    
#     # 1. Data Hasil Karein (Aapka calculate_data function call ho raha hai)
#     df = calculate_data(symbol, expiry)
    
#     if df is None or df.empty:
#         return HttpResponse("Data fetch nahi ho paya ya khali hai. Token check karein.", status=500)

#     # 2. Database mein purana data clear karna (Optional)
#     # Agar aap chahte hain ki sirf latest data rahe toh niche wali line uncomment karein:
#     # OptionChain.objects.all().delete()

#     # 3. DataFrame Rows ko Model Objects mein badlein
#     option_entries = []
    
#     for _, row in df.iterrows():
#         # String Time ko Python datetime mein badleinge
#         if isinstance(row['Time'], str):
#             row_time = datetime.strptime(row['Time'], '%d/%m/%Y %H:%M:%S')
#         else:
#             row_time = row['Time']

#         obj = OptionChain(
#             Time=row_time,
#             Symbol=row['Symbol'],
#             expry_date=row['expry_date'],
#             Strike_Price=row['Strike_Price'],
#             Spot_Price=row['Spot_Price'],
            
#             # CE Data
#             CE_Delta=row.get('CE_Delta'),
#             CE_RANGE=row.get('CE_RANGE'),
#             CE_IV=row.get('CE_IV'),
#             CE_COI_percent=row.get('CE_COI_percent'),
#             CE_COI=row.get('CE_COI'),
#             CE_OI_percent=row.get('CE_OI_percent'),
#             CE_OI=row.get('CE_OI'),
#             CE_Volume_percent=row.get('CE_Volume_percent'),
#             CE_Volume=row.get('CE_Volume'),
#             CE_CLTP=row.get('CE_CLTP'),
#             CE_LTP=row.get('CE_LTP'),
#             Reversl_Ce=row.get('Reversl_Ce'),

#             # PE Data
#             Reversl_Pe=row.get('Reversl_Pe'),
#             PE_LTP=row.get('PE_LTP'),
#             PE_CLTP=row.get('PE_CLTP'),
#             PE_Volume=row.get('PE_Volume'),
#             PE_Volume_percent=row.get('PE_Volume_percent'),
#             PE_OI=row.get('PE_OI'),
#             PE_OI_percent=row.get('PE_OI_percent'),
#             PE_COI=row.get('PE_COI'),
#             PE_COI_percent=row.get('PE_COI_percent'),
#             PE_IV=row.get('PE_IV'),
#             PE_RANGE=row.get('PE_RANGE'),
#             PE_Delta=row.get('PE_Delta'),
#         )
#         option_entries.append(obj)

#     # 4. Bulk Create (Tezi se save karne ke liye)
#     try:
#         OptionChain.objects.bulk_create(option_entries)
#         return HttpResponse(f"Shabash! {len(option_entries)} rows successfully save ho gayi hain.")
#     except Exception as e:
#         return HttpResponse(f"Database Error: {str(e)}", status=500)

# ==================================================================================

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

# @never_cache  # यह ब्राउज़र को पुराना पेज दिखाने से रोकेगा
def dashboard(request):
    # 'get_or_create' का उपयोग करें ताकि अगर रिकॉर्ड न हो तो बन जाए
    nifty_obj, _ = SyncControl.objects.get_or_create(name="nifty_loop")
    others_obj, _ = SyncControl.objects.get_or_create(name="others_loop")
    
    # बाकी डेटा फेच करें
    data = OptionChain.objects.filter(Symbol="NIFTY").order_by('-Time')[:50]
    
    context = {
        'data': data,
        'nifty_active': nifty_obj.is_active,  # यहाँ से HTML को वैल्यू मिलेगी
        'others_active': others_obj.is_active,
        'spot': data[0].Spot_Price if data else 0,
        'latest_time': data[0].Time if data else None,
    }
    return render(request, 'dashboard.html', context)

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

from django.db.models import Q
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
    symbol = request.GET.get('symbol', 'NIFTY').upper()
    
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