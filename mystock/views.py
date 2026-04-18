import requests
import time
from datetime import datetime, time, timedelta, date, time as dt_time
from django.shortcuts import render
from requests.exceptions import SSLError, ConnectionError, Timeout
from .models import OptionChain, SupportResistance, SyncControl, TempOptionChain, LiveSRData, BotSettings
from django.utils import timezone
from django.db.models import OuterRef, Subquery, Q, Sum, F
from django.views.decorators.cache import never_cache
from django.http import HttpResponse, JsonResponse
from django.views.decorators.cache import cache_page
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from .management.commands.async_live import get_instrument_from_db, update_instrument_store_bulk 
from .symbol import symbols as ALL_SYMBOLS
from django.views.decorators.clickjacking import xframe_options_exempt
import pytz
from django.utils.timezone import localtime
import json
from django.http import JsonResponse
from django.db.models.functions import Abs
from asgiref.sync import async_to_sync
from .trade_logic import get_master_levels



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

# ── 1. Admin Panel Page ─────────────────────────────────────
@login_required
def admin_panel_view(request):
    """Admin control panel — सिर्फ page render करता है, data JS से आता है।"""
    return render(request, 'mystock/admin_panel.html')


# ── 2. Admin Status API ─────────────────────────────────────
def admin_status_api1(request):
    """
    सभी loops का status + आज की trade stats एक call में देता है।
    JS हर 10 सेकंड में यहाँ poll करता है।
    """
    from django.db.models import Sum, Count, Q

    # ── Loop Statuses ──────────────────────────────────────
    loop_names = ['nifty_loop', 'others_loop', 'bot_loop']
    loops = {}
    for name in loop_names:
        ctrl, _ = SyncControl.objects.get_or_create(name=name)
        loops[name] = ctrl.is_active

    # ── Today's Trade Stats ────────────────────────────────
    today = timezone.now().date()
    day_start = timezone.make_aware(datetime.combine(today, dt_time.min))
    day_end   = timezone.make_aware(datetime.combine(today, dt_time.max))

    stats_qs = PaperTrade.objects.filter(
        trade_date=today
    ).exclude(result='SKIPPED').aggregate(
        total  = Count('id'),
        wins   = Count('id', filter=Q(result='TARGET')),
        losses = Count('id', filter=Q(result='SL')),
        pnl    = Sum('pnl'),
    )

    # ── Current Spot (NIFTY) ───────────────────────────────
    latest_oc = OptionChain.objects.filter(
        Symbol='NIFTY', Time__gte=day_start, Time__lte=day_end
    ).only('Spot_Price').order_by('-Time').first()

    settings, _ = BotSettings.objects.get_or_create(id=1)

    return JsonResponse({
        'loops': loops,
        'stats': {
            'total' : stats_qs['total']  or 0,
            'wins'  : stats_qs['wins']   or 0,
            'losses': stats_qs['losses'] or 0,
            'pnl'   : round(stats_qs['pnl'] or 0, 2),
            'spot'  : latest_oc.Spot_Price if latest_oc else None,
        },
        'settings': {
            'target': settings.default_target,
            'sl': settings.default_sl,
            'buffer': settings.reversal_buffer
        }
    })


def admin_status_api(request):
    """सभी loops का status + आज की trade stats + Bot Settings"""
    from django.db.models import Sum, Count, Q
    
    # ... (आपका पुराना loop status और stats वाला कोड यहाँ रहेगा) ...
    loop_names = ['nifty_loop', 'others_loop', 'bot_loop']
    loops = {}
    for name in loop_names:
        ctrl, _ = SyncControl.objects.get_or_create(name=name)
        loops[name] = ctrl.is_active

    today = timezone.now().date()
    stats_qs = PaperTrade.objects.filter(trade_date=today).exclude(result='SKIPPED').aggregate(
            total=Count('id'), 
            # TARGET हिट हो या MANUAL_EXIT में प्रॉफिट हो -> Win
            wins=Count('id', filter=Q(result='TARGET') | (Q(result='MANUAL_EXIT', pnl__gt=0))),
            # SL हिट हो या MANUAL_EXIT में लॉस हो -> Loss
            losses=Count('id', filter=Q(result='SL') | (Q(result='MANUAL_EXIT', pnl__lt=0))),
            pnl=Sum('pnl')
        )
    latest_oc = OptionChain.objects.filter(Symbol='NIFTY', Time__date=today).order_by('-Time').first()

    # 👇 नया: डेटाबेस से लाइव सेटिंग्स निकालें
    settings, _ = BotSettings.objects.get_or_create(id=1)

    return JsonResponse({
        'loops': loops,
        'stats': {
            'total': stats_qs['total'] or 0,
            'wins': stats_qs['wins'] or 0,
            'losses': stats_qs['losses'] or 0,
            'pnl': round(stats_qs['pnl'] or 0, 2),
            'spot': latest_oc.Spot_Price if latest_oc else None,
        },
        'settings': {
            'target': settings.default_target,
            'sl': settings.default_sl,
            'buffer': settings.reversal_buffer
        }
    })

# 2. यह नया फंक्शन सबसे नीचे जोड़ दें:
@csrf_exempt
def update_bot_settings_api(request):
    """एडमिन पैनल से सेटिंग्स अपडेट करने के लिए"""
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            settings, _ = BotSettings.objects.get_or_create(id=1)
            
            if 'target' in data: settings.default_target = float(data['target'])
            if 'sl' in data: settings.default_sl = float(data['sl'])
            if 'buffer' in data: settings.reversal_buffer = float(data['buffer'])
            
            settings.save()
            return JsonResponse({'status': 'success', 'msg': 'Settings Updated Successfully!'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'msg': str(e)})
    return JsonResponse({'status': 'invalid method'})

@csrf_exempt
def close_all_open_trades_api(request):
    """एडमिन पैनल से इमरजेंसी में सभी ओपन ट्रेड्स क्लोज करने के लिए"""
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            symbol = data.get('symbol', 'NIFTY').upper()
            
            # करंट स्पॉट प्राइस निकालें
            latest_oc = OptionChain.objects.filter(Symbol__iexact=symbol).order_by('-Time').first()
            if not latest_oc or not latest_oc.Spot_Price:
                return JsonResponse({'status': 'error', 'msg': 'Spot Price नहीं मिला!'})
                
            spot = float(latest_oc.Spot_Price)
            
            # सिर्फ OPEN ट्रेड्स निकालें
            open_trades = PaperTrade.objects.filter(symbol=symbol, result="OPEN")
            count = open_trades.count()
            
            if count == 0:
                return JsonResponse({'status': 'error', 'msg': 'कोई Open Trade नहीं है!'})
            
            # सभी ट्रेड्स का PnL कैलकुलेट करके क्लोज करें
            for trade in open_trades:
                entry = float(trade.entry_spot)
                if trade.trade_type == 'CALL':
                    actual_pnl = spot - entry
                else:
                    actual_pnl = entry - spot
                    
                trade.exit_spot = spot
                trade.exit_time = timezone.now()
                trade.result = "MANUAL_EXIT"
                trade.pnl = round(actual_pnl, 2)
                trade.save()
                
            return JsonResponse({'status': 'success', 'msg': f'{count} Open Trades Closed at {spot}'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'msg': str(e)})
    return JsonResponse({'status': 'invalid method'})

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





import requests
from datetime import date, timedelta

from django.shortcuts import render
from django.http import JsonResponse
from .credentials import access_token
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

from django.core.cache import cache

