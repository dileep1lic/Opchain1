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
# def option_chain_dashboard1(request):
#     # 1. Sabse latest entry ka time nikalna
#     latest_entry = OptionChain.objects.order_by('-Time').first()

#     if not latest_entry:
#         return render(request, 'mystock/dashboard.html', {'data': [], 'latest_time': None})

#     # 2. Latest Time aur Spot Price
#     latest_time = latest_entry.Time
#     spot_price = latest_entry.Spot_Price
#     expiry_date = latest_entry.Expiry_Date

#     # 3. Time Buffer Logic (Taaki us second ki saari strikes mil jayein)
#     all_data = list(
#         OptionChain.objects.filter(
#             Time__gte=latest_time - timedelta(seconds=1),
#             Time__lte=latest_time + timedelta(seconds=1)
#         ).order_by('Strike_Price')
#     )
    
#     # =============== UPDATE LOGIC: 1H Volume Change & Percentage ===============
#     # 1. पिछले डेटा की तलाश (1 घंटे के दायरे में सबसे पुराना उपलब्ध डेटा)
#     time_limit = latest_time - timedelta(hours=1)
    
#     # 1 घंटे पहले और अभी के बीच का सबसे पुराना डेटा उठाएं
#     closest_past_entry = OptionChain.objects.filter(
#         Time__gte=time_limit, 
#         Time__lt=latest_time
#     ).order_by('Time').first()

#     past_vol_map = {}
#     has_past_data = False

#     if closest_past_entry:
#         has_past_data = True
#         past_time = closest_past_entry.Time
#         # उस समय की पूरी चेन निकालें
#         past_chain = OptionChain.objects.filter(
#             Time__gte=past_time - timedelta(seconds=2),
#             Time__lte=past_time + timedelta(seconds=2)
#         )
#         for p_row in past_chain:
#             past_vol_map[p_row.Strike_Price] = {
#                 'ce_vol': p_row.CE_Volume or 0,
#                 'pe_vol': p_row.PE_Volume or 0
#             }

#     max_ce_chg = 0
#     max_pe_chg = 0

#     # 2. डेटा कैलकुलेशन
#     for row in all_data:
#         past_vols = past_vol_map.get(row.Strike_Price)
        
#         if has_past_data and past_vols:
#             row.ce_vol_1h_chg = (row.CE_Volume or 0) - past_vols['ce_vol']
#             row.pe_vol_1h_chg = (row.PE_Volume or 0) - past_vols['pe_vol']
#         else:
#             row.ce_vol_1h_chg = None
#             row.pe_vol_1h_chg = None
            
#         if row.ce_vol_1h_chg is not None and row.ce_vol_1h_chg > max_ce_chg:
#             max_ce_chg = row.ce_vol_1h_chg
#         if row.pe_vol_1h_chg is not None and row.pe_vol_1h_chg > max_pe_chg:
#             max_pe_chg = row.pe_vol_1h_chg

#     # 3. Percentage कैलकुलेशन
#     for row in all_data:
#         if max_ce_chg > 0 and row.ce_vol_1h_chg is not None and row.ce_vol_1h_chg > 0:
#             row.ce_vol_1h_chg_pct = (row.ce_vol_1h_chg / max_ce_chg) * 100
#         else:
#             row.ce_vol_1h_chg_pct = 0
            
#         if max_pe_chg > 0 and row.pe_vol_1h_chg is not None and row.pe_vol_1h_chg > 0:
#             row.pe_vol_1h_chg_pct = (row.pe_vol_1h_chg / max_pe_chg) * 100
#         else:
#             row.pe_vol_1h_chg_pct = 0

#     # =============== NAYA LOGIC: 1H VOL CHG RANKING (Green/Red/Yellow) ===============
#     # सभी मैट्रिक्स को एक ही लिस्ट में डाल दें
#     all_metrics = [
#         'ce_vol_1h_chg_pct', 'pe_vol_1h_chg_pct', # 1H Volume Change Metrics
#         'CE_OI_percent', 'CE_Volume_percent', 'CE_COI_percent', # CE General Metrics
#         'PE_OI_percent', 'PE_Volume_percent', 'PE_COI_percent'  # PE General Metrics
#     ]

#     for metric in all_metrics:
#         # सुरक्षित getattr का उपयोग
#         ranked = sorted(all_data, key=lambda x: getattr(x, metric, 0) or 0, reverse=True)
        
#         # base_class बनाने के लिए: यह '_pct' या '_percent' जो भी मिलेगा उसे '_class' कर देगा
#         base_class = metric.replace('_pct', '_class').replace('_percent', '_class')
        
#         # रैंक 1 के लिए (तभी हरा होगा जब वैल्यू 0 से ज्यादा हो)
#         if len(ranked) > 0:
#             val_1st = getattr(ranked[0], metric, 0) or 0
#             if val_1st > 0:
#                 setattr(ranked[0], base_class, "bg-green")
                
#         # रैंक 2 के लिए (लाल या पीला)
#         if len(ranked) > 1:
#             val_2nd = getattr(ranked[1], metric, 0) or 0
#             if val_2nd >= 75: 
#                 setattr(ranked[1], base_class, "bg-red")
#             elif 65 <= val_2nd < 75: 
#                 setattr(ranked[1], base_class, "bg-yellow")
                
#         # रैंक 3 के लिए (स्वतंत्र रूप से चेक करेगा)
#         if len(ranked) > 2:
#             val_3rd = getattr(ranked[2], metric, 0) or 0
#             if val_3rd >= 65: 
#                 setattr(ranked[2], base_class, "bg-yellow")

#     # 5. WINDOW FILTERING (±15 Strikes)
#     if all_data:
#         # Spot के सबसे पास वाली index निकालना
#         closest_idx = min(range(len(all_data)), key=lambda i: abs(all_data[i].Strike_Price - spot_price))
        
#         start = max(0, closest_idx - 15)
#         end = min(len(all_data), closest_idx + 16)
#         display_data = all_data[start:end]

#         # 6. SINGLE SPOT LINE LOGIC (For Dashboard Divider)
#         # पहली strike जो spot price से बड़ी है, उस पर marker लगाओ
#         for row in display_data:
#             if row.Strike_Price > spot_price:
#                 row.is_spot_divider = True
#                 break 
#     else:
#         display_data = []

#     return render(request, 'mystock/dashboard.html', {
#         'data': display_data,
#         'latest_time': latest_time,
#         'spot': spot_price,
#         'expiry_date': expiry_date
#     })
#  # अगर डेटा न हो तो खाली स्ट्रिंग भेजने के लिए

from django.shortcuts import render
from django.http import HttpResponse
from django.db.models import Sum
from datetime import timedelta

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


# def table_update_api1(request):
#     latest_entry = OptionChain.objects.order_by('-Time').first()

#     # अगर डेटाबेस खाली है, तो खाली रिस्पॉन्स भेजें ताकि JS एरर न दे
#     if not latest_entry:
#         return HttpResponse("") 

#     latest_time = latest_entry.Time
#     spot_price = latest_entry.Spot_Price
#     expiry_date = latest_entry.Expiry_Date
#     # टोटल्स के लिए वेरिएबल्स
#     total_ce_oi = 0
#     total_pe_oi = 0
#     total_ce_coi = 0
#     total_pe_coi = 0

#     all_data = list(
#         OptionChain.objects.filter(
#             Time__gte=latest_time - timedelta(seconds=1),
#             Time__lte=latest_time + timedelta(seconds=1)
#         ).order_by('Strike_Price')
#     )
#     # =============== UPDATE LOGIC: 1H Volume Change & Percentage ===============
#  # 1. पिछले डेटा की तलाश (1 घंटे के दायरे में सबसे पुराना उपलब्ध डेटा)
#     time_limit = latest_time - timedelta(hours=1)
    
#     # 1 घंटे पहले और अभी के बीच का सबसे पुराना डेटा उठाएं
#     closest_past_entry = OptionChain.objects.filter(
#         Time__gte=time_limit, 
#         Time__lt=latest_time
#     ).order_by('Time').first() # .first() यहाँ सबसे पुराना (Oldest) डेटा देगा

#     past_vol_map = {}
#     has_past_data = False

#     if closest_past_entry:
#         has_past_data = True
#         past_time = closest_past_entry.Time
#         # उस समय की पूरी चेन निकालें
#         past_chain = OptionChain.objects.filter(
#             Time__gte=past_time - timedelta(seconds=2),
#             Time__lte=past_time + timedelta(seconds=2)
#         )
#         for p_row in past_chain:
#             past_vol_map[p_row.Strike_Price] = {
#                 'ce_vol': p_row.CE_Volume or 0,
#                 'pe_vol': p_row.PE_Volume or 0
#             }