def get_reversal_lines1(symbol: str, from_date: str, to_date: str):
    # --- OPTIMIZATION 1: Cache (मेमोरी में सेव करना) ---
    cache_key = f"rev_lines_history_{symbol}_{from_date}_{to_date}"
    cached_data = cache.get(cache_key)
    
    if cached_data:
        return cached_data

    try:
        import math
        import re as _re

        sr = LiveSRData.objects.filter(
            Symbol__iexact=symbol,
            Time__date__gte=from_date,
            Time__date__lte=to_date,
        ).order_by('-Time').first()

        if not sr or not sr.resistance_strike or not sr.supprt_strike:
            return []

        low  = min(sr.supprt_strike, sr.resistance_strike)
        high = max(sr.resistance_strike, sr.supprt_strike)

        # WTT/WTB काफी दूर हो सकते हैं, इसलिए EXPAND को 1000 कर दिया है 
        # ताकि दूर वाली स्ट्राइक्स का डेटा भी आसानी से आ जाए
        EXPAND = 1000 
        
        # --- OPTIMIZATION 2: .values() का इस्तेमाल (10x Faster) ---
        oc_qs = OptionChain.objects.filter(
            Symbol__iexact=symbol,
            Time__date__gte=from_date,
            Time__date__lte=to_date,
            Strike_Price__gte=low  - EXPAND,
            Strike_Price__lte=high + EXPAND,
        ).values('Time', 'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe').order_by('Time')

        if not oc_qs:
            return []

        history_data = {}
        latest_rows = {}
        
        def is_valid(val):
            return val is not None and not math.isnan(val) and not math.isinf(val)

        for row in oc_qs:
            strike = row['Strike_Price']
            if strike not in history_data:
                history_data[strike] = {'CE': [], 'PE': []}

            time_str = row['Time'].isoformat()
            
            if is_valid(row['Reversl_Ce']):
                history_data[strike]['CE'].append({'time': time_str, 'value': float(row['Reversl_Ce'])})
            if is_valid(row['Reversl_Pe']):
                history_data[strike]['PE'].append({'time': time_str, 'value': float(row['Reversl_Pe'])})

            latest_rows[strike] = row

        rows = list(latest_rows.values())
        rows.sort(key=lambda r: r['Strike_Price'])

        # Step साइज़ निकालना (जैसे निफ्टी में 50, बैंक निफ्टी में 100)
        all_strikes = sorted(set(r['Strike_Price'] for r in rows))
        step = 50
        if len(all_strikes) >= 2:
            diffs = [all_strikes[i+1] - all_strikes[i] for i in range(len(all_strikes)-1)]
            step = min(diffs) if min(diffs) > 0 else 50

        # ==============================================================
        # 🚀 आपके नए नियम (NEW RULES IMPLEMENTATION)
        # ==============================================================
        
        # ─── 1. CALL SIDE (RESISTANCE) ───
        res_status = str(sr.resistance_status).upper() if sr.resistance_status else ""
        res_base = sr.resistance_strike
        
        # Target Strike खोजना (जहाँ WTT/WTB जा रहा है)
        m_res = _re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
        res_target = float(m_res.group(1)) if m_res else res_base

        effective_res_strike = res_base # Default
        
        if "SHIFTED WTT" in res_status:
            effective_res_strike = res_base           # Resistance स्ट्राइक का रिवर्सल
        elif "SHIFTED WTB" in res_status:
            effective_res_strike = res_base + step    # Resistance स्ट्राइक से बड़ी स्ट्राइक का रिवर्सल
        elif "WTT" in res_status:
            effective_res_strike = res_target + step  # WTT स्ट्राइक से बड़ी स्ट्राइक का रिवर्सल
        elif "WTB" in res_status:
            effective_res_strike = res_target + step  # WTB स्ट्राइक से बड़ी स्ट्राइक का रिवर्सल
        elif "STRONG" in res_status: 
            effective_res_strike = res_base + step    # Strong/Both Strong: Resistance स्ट्राइक से बड़ी स्ट्राइक
        else:
            effective_res_strike = res_base + step    # सुरक्षा के लिए डिफ़ॉल्ट बड़ी स्ट्राइक


        # ─── 2. PUT SIDE (SUPPORT) ───
        sup_status = str(sr.supprt_status).upper() if sr.supprt_status else ""
        sup_base = sr.supprt_strike
        
        # Target Strike खोजना (जहाँ WTT/WTB जा रहा है)
        m_sup = _re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
        sup_target = float(m_sup.group(1)) if m_sup else sup_base

        effective_sup_strike = sup_base # Default
        
        if "SHIFTED WTT" in sup_status:
            effective_sup_strike = sup_base           # Support स्ट्राइक का रिवर्सल
        elif "SHIFTED WTB" in sup_status:
            effective_sup_strike = sup_base - step    # Support स्ट्राइक से छोटी स्ट्राइक का रिवर्सल
        elif "WTT" in sup_status:
            effective_sup_strike = sup_target - step  # WTT स्ट्राइक से छोटी स्ट्राइक का रिवर्सल
        elif "WTB" in sup_status:
            effective_sup_strike = sup_target - step  # WTB स्ट्राइक से छोटी स्ट्राइक का रिवर्सल
        elif "STRONG" in sup_status:
            effective_sup_strike = sup_base - step    # Strong/Both Strong: Support स्ट्राइक से छोटी स्ट्राइक
        else:
            effective_sup_strike = sup_base - step    # सुरक्षा के लिए डिफ़ॉल्ट छोटी स्ट्राइक

        # ==============================================================

        # बाउंड्री सेट करना (चार्ट में कहाँ से कहाँ तक की लाइनें दिखेंगी)
        ce_strikes_list = [effective_res_strike, res_base, res_target]
        pe_strikes_list = [effective_sup_strike, sup_base, sup_target]
        
        global_low = min(pe_strikes_list + ce_strikes_list) - step
        global_high = max(pe_strikes_list + ce_strikes_list) + step

        seen_ce = set()
        seen_pe = set()
        lines   = []

        for row in rows:
            strike = row['Strike_Price']
            spot = row['Spot_Price']

            if global_low <= strike <= global_high and is_valid(spot):
                
                is_bottom = (strike == effective_sup_strike)
                is_top = (strike == effective_res_strike)

                # ─── PUT SIDE (SUPPORT) ───
                # नया नियम: अगर यह मेन सपोर्ट है (is_bottom), तो मार्किट कहीं भी हो, यह ब्लू लाइन हमेशा दिखेगी।
                if is_valid(row['Reversl_Pe']):
                    if (row['Reversl_Pe'] < spot or is_bottom) and row['Reversl_Pe'] not in seen_pe:
                        seen_pe.add(row['Reversl_Pe'])
                        
                        line_color = "#00bfff" if is_bottom else "#3fb950" 
                        
                        lines.append({
                            "price":  float(row['Reversl_Pe']),
                            "strike": float(strike),
                            "type":   "PE",
                            "color":  line_color,
                            "width":  4 if is_bottom else 1,
                            "dash":   0,
                            "label":  f"S {strike:.0f}" if is_bottom else f"P {strike:.0f}",
                            "history": history_data[strike]['PE']
                        })

                # ─── CALL SIDE (RESISTANCE) ───
                # नया नियम: अगर यह मेन रेजिस्टेंस है (is_top), तो मार्किट कहीं भी हो, यह ऑरेंज लाइन हमेशा दिखेगी।
                if is_valid(row['Reversl_Ce']):
                    if (row['Reversl_Ce'] >= spot or is_top) and row['Reversl_Ce'] not in seen_ce:
                        seen_ce.add(row['Reversl_Ce'])
                        
                        line_color = "#ff8c00" if is_top else "#f85149"
                        
                        lines.append({
                            "price":  float(row['Reversl_Ce']),
                            "strike": float(strike),
                            "type":   "CE",
                            "color":  line_color,
                            "width":  4 if is_top else 1,
                            "dash":   0,
                            "label":  f"R {strike:.0f}" if is_top else f"CE {strike:.0f}",
                            "history": history_data[strike]['CE']
                        })

        lines.sort(key=lambda x: x["price"], reverse=True)
        
        # 60 सेकंड के लिए कैश सेव करें
        cache.set(cache_key, lines, timeout=60)
        
        return lines

    except Exception as e:
        print(f"Reversal lines error: {e}")
        return []
    
def get_reversal_lines(symbol: str, from_date: str, to_date: str):
    cache_key = f"rev_lines_{symbol}_{from_date}_{to_date}"
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    try:
        import math, re as _re
        from datetime import datetime, time as dt_time

        day_start = timezone.make_aware(datetime.combine(
            datetime.strptime(from_date, '%Y-%m-%d').date(), dt_time.min))
        day_end   = timezone.make_aware(datetime.combine(
            datetime.strptime(to_date,   '%Y-%m-%d').date(), dt_time.max))

        sr = LiveSRData.objects.filter(
            Symbol=symbol,
            Time__gte=day_start,
            Time__lte=day_end,
        ).order_by('-Time').first()

        if not sr or not sr.resistance_strike or not sr.supprt_strike:
            cache.set(cache_key, [], timeout=60)
            return []

        step = 100 if 'BANKNIFTY' in symbol or 'SENSEX' in symbol else 50

        # def effective_strike(status_raw, base, is_resistance):
        #     status = str(status_raw).upper() if status_raw else ""
        #     m = _re.search(r'(?:WTB|WTT)\s+(\d+)', status)
        #     target = float(m.group(1)) if m else base
        #     # if "SHIFTED WTT" in status: return base 
        #     # if "SHIFTED WTB" in status: return base + step if is_resistance else base - step
        #     # if "WTT" in status or "WTB" in status:
        #     #     return target + step if is_resistance else target - step
        #     # return base + step if is_resistance else base - step

        #     if is_resistance:
        #         # Resistance (CE) Side
        #         if "SHIFTED WTT" in status: return base + step
        #         if "SHIFTED WTB" in status: return base + step
        #         if "WTT" in status or "WTB" in status: return target + step
        #         return base + step
        #     else:
        #         # Support (PE) Side - आपकी शर्त यहाँ लागू होती है
        #         # यदि "Shifted WTT" है, तो base (Support Strike) से एक step नीचे की लाइन
        #         if "SHIFTED WTT" in status: 
        #             return base - step  # <--- यहाँ बदलाव किया गया है
                    
        #         if "SHIFTED WTB" in status: return base - step
        #         if "WTT" in status or "WTB" in status: return target - step
        #         return base - step

        
        # eff_res = effective_strike(sr.resistance_status, float(sr.resistance_strike), True)
        # eff_sup = effective_strike(sr.supprt_status,     float(sr.supprt_strike),     False)

        master_levels = get_master_levels(symbol, day_start.date())
        eff_res = master_levels["R"]["strike"]
        eff_sup = master_levels["S"]["strike"]
        
        # ✅ Original range logic restore — nearby strikes ke liye
        ce_strikes_list = [eff_res, float(sr.resistance_strike)]
        pe_strikes_list = [eff_sup, float(sr.supprt_strike)]
        global_low  = min(pe_strikes_list + ce_strikes_list) - step
        global_high = max(pe_strikes_list + ce_strikes_list) + step

        # ✅ Performance: .values() + date range + exact match
        oc_qs = OptionChain.objects.filter(
            Symbol=symbol,
            Time__gte=day_start,
            Time__lte=day_end,
            Strike_Price__gte=global_low,
            Strike_Price__lte=global_high,
        ).values(
            'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe'
        ).order_by('-Time')   # latest pehle

        # Har strike ki sirf latest row
        seen_strikes = {}
        spot_price   = None
        for row in oc_qs:
            s = row['Strike_Price']
            if s not in seen_strikes:
                seen_strikes[s] = row
            if spot_price is None and row['Spot_Price']:
                spot_price = row['Spot_Price']

        def valid(v):
            return v is not None and not math.isnan(v) and not math.isinf(v)

        seen_ce, seen_pe = set(), set()
        lines = []

        for strike, row in sorted(seen_strikes.items()):
            if not valid(row['Spot_Price']):
                continue
            spot = row['Spot_Price']
            is_top    = (strike == eff_res)
            is_bottom = (strike == eff_sup)

            # ✅ Original filter logic — spot ke upar/neeche wali lines
            if valid(row['Reversl_Ce']):
                if (row['Reversl_Ce'] >= spot or is_top) and row['Reversl_Ce'] not in seen_ce:
                    seen_ce.add(row['Reversl_Ce'])
                    lines.append({
                        "price":  float(row['Reversl_Ce']),
                        "strike": float(strike),
                        "type":   "CE",
                        "color":  "#ff8c00" if is_top else "#f85149",
                        "width":  4 if is_top else 1,
                        "dash":   0,
                        "label":  f"R {strike:.0f}" if is_top else f"CE {strike:.0f}",
                    })

            if valid(row['Reversl_Pe']):
                if (row['Reversl_Pe'] < spot or is_bottom) and row['Reversl_Pe'] not in seen_pe:
                    seen_pe.add(row['Reversl_Pe'])
                    lines.append({
                        "price":  float(row['Reversl_Pe']),
                        "strike": float(strike),
                        "type":   "PE",
                        "color":  "#00bfff" if is_bottom else "#3fb950",
                        "width":  4 if is_bottom else 1,
                        "dash":   0,
                        "label":  f"S {strike:.0f}" if is_bottom else f"P {strike:.0f}",
                    })

        lines.sort(key=lambda x: x["price"], reverse=True)
        cache.set(cache_key, lines, timeout=300)
        return lines

    except Exception as e:
        print(f"Reversal lines error: {e}")
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
@xframe_options_exempt
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
    # ✅ नया parameter — "0" आए तो reversal skip करो
    show_reversal = request.GET.get("reversal", "1") != "0"

    if not symbol:
        return JsonResponse({"error": "symbol parameter जरूरी है।"}, status=400)

    instrument_key = get_instrument_key(symbol)
    if not instrument_key:
        return JsonResponse({"error": f"'{symbol}' symbol DB में नहीं मिला।"}, status=404)

    result = fetch_candle_data(instrument_key, unit, interval, to_date, from_date)
    if not result["success"]:
        return JsonResponse({"error": result["error"]}, status=400)

    candles = parse_candles(result["data"])

    # ✅ reversal=0 हो तो get_reversal_lines() बिल्कुल नहीं चलेगा
    reversal_lines = get_reversal_lines(symbol, from_date, to_date) if show_reversal else []

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



from django.views.decorators.clickjacking import xframe_options_exempt

@xframe_options_exempt
def dashboard_chart_view(request):
    today     = date.today()
    symbol    = request.GET.get("symbol",    "NIFTY").strip().upper()
    unit      = request.GET.get("unit",      "minutes")
    interval  = request.GET.get("interval",  "5")
    from_date = request.GET.get("from_date", today.isoformat()) 
    to_date   = request.GET.get("to_date",   today.isoformat())  

    candles = []
    error = None
    instrument_key = get_instrument_key(symbol)

    if not instrument_key:
        error = f"'{symbol}' symbol DB में नहीं मिला।"
    else:
        result = fetch_candle_data(instrument_key, unit, interval, to_date, from_date)
        if not result["success"]:
            error = result["error"]
        else:
            candles = parse_candles(result["data"])

    reversal_lines = get_reversal_lines(symbol, from_date, to_date)

    context = {
        "candles":        candles,
        "reversal_lines": reversal_lines,
        "error":          error,
        "symbol":         symbol,
        "unit":           unit,
        "interval":       interval,
        "from_date":      from_date,
        "to_date":        to_date,
    }
    return render(request, "mystock/dashboard_chart.html", context)






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
#  NORMAL    → WTT/WTB found (p2nd exists) → SHIFTING  (emit "WTT/WTB p2nd")
#  NORMAL    → WTT/WTB found (no p2nd)     → NORMAL    (emit "WTT/WTB pS")
#  NORMAL    → STR/STRONG                  → NORMAL    (emit "strong pS")
#
#  SHIFTING  → pS == shift_strike, WTT/WTB → IN_SHIFTED (emit "Shifted WTT/WTB")
#  SHIFTING  → pS == shift_strike, STR     → NORMAL     (emit "Shifted strong")
#  SHIFTING  → pS != shift_strike, WTT/WTB, p2nd exists → SHIFTING (new shift)
#  SHIFTING  → pS != shift_strike, WTT/WTB, no p2nd     → NORMAL   (emit "WTT/WTB pS")
#  SHIFTING  → pS != shift_strike, STR                  → NORMAL   (emit "Shifted strong")
#
#  IN_SHIFTED → same strike, WTT/WTB, p2nd same   → IN_SHIFTED (emit "Shifted WTT/WTB")
#  IN_SHIFTED → same strike, WTT/WTB, p2nd changed → SHIFTING   (emit "WTT/WTB new_p2nd")
#  IN_SHIFTED → same strike, STR                   → NORMAL     (emit "strong pS")
#  IN_SHIFTED → strike changed, WTT/WTB, p2nd      → SHIFTING   (emit "WTT/WTB p2nd")
#  IN_SHIFTED → strike changed, WTT/WTB, no p2nd   → NORMAL     (emit "WTT/WTB pS")
#  IN_SHIFTED → strike changed, STR                → NORMAL     (emit "strong pS")
# ─────────────────────────────────────────────────────

def _fmt(v):
    """Float → int string if whole number, else float string"""
    if v is None:
        return "—"
    return str(int(v)) if v == int(v) else str(v)


class ResistanceCalculator:
    """
    CE side — lower strike = primary (closer resistance above spot)
    Rule:
      ce_high_oi_strike < ce_high_vol_strike  → OI is primary
      ce_high_oi_strike > ce_high_vol_strike  → Vol is primary
    Both-WTT threshold : दोनों WTT
    Any-WTB threshold  : कोई एक WTB (या दोनों)
    """

    def __init__(self): self.reset()

    def reset(self):
        self._shifting     = False
        self._shift_strike = None
        self._shift_wt     = None
        self._in_shifted   = False
        self._shifted_wt   = None
        self._prev_p2nd    = None
        self._prev_label   = None

    def calculate(self, row_dict):
        label, source = self._compute(row_dict)
        self._prev_label = label
        return label, source

    # ── Source label ────────────────────────────────
    def _src(self, ptype, pS):
        return f"Resistance ({ptype}){_fmt(pS)}"

    # ── Enter SHIFTING ──────────────────────────────
    def _do_shift(self, shift_to, wt, src):
        self._shifting     = True
        self._in_shifted   = False
        self._shift_strike = shift_to
        self._shift_wt     = wt
        self._prev_p2nd    = None
        return f"Resistance {wt} {_fmt(shift_to)}", src

    # ── Reset all states ────────────────────────────
    def _reset(self):
        self._shifting     = False
        self._shift_strike = None
        self._shift_wt     = None
        self._in_shifted   = False
        self._shifted_wt   = None
        self._prev_p2nd    = None

    # ── Core compute ────────────────────────────────
    def _compute(self, r):
        vs    = r.get("ce_high_vol_strike")
        os_   = r.get("ce_high_oi_strike")
        vStat = (r.get("ce_vol_status") or "").upper()
        oStat = (r.get("ce_oi_status")  or "").upper()

        # ════════════════════════════════════════
        # CASE 1: Same Strike (Both)
        # ════════════════════════════════════════
        if vs is not None and os_ is not None and vs == os_:
            pS     = vs
            src    = self._src("Both", pS)

            # दोनों WTT → WTT shift
            if vStat == WTT and oStat == WTT:
                target = r.get("ce_2nd_high_vol_strike") or r.get("ce_2nd_high_oi_strike")
                # FIX: IN_SHIFTED उसी primary strike (pS) पर → Shifted state preserve करो
                if target and self._in_shifted and self._shift_strike == pS:
                    self._shifted_wt = WTT
                    self._prev_p2nd = target
                    return f"Resistance Shifted WTT {_fmt(target)}", src
                self._reset()
                if target:
                    return self._do_shift(target, WTT, src)
                return f"Resistance WTT {_fmt(pS)}", src

            # कोई एक (या दोनों) WTB → WTB shift
            if vStat == WTB or oStat == WTB:
                if vStat == WTB and oStat == WTB:
                    target = r.get("ce_2nd_high_vol_strike") or r.get("ce_2nd_high_oi_strike")
                elif vStat == WTB:
                    target = r.get("ce_2nd_high_vol_strike")
                else:
                    target = r.get("ce_2nd_high_oi_strike")
                # FIX: IN_SHIFTED उसी primary strike (pS) पर → Shifted state preserve करो
                if target and self._in_shifted and self._shift_strike == pS:
                    self._shifted_wt = WTB
                    self._prev_p2nd = target
                    return f"Resistance Shifted WTB {_fmt(target)}", src
                self._reset()
                if target:
                    return self._do_shift(target, WTB, src)
                return f"Resistance WTB {_fmt(pS)}", src

            # STR / neutral
            self._reset()
            return "Resistance Both strong", src

        # ════════════════════════════════════════
        # CASE 2: Different Strikes
        # CE में lower strike = primary (closer to spot)
        # ════════════════════════════════════════
        if vs is not None and os_ is not None:
            if vs < os_:
                # Vol is lower → Vol primary
                pS, pStat, p2nd, pType = vs,  vStat, r.get("ce_2nd_high_vol_strike"), "Vol"
            else:
                # OI is lower → OI primary
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
                    # BUG 4 FIX: p2nd != prev_p2nd (None→X, X→None, X→Y सभी catch)
                    if p2nd != self._prev_p2nd:
                        self._in_shifted = False
                        if p2nd is not None:
                            # नई 2nd strike → fresh shift
                            return self._do_shift(p2nd, pStat, src)
                        else:
                            # 2nd strike गायब → plain WTT/WTB
                            self._reset()
                            return f"Resistance {pStat} {_fmt(pS)}", src
                    # Same 2nd → continue Shifted
                    self._shifted_wt = pStat
                    self._prev_p2nd  = p2nd
                    # return f"Resistance Shifted {pStat} {_fmt(pS)}", src
                    return f"Resistance Shifted {pStat} {_fmt(p2nd if p2nd else pS)}", src
                else:
                    # STR at shifted strike → strong
                    self._reset()
                    return f"Resistance strong {_fmt(pS)}", src
            else:
                # Strike बदल गई
                self._in_shifted = False
                if pStat in (WTT, WTB):
                    if p2nd:
                        return self._do_shift(p2nd, pStat, src)
                    # BUG 5 FIX: no p2nd → plain WTT/WTB
                    self._reset()
                    return f"Resistance {pStat} {_fmt(pS)}", src
                self._reset()
                return f"Resistance strong {_fmt(pS)}", src

        # ── SHIFTING state ───────────────────────
        if self._shifting:
            if pS == self._shift_strike:
                if pStat in (WTT, WTB):
                    # Shift strike high बन गई → IN_SHIFTED
                    self._shifting   = False
                    self._in_shifted = True
                    self._shifted_wt = pStat
                    self._prev_p2nd  = p2nd
                    # return f"Resistance Shifted {pStat} {_fmt(pS)}", src
                    return f"Resistance Shifted {pStat} {_fmt(p2nd if p2nd else pS)}", src
                else:
                    # STR at shift strike → Shifted strong
                    self._reset()
                    # return "Resistance Shifted strong", src
                    return f"Resistance strong {_fmt(pS)}", src
            else:
                # अलग strike
                if pStat in (WTT, WTB) and p2nd:
                    # New shift
                    return self._do_shift(p2nd, pStat, src)
                if pStat in (WTT, WTB) and not p2nd:
                    # BUG 5 FIX: WTT/WTB लेकिन p2nd नहीं → plain WTT/WTB
                    self._reset()
                    return f"Resistance {pStat} {_fmt(pS)}", src
                # STR / no 2nd → Shifted strong
                self._reset()
                # return "Resistance Shifted strong", src
                return f"Resistance strong {_fmt(pS)}", src

        # ── NORMAL state ─────────────────────────
        if pStat == WTT:
            if p2nd:
                return self._do_shift(p2nd, WTT, src)
            # BUG 2 FIX: WTT लेकिन p2nd नहीं → WTT दिखाओ, "strong" नहीं
            return f"Resistance WTT {_fmt(pS)}", src

        if pStat == WTB:
            if p2nd:
                return self._do_shift(p2nd, WTB, src)
            # BUG 2 FIX
            return f"Resistance WTB {_fmt(pS)}", src

        self._reset()
        return f"Resistance strong {_fmt(pS)}", src