#     max_ce_chg = 0
#     max_pe_chg = 0

#     # 2. डेटा कैलकुलेशन
#     for row in all_data:
#         past_vols = past_vol_map.get(row.Strike_Price)
        
#         if has_past_data and past_vols:
#             row.ce_vol_1h_chg = (row.CE_Volume or 0) - past_vols['ce_vol']
#             row.pe_vol_1h_chg = (row.PE_Volume or 0) - past_vols['pe_vol']
#         else:
#             # अगर कोई पिछला डेटा उपलब्ध नहीं है, तो 0 के बजाय None रखें
#             row.ce_vol_1h_chg = None
#             row.pe_vol_1h_chg = None
            
#         if row.ce_vol_1h_chg is not None and row.ce_vol_1h_chg > max_ce_chg:
#             max_ce_chg = row.ce_vol_1h_chg
#         if row.pe_vol_1h_chg is not None and row.pe_vol_1h_chg > max_pe_chg:
#             max_pe_chg = row.pe_vol_1h_chg

#     # 3. Percentage कैलकुलेशन (सिर्फ तभी जब Change उपलब्ध हो)
#     for row in all_data:
#         if max_ce_chg > 0 and row.ce_vol_1h_chg is not None and row.ce_vol_1h_chg > 0:
#             row.ce_vol_1h_chg_pct = (row.ce_vol_1h_chg / max_ce_chg) * 100
#         else:
#             row.ce_vol_1h_chg_pct = 0
            
#         if max_pe_chg > 0 and row.pe_vol_1h_chg is not None and row.pe_vol_1h_chg > 0:
#             row.pe_vol_1h_chg_pct = (row.pe_vol_1h_chg / max_pe_chg) * 100
#         else:
#             row.pe_vol_1h_chg_pct = 0
            
#    # =============== TOP 3 RANKING LOGIC (Combined with 75% & 65% limits) ===============
    
#     # सभी मैट्रिक्स को एक ही लिस्ट में डाल दें
#     all_metrics = [
#         'ce_vol_1h_chg_pct', 'pe_vol_1h_chg_pct', # 1H Volume Change Metrics
#         'CE_OI_percent', 'CE_Volume_percent', 'CE_COI_percent', # CE General Metrics
#         'PE_OI_percent', 'PE_Volume_percent', 'PE_COI_percent'  # PE General Metrics
#     ]

#     for metric in all_metrics:
#         # वैल्यू के आधार पर सॉर्ट करें (सबसे बड़ी वैल्यू सबसे ऊपर)
#         ranked = sorted(all_data, key=lambda x: getattr(x, metric, 0) or 0, reverse=True)
        
#         # base_class बनाने के लिए: '_pct' या '_percent' जो भी मिलेगा उसे '_class' कर देगा
#         base_class = metric.replace('_pct', '_class').replace('_percent', '_class')
        
#         # 🟢 रैंक 1 के लिए (तभी हरा होगा जब वैल्यू 0 से ज्यादा हो)
#         if len(ranked) > 0:
#             val_1st = getattr(ranked[0], metric, 0) or 0
#             if val_1st > 0:
#                 setattr(ranked[0], base_class, "bg-green")
                
#         # 🔴/🟡 रैंक 2 के लिए
#         if len(ranked) > 1:
#             val_2nd = getattr(ranked[1], metric, 0) or 0
#             if val_2nd >= 75: 
#                 setattr(ranked[1], base_class, "bg-red")     # 75 या उससे ज्यादा पर लाल
#             elif 65 <= val_2nd < 75: 
#                 setattr(ranked[1], base_class, "bg-yellow")  # 65 से 75 के बीच पीला
                
#         # 🟡 रैंक 3 के लिए
#         if len(ranked) > 2:
#             val_3rd = getattr(ranked[2], metric, 0) or 0
#             if val_3rd >= 65: 
#                 setattr(ranked[2], base_class, "bg-yellow")  # 65 या उससे ज्यादा पर पीला
#     # 7. TOTAL OI AND COI CALCULATION (पूरे डेटा का टोटल)
#     if all_data:
#         total_ce_oi = sum(row.CE_OI or 0 for row in all_data)
#         total_pe_oi = sum(row.PE_OI or 0 for row in all_data)
#         total_ce_coi = sum(row.CE_COI or 0 for row in all_data)
#         total_pe_coi = sum(row.PE_COI or 0 for row in all_data)