# ─────────────────────────────────────────────────────
# Support Calculator (PE)
# PE में higher strike = primary (closer support below spot)
#
# *** BUG 1 FIX — Both-case WTT/WTB logic Resistance से अलग है: ***
#   Support Both: दोनों WTB → WTB shift
#                 कोई एक WTT → WTT shift   ← Resistance का उल्टा!
# ─────────────────────────────────────────────────────
class SupportCalculator:
    def __init__(self): self.reset()

    def reset(self):
        self._shifting     = False
        self._shift_strike = None
        self._shift_wt     = None
        self._in_shifted   = False
        self._shifted_wt   = None
        self._prev_p2nd    = None
        self._prev_label   = None

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
        self._shifting     = False
        self._shift_strike = None
        self._shift_wt     = None
        self._in_shifted   = False
        self._shifted_wt   = None
        self._prev_p2nd    = None

    def _compute(self, r):
        vs    = r.get("pe_high_vol_strike")
        os_   = r.get("pe_high_oi_strike")
        vStat = (r.get("pe_vol_status") or "").upper()
        oStat = (r.get("pe_oi_status")  or "").upper()

        # ════════════════════════════════════════
        # CASE 1: Same Strike (Both)
        # ════════════════════════════════════════
        if vs is not None and os_ is not None and vs == os_:
            pS     = vs
            src    = self._src("Both", pS)

            # दोनों WTB → WTB shift
            if vStat == WTB and oStat == WTB:
                target = r.get("pe_2nd_high_vol_strike") or r.get("pe_2nd_high_oi_strike")
                # FIX: IN_SHIFTED उसी primary strike (pS) पर → Shifted state preserve करो
                if target and self._in_shifted and self._shift_strike == pS:
                    self._shifted_wt = WTB
                    self._prev_p2nd = target
                    return f"Support Shifted WTB {_fmt(target)}", src
                self._reset()
                if target:
                    return self._do_shift(target, WTB, src)
                return f"Support WTB {_fmt(pS)}", src

            # कोई एक (या दोनों) WTT → WTT shift
            if vStat == WTT or oStat == WTT:
                if vStat == WTT and oStat == WTT:
                    target = r.get("pe_2nd_high_vol_strike") or r.get("pe_2nd_high_oi_strike")
                elif vStat == WTT:
                    target = r.get("pe_2nd_high_vol_strike")
                else:
                    target = r.get("pe_2nd_high_oi_strike")
                # FIX: IN_SHIFTED उसी primary strike (pS) पर → Shifted state preserve करो
                if target and self._in_shifted and self._shift_strike == pS:
                    self._shifted_wt = WTT
                    self._prev_p2nd = target
                    return f"Support Shifted WTT {_fmt(target)}", src
                self._reset()
                if target:
                    return self._do_shift(target, WTT, src)
                return f"Support WTT {_fmt(pS)}", src

        # ════════════════════════════════════════
        # CASE 2: Different Strikes
        # PE में higher strike = primary (closer to spot from below)
        # ════════════════════════════════════════
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

        # ── IN_SHIFTED state ─────────────────────
        if self._in_shifted:
            if pS == self._shift_strike:
                if pStat in (WTT, WTB):
                    # BUG 4 FIX: p2nd का कोई भी change catch करो
                    if p2nd != self._prev_p2nd:
                        self._in_shifted = False
                        if p2nd is not None:
                            return self._do_shift(p2nd, pStat, src)
                        else:
                            self._reset()
                            return f"Support {pStat} {_fmt(pS)}", src
                    self._shifted_wt = pStat
                    self._prev_p2nd  = p2nd
                    # return f"Support Shifted {pStat} {_fmt(pS)}", src
                    return f"Support Shifted {pStat} {_fmt(p2nd if p2nd else pS)}", src
                else:
                    self._reset()
                    return f"Support strong {_fmt(pS)}", src
            else:
                self._in_shifted = False
                if pStat in (WTT, WTB):
                    if p2nd:
                        return self._do_shift(p2nd, pStat, src)
                    # BUG 5 FIX
                    self._reset()
                    return f"Support {pStat} {_fmt(pS)}", src
                self._reset()
                return f"Support strong {_fmt(pS)}", src

        # ── SHIFTING state ───────────────────────
        if self._shifting:
            if pS == self._shift_strike:
                if pStat in (WTT, WTB):
                    self._shifting   = False
                    self._in_shifted = True
                    self._shifted_wt = pStat
                    self._prev_p2nd  = p2nd
                    # return f"Support Shifted {pStat} {_fmt(pS)}", src
                    return f"Support Shifted {pStat} {_fmt(p2nd if p2nd else pS)}", src
                else:
                    self._reset()
                    # return "Support Shifted strong", src
                    return f"Support strong {_fmt(pS)}", src
            else:
                if pStat in (WTT, WTB) and p2nd:
                    return self._do_shift(p2nd, pStat, src)
                if pStat in (WTT, WTB) and not p2nd:
                    # BUG 5 FIX
                    self._reset()
                    return f"Support {pStat} {_fmt(pS)}", src
                self._reset()
                # return "Support Shifted strong", src
                return f"Support strong {_fmt(pS)}", src

        # ── NORMAL state ─────────────────────────
        if pStat == WTT:
            if p2nd:
                return self._do_shift(p2nd, WTT, src)
            # BUG 2 FIX
            return f"Support WTT {_fmt(pS)}", src

        if pStat == WTB:
            if p2nd:
                return self._do_shift(p2nd, WTB, src)
            # BUG 2 FIX
            return f"Support WTB {_fmt(pS)}", src

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
    today  = today_ist() #-timedelta(days=1)

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

# Dashboard के लिए एक अलग व्यू जो Support और Resistance दोनों दिखाएगा। 
# यह व्यू एक HTML पेज रेंडर करेगा जिसमें एक कैलेंडर होगा, जिससे यूज़र किसी भी दिन का डेटा देख सकेगा। 
# डेटाबेस से डेटा फ़िल्टर करने के लिए चुनी गई तारीख का उपयोग किया जाएगा।
def support_resistance_view(request):
    # IST टाइमज़ोन सेट करें
    ist_timezone = pytz.timezone('Asia/Kolkata')
    today_date = datetime.now(ist_timezone).date()
    
    # HTML फॉर्म से चुनी गई तारीख प्राप्त करें
    selected_date_str = request.GET.get('date')
    
    if selected_date_str:
        # अगर यूज़र ने कैलेंडर से तारीख चुनी है
        selected_date = datetime.strptime(selected_date_str, '%Y-%m-%d').date()
    else:
        # अगर कोई तारीख नहीं चुनी गई है, तो आज की तारीख लें
        selected_date = today_date

    # चुनी गई तारीख के आधार पर डेटाबेस से डेटा फ़िल्टर करें
    sr_data_list = LiveSRData.objects.filter(Time__date=selected_date).order_by('-Time')
    
    context = {
        'sr_data': sr_data_list,
        # HTML के कैलेंडर में वही तारीख दिखाने के लिए इसे स्ट्रिंग में बदल कर भेज रहे हैं
        'selected_date': selected_date.strftime('%Y-%m-%d') 
    }
    
    return render(request, 'sr_data_page.html', context)




from django.http import HttpResponse
from datetime import date
# यह एक बहुत ही बेसिक बैकटेस्टिंग व्यू है जो आज के दिन के ऑप्शन चेन डेटा पर आधारित है। 
# यह स्पॉट प्राइस को रिवर्सल लेवल्स (Reversl_Ce और Reversl_Pe) के साथ तुलना करता है 
# और एंट्री, टारगेट, और स्टॉपलॉस को लॉग करता है। अंत में, यह एक HTML रिपोर्ट बनाता है 
# जिसमें सभी ट्रेड्स और कुल P&L दिखाया जाता है।

def run_backtest_view(request):
    symbol = request.GET.get('symbol', 'NIFTY').upper()
    today = date.today()
    target = 50
    sl = 50

    # 1. आज का सारा डेटा टाइम के हिसाब से लाएं (Time Ascending ताकि सुबह से शाम तक चले)
    # नोट: आपको अपने डेटाबेस के हिसाब से इसे थोड़ा एडजस्ट करना पड़ सकता है
    day_data = OptionChain.objects.filter(
        Symbol__iexact=symbol,
        Time__date=today
    ).values('Time', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe').order_by('Time')

    position = None  
    entry_price = 0
    total_pnl = 0  
    trades_log = []

    for row in day_data:
        time_str = row['Time'].strftime('%H:%M:%S') if row['Time'] else "00:00"
        spot = row['Spot_Price']
        
        # रिवर्सल की वैल्यू (अगर None नहीं है तो float में बदलें)
        r_val = float(row['Reversl_Ce']) if row['Reversl_Ce'] else None
        s_val = float(row['Reversl_Pe']) if row['Reversl_Pe'] else None

        if spot is None:
            continue

        # ─── 1. एंट्री ढूँढना ───
        if position is None:
            # PUT Buy: अगर स्पॉट R (Resistance) के बराबर या ऊपर जाए
            if r_val and spot >= r_val:
                position = 'PE'
                entry_price = spot
                trades_log.append(f"<div class='log entry-put'>[{time_str}] 🔴 <b>PUT Buy</b> @ {spot:.2f} (Resistance = {r_val:.2f})</div>")
            
            # CALL Buy: अगर स्पॉट S (Support) के बराबर या नीचे जाए
            elif s_val and spot <= s_val:
                position = 'CE'
                entry_price = spot
                trades_log.append(f"<div class='log entry-call'>[{time_str}] 🟢 <b>CALL Buy</b> @ {spot:.2f} (Support = {s_val:.2f})</div>")

        # ─── 2. टारगेट और स्टॉपलॉस चेक करना ───
        else:
            if position == 'CE':
                if spot >= (entry_price + target):
                    trades_log.append(f"<div class='log target'>&nbsp;&nbsp;✅ [{time_str}] CALL Target Hit @ {spot:.2f} (+{target} Points)</div>")
                    total_pnl += target
                    position = None
                elif spot <= (entry_price - sl):
                    trades_log.append(f"<div class='log sl'>&nbsp;&nbsp;❌ [{time_str}] CALL SL Hit @ {spot:.2f} (-{sl} Points)</div>")
                    total_pnl -= sl
                    position = None

            elif position == 'PE':
                if spot <= (entry_price - target):
                    trades_log.append(f"<div class='log target'>&nbsp;&nbsp;✅ [{time_str}] PUT Target Hit @ {spot:.2f} (+{target} Points)</div>")
                    total_pnl += target
                    position = None
                elif spot >= (entry_price + sl):
                    trades_log.append(f"<div class='log sl'>&nbsp;&nbsp;❌ [{time_str}] PUT SL Hit @ {spot:.2f} (-{sl} Points)</div>")
                    total_pnl -= sl
                    position = None

    # 3. HTML रिपोर्ट तैयार करना (सुंदर डिज़ाइन के साथ)
    pnl_color = "#00e676" if total_pnl >= 0 else "#ff1744"
    
    html_content = f"""
    <html>
    <head>
        <title>Today's Backtest Report</title>
        <style>
            body {{ background-color: #0f1115; color: #fff; font-family: 'Courier New', monospace; padding: 30px; }}
            h2 {{ color: #2962ff; border-bottom: 1px solid #333; padding-bottom: 10px; }}
            .container {{ max-width: 800px; margin: 0 auto; background: #181b21; padding: 20px; border-radius: 8px; border: 1px solid #333; }}
            .log {{ padding: 8px; margin: 4px 0; border-radius: 4px; font-size: 15px; }}
            .entry-put {{ background: rgba(255, 23, 68, 0.1); border-left: 3px solid #ff1744; }}
            .entry-call {{ background: rgba(0, 200, 83, 0.1); border-left: 3px solid #00c853; }}
            .target {{ color: #00c853; font-weight: bold; }}
            .sl {{ color: #ff1744; font-weight: bold; }}
            .result-box {{ margin-top: 20px; padding: 15px; background: #222; border-radius: 6px; text-align: center; font-size: 20px; border: 1px solid #444; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h2>📊 Backtest Report: {symbol} (Target: {target}, SL: {sl})</h2>
            <p>Date: {today}</p>
            <hr style="border: 0; border-top: 1px solid #333; margin-bottom: 20px;">
    """
    
    if not trades_log:
        html_content += "<div class='log' style='color:#888;'>आज कोई ट्रेड नहीं मिला (मार्केट R और S के बीच में ही रहा)।</div>"
    else:
        for log in trades_log:
            html_content += log
            
    html_content += f"""
            <div class="result-box">
                आज का कुल रिज़ल्ट: <strong style="color: {pnl_color};">{total_pnl} Points</strong>
            </div>
        </div>
    </body>
    </html>
    """
    return HttpResponse(html_content)









# =============================================================================
# FILE: yourapp/views.py  (इस function को अपने existing views.py में add करो)
#
# URL: urls.py में add करो:
#   from yourapp.views import backtest_view
#   path('backtest/', backtest_view, name='backtest'),
# =============================================================================

import math, re
from django.shortcuts import render
from django.utils.timezone import localtime




# ── Helpers (management command जैसे ही) ─────────────────────────────────────
def _is_valid(val):
    try:
        f = float(val)
        return not math.isnan(f) and not math.isinf(f)
    except (TypeError, ValueError):
        return False


def _get_step(strikes):
    all_s = sorted(set(strikes))
    if len(all_s) < 2:
        return 50.0
    diffs = [all_s[i + 1] - all_s[i] for i in range(len(all_s) - 1)]
    step = min(diffs)
    return step if step > 0 else 50.0


def _compute_effective_strikes(sr, step):
    res_status = str(sr.resistance_status).upper() if sr.resistance_status else ""
    res_base   = float(sr.resistance_strike)
    m          = re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
    res_target = float(m.group(1)) if m else res_base

    if   "SHIFTED WTT" in res_status: eff_res = res_base
    elif "SHIFTED WTB" in res_status: eff_res = res_base + step
    elif "WTT"         in res_status: eff_res = res_target + step
    elif "WTB"         in res_status: eff_res = res_target + step
    else:                             eff_res = res_base + step

    sup_status = str(sr.supprt_status).upper() if sr.supprt_status else ""
    sup_base   = float(sr.supprt_strike)
    m          = re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
    sup_target = float(m.group(1)) if m else sup_base

    if   "SHIFTED WTT" in sup_status: eff_sup = sup_base
    elif "SHIFTED WTB" in sup_status: eff_sup = sup_base - step
    elif "WTT"         in sup_status: eff_sup = sup_target - step
    elif "WTB"         in sup_status: eff_sup = sup_target - step
    else:                             eff_sup = sup_base - step

    return eff_res, eff_sup


def _run_backtest(symbol, date_str, target, sl):

    result = {
        'trades': [], 'stats': {}, 'timeline': [],
        'r_level': None, 's_level': None,
        'r_strike': None, 's_strike': None,
        'res_status': '', 'sup_status': '',
        'error': None,
    }

    # ── LiveSRData ─────────────────────────────────────────
    sr = (LiveSRData.objects
          .filter(Symbol__iexact=symbol, Time__date=date_str)
          .only('resistance_strike', 'supprt_strike', 'resistance_status', 'supprt_status')
          .order_by('-Time')
          .first())

    if not sr:
        result['error'] = f"LiveSRData नहीं मिली: {symbol} / {date_str}"
        return result

    if not sr.resistance_strike or not sr.supprt_strike:
        result['error'] = "resistance_strike या supprt_strike खाली है!"
        return result

    # ── OptionChain: SINGLE QUERY ─────────────────────────
    oc_qs = list(
        OptionChain.objects
        .filter(Symbol__iexact=symbol, Time__date=date_str)
        .values('Time', 'Spot_Price', 'Strike_Price', 'Reversl_Ce', 'Reversl_Pe')
        .order_by('Time')
    )

    if not oc_qs:
        result['error'] = "OptionChain data नहीं मिला!"
        return result

    # ── STEP FAST ─────────────────────────────────────────
    strikes = sorted({row['Strike_Price'] for row in oc_qs if row['Strike_Price']})
    step = min((b - a for a, b in zip(strikes, strikes[1:])), default=50.0)

    # ── Effective Strikes ─────────────────────────────────
    eff_res, eff_sup = _compute_effective_strikes(sr, step)

    # ── Latest data map (NO DB HIT) ───────────────────────
    latest_map = {}
    for row in reversed(oc_qs):  # latest first
        sp = row['Strike_Price']
        if sp not in latest_map:
            latest_map[sp] = row

    r_row = latest_map.get(eff_res)
    s_row = latest_map.get(eff_sup)

    if not r_row or not _is_valid(r_row.get('Reversl_Ce')):
        result['error'] = f"Strike {eff_res} की Reversl_Ce नहीं मिली!"
        return result

    if not s_row or not _is_valid(s_row.get('Reversl_Pe')):
        result['error'] = f"Strike {eff_sup} की Reversl_Pe नहीं मिली!"
        return result

    R_LEVEL = float(r_row['Reversl_Ce'])
    S_LEVEL = float(s_row['Reversl_Pe'])

    result.update({
        'r_level': R_LEVEL,
        's_level': S_LEVEL,
        'r_strike': eff_res,
        's_strike': eff_sup,
        'res_status': str(sr.resistance_status or ''),
        'sup_status': str(sr.supprt_status or ''),
        'step': step,
    })

    # ── Timeline FAST ─────────────────────────────────────
    timeline_dict = {}
    for row in oc_qs:
        t = row['Time']
        sp = row['Spot_Price']
        if t not in timeline_dict and _is_valid(sp):
            timeline_dict[t] = float(sp)

    if not timeline_dict:
        result['error'] = "Spot_Price data नहीं मिला!"
        return result

    sorted_times = sorted(timeline_dict)

    result['timeline'] = [
        {'time': localtime(t).strftime('%H:%M'), 'spot': timeline_dict[t]}
        for t in sorted_times
    ]

    # ── Backtest Loop (Optimized) ─────────────────────────
    trades = []
    append_trade = trades.append

    open_trade = None
    trade_no = 0

    for t in sorted_times:
        spot = timeline_dict[t]
        t_str = localtime(t).strftime('%H:%M:%S')

        # EXIT
        if open_trade:
            entry = open_trade['entry_spot']
            ttype = open_trade['type']

            if ttype == 'PUT':
                if spot <= entry - target:
                    pnl, res = target, 'TARGET'
                elif spot >= entry + sl:
                    pnl, res = -sl, 'SL'
                else:
                    pnl = None
            else:
                if spot >= entry + target:
                    pnl, res = target, 'TARGET'
                elif spot <= entry - sl:
                    pnl, res = -sl, 'SL'
                else:
                    pnl = None

            if pnl is not None:
                open_trade.update({
                    'exit_spot': round(spot, 2),
                    'exit_time': t_str,
                    'result': res,
                    'pnl': round(pnl, 2),
                })
                append_trade(open_trade)
                open_trade = None
                continue

        # ENTRY
        if not open_trade:
            if spot >= R_LEVEL:
                trade_no += 1
                open_trade = {
                    'no': trade_no,
                    'type': 'PUT',
                    'trigger': 'R',
                    'level': R_LEVEL,
                    'entry_spot': round(spot, 2),
                    'entry_time': t_str,
                }

            elif spot <= S_LEVEL:
                trade_no += 1
                open_trade = {
                    'no': trade_no,
                    'type': 'CALL',
                    'trigger': 'S',
                    'level': S_LEVEL,
                    'entry_spot': round(spot, 2),
                    'entry_time': t_str,
                }

    # ── Day End ───────────────────────────────────────────
    if open_trade:
        last_spot = timeline_dict[sorted_times[-1]]
        entry = open_trade['entry_spot']

        pnl = (entry - last_spot) if open_trade['type'] == 'PUT' else (last_spot - entry)

        open_trade.update({
            'exit_spot': round(last_spot, 2),
            'exit_time': localtime(sorted_times[-1]).strftime('%H:%M:%S'),
            'result': 'OPEN',
            'pnl': round(pnl, 2),
        })
        append_trade(open_trade)

    result['trades'] = trades

    # ── Stats FAST ────────────────────────────────────────
    total = len(trades)
    wins = sum(1 for tr in trades if tr['pnl'] > 0)
    losses = sum(1 for tr in trades if tr['pnl'] < 0)
    net = sum(tr['pnl'] for tr in trades)

    result['stats'] = {
        'total': total,
        'wins': wins,
        'losses': losses,
        'win_rate': round((wins / total * 100), 1) if total else 0,
        'net_pnl': round(net, 2),
        'open_time': localtime(sorted_times[0]).strftime('%H:%M:%S'),
        'close_time': localtime(sorted_times[-1]).strftime('%H:%M:%S'),
        'ticks': len(sorted_times),
    }

    return result


# =============================================================================
# VIEW
# =============================================================================
def backtest_view(request):
    context = {'result': None, 'form': {}}

    if request.method == 'POST':
        symbol  = request.POST.get('symbol', '').strip().upper()
        date    = request.POST.get('date', '').strip()
        target  = float(request.POST.get('target', 50))
        sl      = float(request.POST.get('sl', 50))

        context['form'] = {
            'symbol': symbol, 'date': date,
            'target': target, 'sl': sl,
        }

        if symbol and date:
            context['result'] = _run_backtest(symbol, date, target, sl)

    return render(request, 'mystock/backtestv.html', context)

import re as _re
from django.http import HttpResponse
from datetime import date
from django.utils import timezone  # <--- 1. यह नया इम्पोर्ट जोड़ा गया है (IST के लिए)
# सुनिश्चित करें कि आपके मॉडल्स (OptionChain, LiveSRData) ऊपर इम्पोर्टेड हों

def run_backtest_view(request):
    symbol = request.GET.get('symbol', 'NIFTY').upper()
    today = date.today()
    target = 50
    sl = 50

    # ─── 0. Step Size (गैप) निकालना ───
    oc_strikes = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=today).values_list('Strike_Price', flat=True).distinct()
    all_strikes = sorted(list(oc_strikes))
    step = 50
    if len(all_strikes) >= 2:
        diffs = [all_strikes[i+1] - all_strikes[i] for i in range(len(all_strikes)-1)]
        valid_diffs = [d for d in diffs if d > 0]
        if valid_diffs:
            step = min(valid_diffs)

    # ==========================================
    # 1. S/R Line Movement (सिर्फ चार्ट की लाइन खिसकने पर)
    # ==========================================
    sr_history_data = LiveSRData.objects.filter(
        Symbol__iexact=symbol,
        Time__date=today
    ).order_by('Time')

    sr_shifts_log = []
    last_eff_r = None
    last_eff_s = None

    for sr in sr_history_data:
        # <--- 2. यहाँ टाइम को भारतीय समय (IST) में बदला गया है --->
        local_time = timezone.localtime(sr.Time) if sr.Time else None
        time_str = local_time.strftime('%H:%M:%S') if local_time else "00:00"

        # ─── CALL SIDE (RESISTANCE LINE) ───
        res_status = str(sr.resistance_status).upper() if sr.resistance_status else ""
        res_base = sr.resistance_strike
        m_res = _re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
        res_target = float(m_res.group(1)) if m_res else res_base

        eff_r = None
        if res_base:
            if "SHIFTED WTT" in res_status: eff_r = res_base
            elif "SHIFTED WTB" in res_status: eff_r = res_base + step
            elif "WTT" in res_status: eff_r = res_target + step
            elif "WTB" in res_status: eff_r = res_target + step
            elif "STRONG" in res_status: eff_r = res_base + step
            else: eff_r = res_base + step

        # ─── PUT SIDE (SUPPORT LINE) ───
        sup_status = str(sr.supprt_status).upper() if sr.supprt_status else ""
        sup_base = sr.supprt_strike
        m_sup = _re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
        sup_target = float(m_sup.group(1)) if m_sup else sup_base

        eff_s = None
        if sup_base:
            if "SHIFTED WTT" in sup_status: eff_s = sup_base
            elif "SHIFTED WTB" in sup_status: eff_s = sup_base - step
            elif "WTT" in sup_status: eff_s = sup_target - step
            elif "WTB" in sup_status: eff_s = sup_target - step
            elif "STRONG" in sup_status: eff_s = sup_base - step
            else: eff_s = sup_base - step

        # अगर चार्ट पर 'Drawn Line' की पोजीशन बदलती है, तभी लिस्ट में डालेंगे
        if (eff_r is not None and eff_s is not None) and (eff_r != last_eff_r or eff_s != last_eff_s):
            sr_shifts_log.append(
                f"<div class='sr-shift-line'>⏱️ <b>{time_str}</b> &nbsp;👉&nbsp; "
                f"<span style='color:#ff8c00;'>R Line: {eff_r:.0f}</span> &nbsp;|&nbsp; "
                f"<span style='color:#00bfff;'>S Line: {eff_s:.0f}</span></div>"
            )
            last_eff_r = eff_r
            last_eff_s = eff_s

    # ==========================================
    # 2. बैकटेस्ट ट्रेड लॉजिक
    # ==========================================
    day_data = OptionChain.objects.filter(
        Symbol__iexact=symbol,
        Time__date=today
    ).values('Time', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe').order_by('Time')

    position = None  
    entry_price = 0
    total_pnl = 0  
    trades_log = []

    for row in day_data:
        # <--- 3. यहाँ भी टाइम को भारतीय समय (IST) में बदला गया है --->
        local_time = timezone.localtime(row['Time']) if row['Time'] else None
        time_str = local_time.strftime('%H:%M:%S') if local_time else "00:00"
        
        spot = row['Spot_Price']
        
        r_val = float(row['Reversl_Ce']) if row['Reversl_Ce'] else None
        s_val = float(row['Reversl_Pe']) if row['Reversl_Pe'] else None

        if spot is None:
            continue

        if position is None:
            if r_val and spot >= r_val:
                position = 'PE'
                entry_price = spot
                trades_log.append(f"<div class='log entry-put'>[{time_str}] 🔴 <b>PUT Buy</b> @ {spot:.2f} (R Line = {r_val:.2f})</div>")
            elif s_val and spot <= s_val:
                position = 'CE'
                entry_price = spot
                trades_log.append(f"<div class='log entry-call'>[{time_str}] 🟢 <b>CALL Buy</b> @ {spot:.2f} (S Line = {s_val:.2f})</div>")
        else:
            if position == 'CE':
                if spot >= (entry_price + target):
                    trades_log.append(f"<div class='log target'>&nbsp;&nbsp;✅ [{time_str}] CALL Target Hit @ {spot:.2f} (+{target} Points)</div>")
                    total_pnl += target
                    position = None
                elif spot <= (entry_price - sl):
                    trades_log.append(f"<div class='log sl'>&nbsp;&nbsp;❌ [{time_str}] CALL SL Hit @ {spot:.2f} (-{sl} Points)</div>")
                    total_pnl -= sl
                    position = None

            elif position == 'PE':
                if spot <= (entry_price - target):
                    trades_log.append(f"<div class='log target'>&nbsp;&nbsp;✅ [{time_str}] PUT Target Hit @ {spot:.2f} (+{target} Points)</div>")
                    total_pnl += target
                    position = None
                elif spot >= (entry_price + sl):
                    trades_log.append(f"<div class='log sl'>&nbsp;&nbsp;❌ [{time_str}] PUT SL Hit @ {spot:.2f} (-{sl} Points)</div>")
                    total_pnl -= sl
                    position = None

    # ==========================================
    # 3. HTML रिपोर्ट तैयार करना
    # ==========================================
    pnl_color = "#00e676" if total_pnl >= 0 else "#ff1744"
    
    html_content = f"""
    <html>
    <head>
        <title>Today's Backtest & S/R Report</title>
        <style>
            body {{ background-color: #0f1115; color: #fff; font-family: 'Courier New', monospace; padding: 30px; }}
            h2 {{ color: #2962ff; border-bottom: 1px solid #333; padding-bottom: 10px; margin-top: 0; }}
            .container {{ max-width: 900px; margin: 0 auto; display: flex; flex-direction: column; gap: 20px; }}
            .card {{ background: #181b21; padding: 20px; border-radius: 8px; border: 1px solid #333; }}
            .log {{ padding: 8px; margin: 4px 0; border-radius: 4px; font-size: 15px; }}
            .entry-put {{ background: rgba(255, 23, 68, 0.1); border-left: 3px solid #ff1744; }}
            .entry-call {{ background: rgba(0, 200, 83, 0.1); border-left: 3px solid #00c853; }}
            .target {{ color: #00c853; font-weight: bold; }}
            .sl {{ color: #ff1744; font-weight: bold; }}
            .sr-shift-line {{ padding: 6px 10px; font-size: 14px; border-bottom: 1px dashed #2a2d35; }}
            .sr-shift-line:last-child {{ border-bottom: none; }}
            .sr-history-box {{ background: #0d1117; border: 1px solid #2a2d35; border-radius: 6px; padding: 10px; max-height: 250px; overflow-y: auto; }}
            .result-box {{ margin-top: 10px; padding: 15px; background: #222; border-radius: 6px; text-align: center; font-size: 20px; border: 1px solid #444; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="card">
                <h2>📈 Support & Resistance Chart Lines</h2>
                <p style="color:#888; font-size: 13px; margin-top:-10px;">यहाँ सिर्फ वे समय (Time) दिखाए गए हैं जब चार्ट पर लाइन (R और S) ने अपनी असली जगह बदली।</p>
                <div class="sr-history-box">
                    {"".join(sr_shifts_log) if sr_shifts_log else "<div style='color:#888;'>आज लाइन ने अपनी जगह नहीं बदली।</div>"}
                </div>
            </div>

            <div class="card">
                <h2>📊 Backtest Trades (Target: {target}, SL: {sl})</h2>
                <p style="color:#888; font-size: 13px; margin-top:-10px;">Date: {today} | Symbol: {symbol}</p>
                <div style="margin-top: 15px;">
                    {"".join(trades_log) if trades_log else "<div class='log' style='color:#888;'>आज कोई ट्रेड नहीं मिला (मार्केट R और S के बीच में ही रहा)।</div>"}
                </div>
                
                <div class="result-box">
                    आज का कुल P&L (बिना ब्रोकरेज): <strong style="color: {pnl_color};">{total_pnl} Points</strong>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return HttpResponse(html_content)











 # लाइव पेपर ट्रेड्स देखने के लिए
from django.shortcuts import render
from django.utils import timezone
from .models import PaperTrade

def live_trades_view(request):
    symbol = request.GET.get('symbol', 'NIFTY').upper()
    selected_date_str = request.GET.get('date')

    if selected_date_str:
        selected_date = timezone.datetime.strptime(selected_date_str, '%Y-%m-%d').date()
    else:
        selected_date = timezone.now().date()

    # ✅ Fix 1: date range — index-friendly (timezone.make_aware use karo)
    day_start = timezone.make_aware(datetime.combine(selected_date, dt_time.min))
    day_end   = timezone.make_aware(datetime.combine(selected_date, dt_time.max))

    # ✅ Trades query — symbol exact match (already .upper() hai)
    trades = PaperTrade.objects.filter(
        symbol=symbol, trade_date=selected_date
    ).order_by('-entry_time')

    total_trades = trades.count()
    wins   = trades.filter(result='TARGET').count()
    losses = trades.filter(result='SL').count()

    # ✅ Fix 2: DB aggregate, Python loop nahi
    net_pnl = trades.exclude(result='SKIPPED').aggregate(total=Sum('pnl'))['total'] or 0.0

    # ✅ Fix 3: Heavy OptionChain queries page load pe nahi — sirf ek lightweight query
    latest_oc = OptionChain.objects.filter(
        Symbol=symbol, Time__gte=day_start, Time__lte=day_end
    ).only('Spot_Price').order_by('-Time').first()
    current_spot = latest_oc.Spot_Price if latest_oc else None

    context = {
        'trades': trades,
        'symbol': symbol,
        'selected_date': selected_date.strftime('%Y-%m-%d'),
        'total_trades': total_trades,
        'wins': wins,
        'losses': losses,
        'net_pnl': round(net_pnl, 2),
        'spot': current_spot,
        # r/s levels JS polling (dashboard_data_api) se aate hain
        'r_level': None,
        's_level': None,
        'abs_dist_r': None,
        'abs_dist_s': None,
        'dir_r': '',
        'dir_s': '',
        'is_r_closer': False,
    }

    return render(request, 'mystock/live_trades.html', context)

from django.http import JsonResponse
from django.utils import timezone
from django.utils.timezone import localtime
import re

def dashboard_data_api1(request):
    symbol = request.GET.get('symbol', 'NIFTY').upper()
    date_str = request.GET.get('date') 
    
    if date_str:
        selected_date = timezone.datetime.strptime(date_str, '%Y-%m-%d').date()
    else:
        selected_date = timezone.now().date()
    
    day_start = timezone.make_aware(datetime.combine(selected_date, dt_time.min))
    day_end   = timezone.make_aware(datetime.combine(selected_date, dt_time.max))

    # 1. Latest Spot Price
    latest_oc = OptionChain.objects.filter(
        Symbol=symbol, Time__gte=day_start, Time__lte=day_end
    ).only('Spot_Price', 'Time').order_by('-Time').first()
    current_spot = latest_oc.Spot_Price if latest_oc else None

    # 2. Trades Query
    trades_qs = PaperTrade.objects.filter(symbol=symbol, trade_date=selected_date).order_by('-entry_time')
    
    total_pnl = 0.0
    trades_list = []
    
    # सिंबल के हिसाब से स्टेप (Gap) तय करें
    step = 100 if "BANKNIFTY" in symbol or "SENSEX" in symbol else 50

    for tr in trades_qs:
        current_pnl = float(tr.pnl) if tr.pnl else 0.0
        
        if tr.result == 'OPEN' and current_spot:
            if tr.trade_type == 'PUT':
                current_pnl = float(tr.entry_spot) - float(current_spot)
            elif tr.trade_type == 'CALL':
                current_pnl = float(current_spot) - float(tr.entry_spot)
                
        total_pnl += current_pnl

        # 👇 डायनामिक टारगेट और स्टॉपलॉस कैलकुलेशन (UI के लिए)
        trade_target = 0
        trade_sl = 0
        
        if tr.entry_strike:
            if tr.trade_type == 'PUT':
                # PUT के लिए: Target = (Strike - Step) का Reversal_Ce, SL = (Strike + Step) का Reversal_Ce
                t_strike = tr.entry_strike - step
                sl_strike = tr.entry_strike + step
                
                t_row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=selected_date, Strike_Price=t_strike).order_by('-Time').first()
                sl_row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=selected_date, Strike_Price=sl_strike).order_by('-Time').first()
                
                trade_target = float(t_row.Reversl_Ce) if t_row and t_row.Reversl_Ce else (tr.entry_spot - 50)
                trade_sl = float(sl_row.Reversl_Ce) if sl_row and sl_row.Reversl_Ce else (tr.entry_spot + 50)
                
            elif tr.trade_type == 'CALL':
                # CALL के लिए: Target = (Strike + Step) का Reversal_Pe, SL = (Strike - Step) का Reversal_Pe
                t_strike = tr.entry_strike + step
                sl_strike = tr.entry_strike - step
                
                t_row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=selected_date, Strike_Price=t_strike).order_by('-Time').first()
                sl_row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=selected_date, Strike_Price=sl_strike).order_by('-Time').first()
                
                trade_target = float(t_row.Reversl_Pe) if t_row and t_row.Reversl_Pe else (tr.entry_spot + 50)
                trade_sl = float(sl_row.Reversl_Pe) if sl_row and sl_row.Reversl_Pe else (tr.entry_spot - 50)
        else:
            # अगर एंट्री स्ट्राइक नहीं है (पुराने डेटा के लिए), तो डिफॉल्ट 50 पॉइंट्स
            trade_target = tr.entry_spot + 50 if tr.trade_type == 'CALL' else tr.entry_spot - 50
            trade_sl = tr.entry_spot - 50 if tr.trade_type == 'CALL' else tr.entry_spot + 50

        trades_list.append({
            'type': tr.trade_type,
            'entry_time': localtime(tr.entry_time).strftime('%H:%M:%S') if tr.entry_time else '—',
            'trigger_level': tr.trigger_level,
            'trigger_price': round(tr.trigger_price, 2) if tr.trigger_price else 0,
            'entry_spot': round(tr.entry_spot, 2) if tr.entry_spot else 0,
            'exit_time': localtime(tr.exit_time).strftime('%H:%M:%S') if tr.exit_time else '—',
            'exit_spot': round(tr.exit_spot, 2) if tr.exit_spot else None,
            'result': tr.result,
            'pnl': round(current_pnl, 2),
            'target': round(trade_target, 2), # ✨ डायनामिक टारगेट
            'sl': round(trade_sl, 2),         # ✨ डायनामिक स्टॉपलॉस
            'entry_strike': tr.entry_strike
        })

    # 3. उस तारीख के लेवल (R/S) लाएं
    sr = LiveSRData.objects.filter(Symbol__iexact=symbol, Time__date=selected_date).order_by('-Time').first()

    # लेवल कैलकुलेशन लॉजिक
    r_trigger, s_trigger = None, None
    r_strike, s_strike = None, None
    
    try:
        ctrl, created = SyncControl.objects.get_or_create(name="bot_loop") 
        bot_active = ctrl.is_active
    except Exception as e:
        bot_active = False

    if sr and current_spot:
        step = 100 if "BANKNIFTY" in symbol or "SENSEX" in symbol else 50
        
        # Resistance logic
        res_status = str(sr.resistance_status).upper() if sr.resistance_status else ""
        res_base = float(sr.resistance_strike) if sr.resistance_strike else 0
        m_res = re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
        res_target = float(m_res.group(1)) if m_res else res_base

        if "SHIFTED WTT" in res_status: eff_r = res_base + step
        elif "SHIFTED WTB" in res_status: eff_r = res_base + step
        elif "WTT" in res_status: eff_r = res_target + step
        elif "WTB" in res_status: eff_r = res_target + step
        elif "STRONG" in res_status: eff_r = res_base + step
        else: eff_r = res_base + step

        # Support logic
        sup_status = str(sr.supprt_status).upper() if sr.supprt_status else ""
        sup_base = float(sr.supprt_strike) if sr.supprt_strike else 0
        m_sup = re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
        sup_target = float(m_sup.group(1)) if m_sup else sup_base

        if "SHIFTED WTT" in sup_status: eff_s = sup_base - step
        elif "SHIFTED WTB" in sup_status: eff_s = sup_base - step
        elif "WTT" in sup_status: eff_s = sup_target - step
        elif "WTB" in sup_status: eff_s = sup_target - step
        elif "STRONG" in sup_status: eff_s = sup_base - step
        else: eff_s = sup_base - step
        
        r_row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=selected_date, Strike_Price=eff_r).order_by('-Time').first()
        s_row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=selected_date, Strike_Price=eff_s).order_by('-Time').first()
        
        r_trigger = float(r_row.Reversl_Ce) if r_row and r_row.Reversl_Ce else None
        s_trigger = float(s_row.Reversl_Pe) if s_row and s_row.Reversl_Pe else None
        r_strike, s_strike = eff_r, eff_s

    return JsonResponse({
        'server_time': localtime(timezone.now()).strftime('%H:%M:%S'),
        'bot_active': bot_active,
        'total_pnl': round(total_pnl, 2), # ✨ Total PnL में भी लाइव चेंज दिखेगा
        'triggers': {
            'spot': current_spot,
            'r_trigger': r_trigger,
            'r_strike': r_strike,
            'r_status': sr.resistance_status if sr else '—',
            's_trigger': s_trigger,
            's_strike': s_strike,
            's_status': sr.supprt_status if sr else '—',
            'data_time': latest_oc.Time.isoformat() if latest_oc else None
        },
        'trades': trades_list
    })

def dashboard_data_api(request):
    symbol = request.GET.get('symbol', 'NIFTY').upper()
    date_str = request.GET.get('date') 
    
    if date_str:
        selected_date = timezone.datetime.strptime(date_str, '%Y-%m-%d').date()
    else:
        selected_date = timezone.now().date()
    
    day_start = timezone.make_aware(datetime.combine(selected_date, dt_time.min))
    day_end   = timezone.make_aware(datetime.combine(selected_date, dt_time.max))

    # 1. Latest Spot Price
    latest_oc = OptionChain.objects.filter(
        Symbol=symbol, Time__gte=day_start, Time__lte=day_end
    ).only('Spot_Price', 'Time').order_by('-Time').first()
    current_spot = latest_oc.Spot_Price if latest_oc else None

    # ==========================================
    # 2. MASTER LEVELS (सब कुछ एक जगह से)
    # ==========================================
    master_levels = get_master_levels(symbol, selected_date)

    # 3. Trades Query
    trades_qs = PaperTrade.objects.filter(symbol=symbol, trade_date=selected_date).order_by('-entry_time')
    
    total_pnl = 0.0
    trades_list = []

    for tr in trades_qs:
        current_pnl = float(tr.pnl) if tr.pnl else 0.0
        
        if tr.result == 'OPEN' and current_spot:
            if tr.trade_type == 'PUT':
                current_pnl = float(tr.entry_spot) - float(current_spot)
            elif tr.trade_type == 'CALL':
                current_pnl = float(current_spot) - float(tr.entry_spot)
                
        total_pnl += current_pnl

        # 👇 डायनामिक टारगेट और स्टॉपलॉस अब सीधे मास्टर लेवल से आएंगे (नो एक्स्ट्रा डेटाबेस क्वेरी)
        trade_side = "R" if tr.trade_type == "PUT" else "S"
        
        if master_levels[trade_side]["target"] is not None:
            trade_target = master_levels[trade_side]["target"]
        else:
            trade_target = (tr.entry_spot - 50) if tr.trade_type == 'PUT' else (tr.entry_spot + 50)
            
        if master_levels[trade_side]["sl"] is not None:
            trade_sl = master_levels[trade_side]["sl"]
        else:
            trade_sl = (tr.entry_spot + 50) if tr.trade_type == 'PUT' else (tr.entry_spot - 50)

        trades_list.append({
            'type': tr.trade_type,
            'entry_time': localtime(tr.entry_time).strftime('%H:%M:%S') if tr.entry_time else '—',
            'trigger_level': tr.trigger_level,
            'trigger_price': round(tr.trigger_price, 2) if tr.trigger_price else 0,
            'entry_spot': round(tr.entry_spot, 2) if tr.entry_spot else 0,
            'exit_time': localtime(tr.exit_time).strftime('%H:%M:%S') if tr.exit_time else '—',
            'exit_spot': round(tr.exit_spot, 2) if tr.exit_spot else None,
            'result': tr.result,
            'pnl': round(current_pnl, 2),
            'target': round(trade_target, 2), # ✨ डायनामिक टारगेट
            'sl': round(trade_sl, 2),         # ✨ डायनामिक स्टॉपलॉस
            'entry_strike': tr.entry_strike
        })

    # 4. Bot Status
    try:
        ctrl, created = SyncControl.objects.get_or_create(name="bot_loop") 
        bot_active = ctrl.is_active
    except Exception as e:
        bot_active = False

    # 5. JSON Response
    return JsonResponse({
        'server_time': localtime(timezone.now()).strftime('%H:%M:%S'),
        'bot_active': bot_active,
        'total_pnl': round(total_pnl, 2),
        'triggers': {
            'spot': current_spot,
            'r_trigger': master_levels["R"]["entry"],
            'r_strike': master_levels["R"]["strike"],
            'r_status': master_levels["R"]["status"] or '—',
            's_trigger': master_levels["S"]["entry"],
            's_strike': master_levels["S"]["strike"],
            's_status': master_levels["S"]["status"] or '—',
            'data_time': latest_oc.Time.isoformat() if latest_oc else None
        },
        'trades': trades_list
    })

from django.views.decorators.csrf import csrf_exempt
import json
from django.http import JsonResponse



@csrf_exempt
def skip_trade_api(request):
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            symbol = data.get('symbol', 'NIFTY').upper()
            trade_type = data.get('type')  # 'R' या 'S'
            price = float(data.get('price', 0))
            
            selected_date = timezone.now().date()
            
            # एक डमी ट्रेड सेव करें ताकि बॉट इसे "Already Traded" मानकर इग्नोर कर दे
            if trade_type == 'R':
                PaperTrade.objects.create(
                    symbol=symbol, trade_date=selected_date, trade_type='PUT',
                    entry_time=timezone.now(), entry_spot=price, 
                    trigger_level='R', trigger_price=price, result='SKIPPED', pnl=0.0
                )
            elif trade_type == 'S':
                PaperTrade.objects.create(
                    symbol=symbol, trade_date=selected_date, trade_type='CALL',
                    entry_time=timezone.now(), entry_spot=price, 
                    trigger_level='S', trigger_price=price, result='SKIPPED', pnl=0.0
                )
            return JsonResponse({'status': 'success'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'msg': str(e)})
    return JsonResponse({'status': 'invalid method'})


# पुराने ट्रेड्स और डैशबोर्ड के लिए व्यू
def trade_dashboard(request):
    today = timezone.now().date()

    start_date_str = request.GET.get('start_date')
    end_date_str   = request.GET.get('end_date')
    symbol_filter  = request.GET.get('symbol')

    if start_date_str:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
    else:
        start_date = today

    if end_date_str:
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
    else:
        end_date = today

    # SKIPPED हटाओ, date range filter करो
    trades = PaperTrade.objects.exclude(result="SKIPPED").filter(
        trade_date__range=[start_date, end_date]
    )

    if symbol_filter and symbol_filter != 'ALL':
        trades = trades.filter(symbol=symbol_filter)

    # ✅ Fix 1: एक ही aggregate call में सब कैलकुलेट करो — 3 अलग queries की जगह 1
    from django.db.models import Sum, Count, Q
    stats = trades.aggregate(
        total_pnl  = Sum('pnl'),
        total      = Count('id'),
        # wins       = Count('id', filter=Q(result='TARGET')),
        # losses     = Count('id', filter=Q(result='SL')),
        wins       = Count('id', filter=Q(result='TARGET') | (Q(result='MANUAL_EXIT', pnl__gt=0))),
        losses     = Count('id', filter=Q(result='SL') | (Q(result='MANUAL_EXIT', pnl__lt=0))),
    )

    total_pnl    = round(stats['total_pnl'] or 0, 2)
    total_trades = stats['total']
    wins         = stats['wins']
    losses       = stats['losses']
    closed_trades = wins + losses
    win_rate     = round((wins / closed_trades * 100), 1) if closed_trades > 0 else 0

    # ✅ Fix 2: unique_symbols — SKIPPED-only symbols filter होंगे, order_by भी
    unique_symbols = (
        PaperTrade.objects
        .exclude(result="SKIPPED")
        .values_list('symbol', flat=True)
        .distinct()
        .order_by('symbol')
    )

    return render(request, 'mystock/trade_dashboard.html', {
        'trades'         : trades.order_by('-trade_date', '-entry_time'),
        'start_date'     : start_date,
        'end_date'       : end_date,
        'selected_symbol': symbol_filter or 'ALL',
        'unique_symbols' : unique_symbols,
        'total_pnl'      : total_pnl,
        'total_trades'   : total_trades,   # ✅ Fix 1: template में trades.count नहीं चलेगा
        'wins'           : wins,            # ✅ Fix 2: नये stat cards के लिए
        'losses'         : losses,
        'win_rate'       : win_rate,
    })


from django.views.decorators.csrf import csrf_exempt
import json

# यह API डैशबोर्ड से मैन्युअल ट्रेड (PENDING) जोड़ने के लिए है, ताकि आप चार्ट पर लाइन के हिसाब से तुरंत ट्रेड डाल सकें

@csrf_exempt
def add_manual_trade_api(request):
    """डैशबोर्ड से मैन्युअल लिमिट ऑर्डर (PENDING) जोड़ने के लिए"""
    if request.method == "POST":
        try:
            data = json.loads(request.body)
            symbol = data.get('symbol', 'NIFTY').upper()
            trade_type = data.get('type', 'CALL').upper()
            price = float(data.get('price', 0))

            # डेटाबेस में PENDING ट्रेड बनाएँ
            PaperTrade.objects.create(
                symbol=symbol, 
                trade_date=timezone.now().date(), 
                trade_type=trade_type,
                trigger_level='MANUAL', # इससे पता चलेगा कि यह आपने डाला है
                trigger_price=price, 
                entry_spot=price, 
                result='PENDING', 
                pnl=0.0
            )
            return JsonResponse({'status': 'success', 'msg': f'{trade_type} Order set at {price}'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'msg': str(e)})
    return JsonResponse({'status': 'invalid method'})
  