#     # Filtering & Divider logic (बिल्कुल सही है)
#     if all_data:
#         closest_idx = min(range(len(all_data)), key=lambda i: abs(all_data[i].Strike_Price - spot_price))
#         display_data = all_data[max(0, closest_idx - 15) : min(len(all_data), closest_idx + 16)]
#         for row in display_data:
#             if row.Strike_Price > spot_price:
#                 row.is_spot_divider = True
#                 break 
#     else:
#         display_data = []

#     context = {
#         'data': display_data,
#         'latest_time': latest_time,
#         'spot': spot_price,
#         'expiry_date': expiry_date,
#         # कॉन्टेक्स्ट में पूरे डेटा का टोटल पास कर रहे हैं
#         'total_ce_oi': total_ce_oi,
#         'total_pe_oi': total_pe_oi,
#         'total_ce_coi': total_ce_coi,
#         'total_pe_coi': total_pe_coi,
#     }
    
#     # यह table_partial.html में सिर्फ <tbody> और उसकी Rows होनी चाहिए
#     return render(request, 'mystock/table_partial.html', context)

from django.db.models import Sum
from django.shortcuts import render
from django.http import HttpResponse
from datetime import timedelta

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



def calculate_final_sr1(oi_strike, oi_status, vol_strike, vol_status, option_type):
    """
    यह फंक्शन आपके नियमों के अनुसार फाइनल Support या Resistance और उसका Status निकाल कर देगा।
    """
    
    if not oi_strike or not vol_strike:
        return None, None

    # ==========================================
    # नियम 1: अगर OI और Volume दोनों एक ही स्ट्राइक पर हैं
    # ==========================================
    if oi_strike == vol_strike:
        final_strike = oi_strike
        
        if option_type == "CE":
            # --- RESISTANCE (CE) का नियम ---
            # अगर एक भी WTB है, तो ओवरऑल WTB
            if oi_status == "WTB" or vol_status == "WTB":
                final_status = "BOTH WTB"
            # अगर दोनों WTT हैं, तभी ओवरऑल WTT
            elif oi_status == "WTT" and vol_status == "WTT":
                final_status = "BOTH WTT"
            else:
                final_status = "BOTH STRONG"
                
        elif option_type == "PE":
            # --- SUPPORT (PE) का नियम ---
            # अगर एक भी WTT है, तो ओवरऑल WTT
            if oi_status == "WTT" or vol_status == "WTT":
                final_status = "BOTH WTT"
            # अगर दोनों WTB हैं, तभी ओवरऑल WTB
            elif oi_status == "WTB" and vol_status == "WTB":
                final_status = "BOTH WTB"
            else:
                final_status = "BOTH STRONG"

    # ==========================================
    # नियम 2: अगर OI और Volume अलग-अलग स्ट्राइक पर हैं
    # ==========================================
    else:
        if option_type == "CE":
            # Resistance (CE): दोनों में जो छोटी स्ट्राइक है, उसका माना जाएगा
            if oi_strike < vol_strike:
                final_strike = oi_strike
                final_status = "OI " + oi_status  # 'OI ' प्रिफिक्स जोड़ा गया
            else:
                final_strike = vol_strike
                final_status = "Vol " + vol_status # 'Vol ' प्रिफिक्स जोड़ा गया
                
        elif option_type == "PE":
            # Support (PE): दोनों में जो बड़ी स्ट्राइक (स्पॉट के ज्यादा पास) है, उसका माना जाएगा
            if oi_strike > vol_strike:
                final_strike = oi_strike
                final_status = "OI " + oi_status  # 'OI ' प्रिफिक्स जोड़ा गया
            else:
                final_strike = vol_strike
                final_status = "Vol " + vol_status # 'Vol ' प्रिफिक्स जोड़ा गया

    return final_strike, final_status

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

# views.py में जोड़ें
from django.shortcuts import render
from .models import LiveSRData
# (अपने calculate_final_sr फंक्शन को भी यहाँ रखें या इम्पोर्ट करें)

from django.utils import timezone

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




