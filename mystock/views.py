import requests
import time
from datetime import datetime, time, timedelta, date, time as dt_time
from django.shortcuts import render
from requests.exceptions import SSLError, ConnectionError, Timeout
from .models import OptionChain, SupportResistance, SyncControl, TempOptionChain, LiveSRData, BotSettings, PaperTrade
from django.utils import timezone
from django.db.models import OuterRef, Subquery, Q, Sum, F, Count
from django.views.decorators.cache import never_cache, cache_page
from django.http import HttpResponse, JsonResponse
from django.contrib.auth.decorators import login_required
from django.views.decorators.csrf import csrf_exempt
from .management.commands.async_live import get_instrument_from_db, update_instrument_store_bulk, run_live_paper_trading 
from .symbol import symbols as ALL_SYMBOLS
from django.views.decorators.clickjacking import xframe_options_exempt
import pytz
from django.utils.timezone import localtime
import json
from django.db.models.functions import Abs
from asgiref.sync import async_to_sync
from .trade_logic import get_master_levels
from django.core.cache import cache



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
def admin_status_api(request):
    """सभी loops का status + trade stats + Bot Settings — optimized & defensive"""

    # ── Loop Status: एक query में सारे loops ──
    loop_names = ['nifty_loop', 'others_loop', 'bot_loop']
    try:
        existing = {c.name: c.is_active for c in SyncControl.objects.filter(name__in=loop_names)}
        for name in loop_names:
            if name not in existing:
                ctrl, _ = SyncControl.objects.get_or_create(
                    name=name, defaults={'is_active': True}
                )
                existing[name] = ctrl.is_active
    except Exception:
        existing = {name: False for name in loop_names}
    loops = existing

    today = timezone.now().date()
    stats_qs = PaperTrade.objects.filter(trade_date=today).exclude(result='SKIPPED').aggregate(
        total=Count('id'),
        wins=Count('id', filter=Q(result='TARGET') | Q(result='MANUAL_EXIT', pnl__gt=0)),
        losses=Count('id', filter=Q(result='SL') | Q(result='MANUAL_EXIT', pnl__lt=0)),
        pnl=Sum('pnl')
    )
    # FIX: only() से सिर्फ ज़रूरी field fetch
    latest_oc = OptionChain.objects.filter(Symbol='NIFTY', Time__date=today)        .only('Spot_Price').order_by('-Time').first()

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
@login_required
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
@login_required
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
            open_trades = list(PaperTrade.objects.filter(symbol=symbol, result="OPEN"))
            count = len(open_trades)
            
            if count == 0:
                return JsonResponse({'status': 'error', 'msg': 'कोई Open Trade नहीं है!'})
            
            now = timezone.now()
            # FIX: N+1 save() की जगह bulk_update — सिर्फ एक DB call
            for trade in open_trades:
                entry = float(trade.entry_spot)
                actual_pnl = (spot - entry) if trade.trade_type == 'CALL' else (entry - spot)
                trade.exit_spot = spot
                trade.exit_time = now
                trade.result    = "MANUAL_EXIT"
                trade.pnl       = round(actual_pnl, 2)

            PaperTrade.objects.bulk_update(
                open_trades, ['exit_spot', 'exit_time', 'result', 'pnl']
            )
                
            return JsonResponse({'status': 'success', 'msg': f'{count} Open Trades Closed at {spot}'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'msg': str(e)})
    return JsonResponse({'status': 'invalid method'})

# Constants
TIME_WINDOW = timedelta(seconds=1)
PAST_WINDOW = timedelta(seconds=2)

def calculate_percentage(change, max_change):
    return (change / max_change) * 100 if max_change > 0 and change else 0

def apply_ranking_styles1(all_data, metric):
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

def apply_ranking_styles(all_data, metric):
    if not all_data: 
        return
        
    # चेक करें कि डेटा Dictionary है (Cache से) या Object है (DB से)
    is_dict = isinstance(all_data[0], dict)
    
    # वैल्यू निकालने का हेल्पर
    def get_val(item, key):
        val = item.get(key) if is_dict else getattr(item, key, 0)
        return float(val) if val else 0.0

    ranked = sorted(all_data, key=lambda x: get_val(x, metric), reverse=True)
    base_class = metric.replace('_pct', '_class').replace('_percent', '_class')

    for idx, row in enumerate(ranked[:3]):
        val = get_val(row, metric)
        color = ""
        
        if idx == 0 and val > 0:
            color = "bg-green"
        elif idx == 1:
            if val >= 75:
                color = "bg-red"
            elif 65 <= val < 75:
                color = "bg-yellow"
        elif idx == 2:
            val_2nd = get_val(ranked[1], metric)
            if val_2nd >= 75 and val >= 65:
                color = "bg-yellow"

        # कलर क्लास सेट करें
        if color:
            if is_dict:
                row[base_class] = color
            else:
                setattr(row, base_class, color)

from datetime import timedelta

def _get_nifty_chain_context(symbol='NIFTY'):
    """
    Shared helper — option chain data fetch करता है।
    option_chain_dashboard और table_update_api दोनों इसे use करते हैं।
    """
    # 1. सबसे पहले फंक्शन का अपना 3-सेकंड वाला कैश चेक करें (सबसे तेज़)
    CACHE_KEY = f'chain_ctx_{symbol}'
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        print(f"⚡ FAST: {symbol} Data served from 3s Function Cache")
        return cached

    # 2. 🟢 मेमोरी (Cache) से लाइव डेटा उठाएं जो Async लूप ने सेव किया है
    live_data = cache.get(f'live_nifty_data_{symbol}')
    spot_price = cache.get(f'live_nifty_spot_{symbol}')

    if live_data and spot_price:
        print(f"🟢 SUCCESS: {symbol} Data served from Async Cache (0 DB Queries)")
        
        latest_time = live_data[0].get('Time') 
        expiry_date = live_data[0].get('Expiry_Date')
        
        # 🟢 कलर कोडिंग (Cache वाले Dict डेटा पर)
        all_metrics = [
            'CE_OI_percent', 'CE_Volume_percent', 'CE_COI_percent',
            'PE_OI_percent', 'PE_Volume_percent', 'PE_COI_percent'
        ]
        for metric in all_metrics:
            apply_ranking_styles(live_data, metric)
            
        # आपके display logic के हिसाब से ±15 strikes filter कर लें
        closest_idx = min(range(len(live_data)), key=lambda i: abs(live_data[i]['Strike_Price'] - spot_price))
        display_data = live_data[max(0, closest_idx - 15): min(len(live_data), closest_idx + 16)]
        
        # Spot Divider लॉजिक (Dict Syntax)
        for row in display_data:
            if row['Strike_Price'] > spot_price:
                row['is_spot_divider'] = True
                break
        
        result = (latest_time, spot_price, expiry_date, display_data, live_data)
        cache.set(CACHE_KEY, result, 3) 
        return result
    
    else:
        # 🔴 Fallback: जब Cache खाली हो तब Database से डेटा लाएं
        print(f"🔴 MISS: {symbol} Cache Empty! Fetching from Database (Fallback)")
        
        latest_entry = OptionChain.objects.filter(Symbol=symbol).order_by('-Time').first()
        if not latest_entry:
            return None, None, None, [], {}

        latest_time = latest_entry.Time
        spot_price  = latest_entry.Spot_Price
        expiry_date = latest_entry.Expiry_Date

        NEEDED_FIELDS = [
            'Strike_Price', 'CE_OI', 'CE_OI_percent', 'CE_Volume', 'CE_Volume_percent',
            'CE_COI', 'CE_COI_percent', 'CE_LTP', 'CE_IV', 'CE_Delta',
            'PE_OI', 'PE_OI_percent', 'PE_Volume', 'PE_Volume_percent',
            'PE_COI', 'PE_COI_percent', 'PE_LTP', 'PE_IV', 'PE_Delta',
            'Reversl_Ce', 'Reversl_Pe', 'Spot_Price', 'Time', 'Symbol', 'Lot_size',
        ]
        
        # 🔴 डेटाबेस से डेटा निकालें
        TIME_WINDOW = timedelta(seconds=1)
        all_data = list(
            OptionChain.objects.filter(
                Symbol=symbol,
                Time__range=(latest_time - TIME_WINDOW, latest_time + TIME_WINDOW)
            ).only(*NEEDED_FIELDS).order_by('Strike_Price')
        )

        # 🔴 कलर कोडिंग (Database वाले Object डेटा पर)
        all_metrics = [
            'CE_OI_percent', 'CE_Volume_percent', 'CE_COI_percent',
            'PE_OI_percent', 'PE_Volume_percent', 'PE_COI_percent'
        ]
        for metric in all_metrics:
            apply_ranking_styles(all_data, metric)

        # Window filter ±15 strikes around spot
        display_data = []
        if all_data:
            closest_idx = min(range(len(all_data)), key=lambda i: abs(all_data[i].Strike_Price - spot_price))
            display_data = all_data[max(0, closest_idx - 15): min(len(all_data), closest_idx + 16)]
            
            # Spot Divider लॉजिक (Object Syntax)
            for row in display_data:
                if row.Strike_Price > spot_price:
                    row.is_spot_divider = True
                    break

        result = (latest_time, spot_price, expiry_date, display_data, all_data)
        cache.set(CACHE_KEY, result, 3) 
        return result

def option_chain_dashboard(request):
    """
    FIX: Page load पर अब कोई DB query नहीं।
    पहले _get_nifty_chain_context() 4-5 queries करता था →
    Render/NeonDB पर हर query ~100ms = 400-500ms page load।

    अब: empty shell return करो (instant) → JS/AJAX table load करे।
    """
    return render(request, 'mystock/dashboard.html', {
        'data': [],
        'latest_time': None,
        'spot': None,
        'expiry_date': None,
    })

def table_update_api(request):
    """
    AJAX table refresh — shared helper + ETag cache.
    
    अगर data नहीं बदला तो 304 Not Modified return होगा (0 bytes transfer)।
    पहले हर 5 सेकंड 125KB भेजता था — अब सिर्फ तब जब data नया हो।
    """
    latest_time, spot_price, expiry_date, display_data, all_data = _get_nifty_chain_context()

    if latest_time is None:
        return HttpResponse("")

    # ETag चेक (सेम रहेगा)
    etag = f'"{latest_time.strftime("%Y%m%d%H%M%S")}"'
    if request.META.get('HTTP_IF_NONE_MATCH') == etag:
        return HttpResponse(status=304)

    # 🟢 भारी DB Aggregate की जगह सीधे Cache से Totals लें
    totals = cache.get('live_nifty_totals_NIFTY')
    
    if not totals:
        # Fallback: अगर किसी वजह से कैश में नहीं है तो पुराना DB तरीका (Backup)
        totals = OptionChain.objects.filter(
            Symbol='NIFTY',
            Time__range=(latest_time - TIME_WINDOW, latest_time + TIME_WINDOW)
        ).aggregate(
            total_ce_oi=Sum('CE_OI'),
            total_pe_oi=Sum('PE_OI'),
            total_ce_coi=Sum('CE_COI'),
            total_pe_coi=Sum('PE_COI')
        )

    # 🟢 SR Data को भी कैश से लिया जा सकता है अगर आपने वहां सेट किया है
    latest_sr = LiveSRData.objects.filter(Symbol='NIFTY').order_by('-Time').first()

    context = {
        'data': display_data,
        'latest_time': latest_time,
        'spot': spot_price,
        'expiry_date': expiry_date,
        'sr_data': latest_sr,
        **totals  # Dictionary के रूप में totals पास करें
    }
    # (render context और response return करें)
    response = render(request, 'mystock/table_partial.html', context)
    response['ETag'] = etag
    response['Cache-Control'] = 'no-cache'
    response['Vary'] = 'Accept-Encoding'   # GZip के साथ correct caching
    return response
    
# @login_required
def toggle_sync(request, loop_name):
    """Loop चालू/बंद करने का API — FIX: .get() → get_or_create() (DoesNotExist crash fix)"""
    if request.method != "POST":
        return JsonResponse({"error": "Invalid method"}, status=405)

    allowed = ['nifty_loop', 'others_loop', 'bot_loop']
    if loop_name not in allowed:
        return JsonResponse({"error": f"Unknown loop: {loop_name}"}, status=400)

    # FIX: पहले .get() था जो DoesNotExist exception देता था अगर record DB में नहीं था
    ctrl, _ = SyncControl.objects.get_or_create(name=loop_name, defaults={'is_active': True})
    ctrl.is_active = not ctrl.is_active
    ctrl.save()

    return JsonResponse({
        "loop": loop_name,
        "is_active": ctrl.is_active,
        "status": "ok"
    })

@cache_page(10) 
def all_stocks_dashboard(request):
    """हर symbol की latest SR entry — 60s cache से fast response"""
    CACHE_KEY = 'all_stocks_data'
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return render(request, 'mystock/all_stocks.html', {'stocks_data': cached})

    newest = SupportResistance.objects.filter(
        Symbol=OuterRef('Symbol')
    ).order_by('-Time')

    latest_data = list(SupportResistance.objects.filter(
        id=Subquery(newest.values('id')[:1])
    ).exclude(Reversl_Ce__lte=0.01).exclude(Reversl_Ce__isnull=True
    ).exclude(Reversl_Pe__lte=0.01).exclude(Reversl_Pe__isnull=True
    ).order_by('Symbol'))

    cache.set(CACHE_KEY, latest_data, 60)
    return render(request, 'mystock/all_stocks.html', {'stocks_data': latest_data})

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


# def generate_timestamps(today):
#     start_dt = timezone.make_aware(datetime.combine(today, time(9, 15)))
#     end_dt = timezone.make_aware(datetime.combine(today, time(15, 30)))

#     timestamps = []
#     current = start_dt
#     while current <= end_dt:
#         timestamps.append(current.astimezone(india_tz).strftime('%H:%M'))
#         current += timedelta(minutes=1)
#     return timestamps


# def option_chart_view(request):
#     today = timezone.localdate()
#     timestamps = generate_timestamps(today)

#     symbol = "NIFTY"

#     base_qs = OptionChain.objects.filter(
#         Time__range=(timezone.make_aware(datetime.combine(today, time(9, 15))),
#                      timezone.make_aware(datetime.combine(today, time(15, 30)))),
#         Symbol=symbol
#     ).only(
#         'Time', 'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe'
#     )

#     latest_row = base_qs.order_by('-Time').values('Spot_Price').first()
#     latest_spot = latest_row['Spot_Price'] if latest_row else None

#     available_strikes = sorted(set(base_qs.values_list('Strike_Price', flat=True)))

#     ce_selected = request.GET.getlist('ce_strikes')
#     pe_selected = request.GET.getlist('pe_strikes')

#     ce_selected = list(map(float, ce_selected)) if ce_selected else []
#     pe_selected = list(map(float, pe_selected)) if pe_selected else []

#     if not ce_selected and not pe_selected and latest_spot:
#         ce_selected = [s for s in available_strikes if s >= latest_spot][:10]
#         pe_selected = [s for s in reversed(available_strikes) if s <= latest_spot][:10]

#     selected_strikes = list(set(ce_selected + pe_selected))

#     qs = base_qs.filter(Strike_Price__in=selected_strikes).values(
#         'Time', 'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe'
#     ).order_by('Time')

#     # Map data by time
#     data_map = {}
#     for row in qs:
#         t = row['Time'].astimezone(india_tz).strftime('%H:%M')
#         if t not in data_map:
#             data_map[t] = {'spot': row['Spot_Price']}
#         data_map[t][str(row['Strike_Price'])] = {
#             'CE': row['Reversl_Ce'],
#             'PE': row['Reversl_Pe']
#         }

#     spot_prices = []
#     ce_data = {str(s): [] for s in ce_selected}
#     pe_data = {str(s): [] for s in pe_selected}

#     for t in timestamps:
#         spot_prices.append(data_map.get(t, {}).get('spot'))
#         for s in ce_selected:
#             ce_data[str(s)].append(data_map.get(t, {}).get(str(s), {}).get('CE'))
#         for s in pe_selected:
#             pe_data[str(s)].append(data_map.get(t, {}).get(str(s), {}).get('PE'))

#     chart_data = {
#         "timestamps": timestamps,
#         "spot_prices": spot_prices,
#         "ce_reversals": ce_data,
#         "pe_reversals": pe_data,
#     }

#     return render(request, "mystock/chart_template.html", {
#         "chart_data": json.dumps(chart_data),
#         "available_strikes": available_strikes,
#         "ce_selected": ce_selected,
#         "pe_selected": pe_selected,
#     })


# def option_chart_api(request):
#     today = timezone.localdate()
#     timestamps = generate_timestamps(today)

#     symbol = request.GET.get("symbol", "NIFTY")

#     ce_selected = request.GET.getlist('ce_strikes[]')
#     pe_selected = request.GET.getlist('pe_strikes[]')

#     ce_selected = list(map(float, ce_selected)) if ce_selected else []
#     pe_selected = list(map(float, pe_selected)) if pe_selected else []

#     selected_strikes = list(set(ce_selected + pe_selected))

#     qs = OptionChain.objects.filter(
#         Time__range=(timezone.make_aware(datetime.combine(today, time(9, 15))),
#                      timezone.make_aware(datetime.combine(today, time(15, 30)))),
#         Symbol=symbol,
#         Strike_Price__in=selected_strikes
#     ).values(
#         'Time', 'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe'
#     ).order_by('Time')

#     data_map = {}
#     for row in qs:
#         t = row['Time'].astimezone(india_tz).strftime('%H:%M')
#         if t not in data_map:
#             data_map[t] = {'spot': row['Spot_Price']}
#         data_map[t][str(row['Strike_Price'])] = {
#             'CE': row['Reversl_Ce'],
#             'PE': row['Reversl_Pe']
#         }

#     spot_prices = []
#     ce_data = {str(s): [] for s in ce_selected}
#     pe_data = {str(s): [] for s in pe_selected}

#     for t in timestamps:
#         spot_prices.append(data_map.get(t, {}).get('spot'))
#         for s in ce_selected:
#             ce_data[str(s)].append(data_map.get(t, {}).get(str(s), {}).get('CE'))
#         for s in pe_selected:
#             pe_data[str(s)].append(data_map.get(t, {}).get(str(s), {}).get('PE'))

#     return JsonResponse({
#         "timestamps": timestamps,
#         "spot_prices": spot_prices,
#         "ce_reversals": ce_data,
#         "pe_reversals": pe_data
#     })





import requests
from datetime import date, timedelta
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

def get_reversal_lines2(symbol: str, from_date: str, to_date: str):
    from datetime import date as _date
    today_str = _date.today().isoformat()
 
    cache_key = f"rev_lines_{symbol}_{from_date}_{to_date}"
 
    # आज की date है तो cache use मत करो — हमेशा fresh data चाहिए
    if from_date != today_str:
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
            # आज नहीं मिला → थोड़ी देर cache (60s) ताकि हर request पर DB hit न हो
            if from_date != today_str:
                cache.set(cache_key, [], timeout=60)
            return []
 
        step = 100 if 'BANKNIFTY' in symbol or 'SENSEX' in symbol else 50
 
        master_levels = get_master_levels(symbol, day_start.date())
        eff_res = master_levels["R"]["strike"]
        eff_sup = master_levels["S"]["strike"]
 
        ce_strikes_list = [eff_res, float(sr.resistance_strike)]
        pe_strikes_list = [eff_sup, float(sr.supprt_strike)]
        global_low  = min(pe_strikes_list + ce_strikes_list) - step
        global_high = max(pe_strikes_list + ce_strikes_list) + step
 
        oc_qs = OptionChain.objects.filter(
            Symbol=symbol,
            Time__gte=day_start,
            Time__lte=day_end,
            Strike_Price__gte=global_low,
            Strike_Price__lte=global_high,
        ).values(
            'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe'
        ).order_by('-Time')
 
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
 
        # ✅ Historical dates → 5 मिनट cache (नहीं बदलता)
        # ✅ आज की date → cache नहीं (हर request पर fresh)
        if from_date != today_str:
            cache.set(cache_key, lines, timeout=300)
 
        return lines
 
    except Exception as e:
        print(f"Reversal lines error: {e}")
        return []

def get_reversal_lines3(symbol: str, from_date: str, to_date: str):
    from datetime import date as _date
    today_str = _date.today().isoformat()
    cache_key = f"rev_lines_{symbol}_{from_date}_{to_date}"
 
    # 1. 🟢 पहले कैश चेक करें (आज के लिए 3s, हिस्ट्री के लिए 5m)
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
            if from_date != today_str:
                cache.set(cache_key, [], timeout=60)
            return []
 
        step = 100 if 'BANKNIFTY' in symbol or 'SENSEX' in symbol else 50
 
        master_levels = get_master_levels(symbol, day_start.date())
        eff_res = master_levels["R"]["strike"]
        eff_sup = master_levels["S"]["strike"]
 
        ce_strikes_list = [eff_res, float(sr.resistance_strike)]
        pe_strikes_list = [eff_sup, float(sr.supprt_strike)]
        global_low  = min(pe_strikes_list + ce_strikes_list) - step
        global_high = max(pe_strikes_list + ce_strikes_list) + step
 
        seen_strikes = {}
        spot_price   = None

        # =======================================================
        # 2. 🟢 HYBRID LOGIC: आज का डेटा सीधा मेमोरी (Cache) से लें
        # =======================================================
        is_live_data_used = False
        if from_date == today_str:
            live_data = cache.get(f'live_nifty_data_{symbol}')
            if live_data:
                is_live_data_used = True
                for row in live_data:
                    s = float(row.get('Strike_Price', 0))
                    if global_low <= s <= global_high:
                        seen_strikes[s] = row
                        if spot_price is None and row.get('Spot_Price'):
                            spot_price = float(row['Spot_Price'])

        # 3. 🔴 FALLBACK / HISTORY: अगर आज का कैश खाली है, या पुरानी डेट है
        # =======================================================
        if not is_live_data_used:
            oc_qs = OptionChain.objects.filter(
                Symbol=symbol,
                Time__gte=day_start,
                Time__lte=day_end,
                Strike_Price__gte=global_low,
                Strike_Price__lte=global_high,
            ).values(
                'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe'
            ).order_by('-Time')
            
            for row in oc_qs:
                s = float(row['Strike_Price'])
                if s not in seen_strikes:
                    seen_strikes[s] = row
                if spot_price is None and row.get('Spot_Price'):
                    spot_price = float(row['Spot_Price'])
 
        def valid(v):
            return v is not None and not math.isnan(float(v)) and not math.isinf(float(v))
 
        seen_ce, seen_pe = set(), set()
        lines = []
 
        for strike, row in sorted(seen_strikes.items()):
            s_price = row.get('Spot_Price')
            if not valid(s_price):
                continue
            
            spot = float(s_price)
            is_top    = (strike == eff_res)
            is_bottom = (strike == eff_sup)
 
            rev_ce = row.get('Reversl_Ce')
            if valid(rev_ce):
                rev_ce = float(rev_ce)
                if (rev_ce >= spot or is_top) and rev_ce not in seen_ce:
                    seen_ce.add(rev_ce)
                    lines.append({
                        "price":  rev_ce,
                        "strike": float(strike),
                        "type":   "CE",
                        "color":  "#ff8c00" if is_top else "#f85149",
                        "width":  4 if is_top else 1,
                        "dash":   0,
                        "label":  f"R {strike:.0f}" if is_top else f"CE {strike:.0f}",
                    })
 
            rev_pe = row.get('Reversl_Pe')
            if valid(rev_pe):
                rev_pe = float(rev_pe)
                if (rev_pe < spot or is_bottom) and rev_pe not in seen_pe:
                    seen_pe.add(rev_pe)
                    lines.append({
                        "price":  rev_pe,
                        "strike": float(strike),
                        "type":   "PE",
                        "color":  "#00bfff" if is_bottom else "#3fb950",
                        "width":  4 if is_bottom else 1,
                        "dash":   0,
                        "label":  f"S {strike:.0f}" if is_bottom else f"P {strike:.0f}",
                    })
 
        lines.sort(key=lambda x: x["price"], reverse=True)
 
        # =======================================================
        # 4. ✅ SMART CACHING (3s vs 5m)
        # =======================================================
        if from_date == today_str:
            # आज के लाइव डेटा को 3 सेकंड के लिए कैश करें (हज़ारों Requests हैंडल करने के लिए)
            cache.set(cache_key, lines, timeout=3)
        else:
            # पुराने डेटा को 5 मिनट तक कैश में रखें
            cache.set(cache_key, lines, timeout=300)
 
        return lines
 
    except Exception as e:
        print(f"Reversal lines error: {e}")
        return []

import math, re as _re
def get_reversal_lines(symbol: str, from_date: str, to_date: str):
    from datetime import date as _date
    today_str = _date.today().isoformat()
    
    # 1. Cache Key
    cache_key = f"rev_lines_with_history_all_{symbol}_{from_date}_{to_date}"
 
    cached_lines = cache.get(cache_key)
    if cached_lines is not None:
        return cached_lines
 
    try:
        day_start = timezone.make_aware(datetime.combine(
            datetime.strptime(from_date, '%Y-%m-%d').date(), dt_time.min))
        day_end   = timezone.make_aware(datetime.combine(
            datetime.strptime(to_date,   '%Y-%m-%d').date(), dt_time.max))
 
        step = 100 if 'BANKNIFTY' in symbol or 'SENSEX' in symbol else 50
        
        # =======================================================
        # PART A: SR Data से R और S की History बनाना
        # =======================================================
        all_sr_data = list(LiveSRData.objects.filter(
            Symbol=symbol, Time__gte=day_start, Time__lte=day_end
        ).order_by('Time'))

        res_history, sup_history = [], []
        current_res, current_sup = 0, 0
        eff_res_strike, eff_sup_strike = 0, 0

        if all_sr_data:
            for sr in all_sr_data:
                t_str = sr.Time.isoformat()
                
                # Resistance History
                r_status = str(sr.resistance_status or "").upper()
                r_base = float(sr.resistance_strike or 0)
                m_res = _re.search(r'(?:WTB|WTT)\s+(\d+)', r_status)
                r_target = float(m_res.group(1)) if m_res else r_base
                
                if "SHIFTED" in r_status: eff_res = r_base + step
                elif "WTT" in r_status: eff_res = r_target - step
                elif "WTB" in r_status: eff_res = r_target + step
                else: eff_res = r_base + step
                res_history.append({"time": t_str, "value": eff_res})

                # Support History
                s_status = str(sr.supprt_status or "").upper()
                s_base = float(sr.supprt_strike or 0)
                m_sup = _re.search(r'(?:WTB|WTT)\s+(\d+)', s_status)
                s_target = float(m_sup.group(1)) if m_sup else s_base
                
                if "SHIFTED" in s_status: eff_sup = s_base - step
                elif "WTT" in s_status: eff_sup = s_target - step
                elif "WTB" in s_status: eff_sup = s_target + step
                else: eff_sup = s_base - step
                sup_history.append({"time": t_str, "value": eff_sup})

            latest_sr = all_sr_data[-1]
            current_res = res_history[-1]["value"] if res_history else 0
            current_sup = sup_history[-1]["value"] if sup_history else 0
            
            master_levels = get_master_levels(symbol, day_start.date())
            eff_res_strike = master_levels["R"]["strike"] if master_levels else float(latest_sr.resistance_strike or 0)
            eff_sup_strike = master_levels["S"]["strike"] if master_levels else float(latest_sr.supprt_strike or 0)
        else:
            return [] # अगर SR डेटा नहीं है तो चार्ट पर कुछ नहीं बनेगा

        # =======================================================
        # PART B: Option Chain से बाकी CE/PE लाइनों का ताज़ा डेटा
        # =======================================================
        ce_strikes_list = [eff_res_strike, float(latest_sr.resistance_strike or 0)]
        pe_strikes_list = [eff_sup_strike, float(latest_sr.supprt_strike or 0)]
        global_low  = min(pe_strikes_list + ce_strikes_list) - step
        global_high = max(pe_strikes_list + ce_strikes_list) + step

        seen_strikes = {}
        spot_price   = None

        # 🟢 SMART CACHE: आज का डेटा सीधा मेमोरी से
        is_live_data_used = False
        if from_date == today_str:
            live_data = cache.get(f'live_nifty_data_{symbol}')
            if live_data:
                is_live_data_used = True
                for row in live_data:
                    s = float(row.get('Strike_Price', 0))
                    if global_low <= s <= global_high:
                        seen_strikes[s] = row
                        if spot_price is None and row.get('Spot_Price'):
                            spot_price = float(row['Spot_Price'])

        # 🔴 FALLBACK: अगर कैश खाली है या पुरानी डेट है
        if not is_live_data_used:
            oc_qs = OptionChain.objects.filter(
                Symbol=symbol, Time__gte=day_start, Time__lte=day_end,
                Strike_Price__gte=global_low, Strike_Price__lte=global_high
            ).values('Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe').order_by('-Time')
            
            for row in oc_qs:
                s = float(row['Strike_Price'])
                if s not in seen_strikes:
                    seen_strikes[s] = row
                if spot_price is None and row.get('Spot_Price'):
                    spot_price = float(row['Spot_Price'])

        def valid(v): return v is not None and not math.isnan(float(v)) and not math.isinf(float(v))

        # =======================================================
        # PART C: सारी लाइनें (Moving + Straight) कंबाइन करना
        # =======================================================
        lines = []
        seen_ce, seen_pe = set(), set()
        
        # 1. मुख्य R और S को History (Moving) के साथ जोड़ें
        if current_res > 0:
            lines.append({
                "price": current_res, "strike": eff_res_strike, "type": "CE",
                "color": "#ff8c00", "width": 4, "dash": 0,
                "label": f"R {eff_res_strike:.0f}", "history": res_history
            })
            seen_ce.add(current_res)
            
        if current_sup > 0:
            lines.append({
                "price": current_sup, "strike": eff_sup_strike, "type": "PE",
                "color": "#00bfff", "width": 4, "dash": 0,
                "label": f"S {eff_sup_strike:.0f}", "history": sup_history
            })
            seen_pe.add(current_sup)

        # 2. बाकी CE/PE को बिना History (Straight) के जोड़ें
        for strike, row in sorted(seen_strikes.items()):
            s_price = row.get('Spot_Price')
            if not valid(s_price): continue
            spot = float(s_price)

            is_top    = (strike == eff_res_strike)
            is_bottom = (strike == eff_sup_strike)

            rev_ce = row.get('Reversl_Ce')
            if valid(rev_ce):
                rev_ce = float(rev_ce)
                if (rev_ce >= spot or is_top) and rev_ce not in seen_ce:
                    seen_ce.add(rev_ce)
                    lines.append({
                        "price": rev_ce, "strike": float(strike), "type": "CE",
                        "color": "#f85149", "width": 1, "dash": 0,
                        "label": f"CE {strike:.0f}"
                    }) # इसमें history नहीं है, तो JS इसे Straight खींचेगा

            rev_pe = row.get('Reversl_Pe')
            if valid(rev_pe):
                rev_pe = float(rev_pe)
                if (rev_pe < spot or is_bottom) and rev_pe not in seen_pe:
                    seen_pe.add(rev_pe)
                    lines.append({
                        "price": rev_pe, "strike": float(strike), "type": "PE",
                        "color": "#3fb950", "width": 1, "dash": 0,
                        "label": f"P {strike:.0f}"
                    }) # इसमें भी history नहीं है

        lines.sort(key=lambda x: x["price"], reverse=True)

        # =======================================================
        # PART D: 60 सेकंड का Cache
        # =======================================================
        if from_date == today_str:
            cache.set(cache_key, lines, timeout=60)
        else:
            cache.set(cache_key, lines, timeout=86400)
 
        return lines
 
    except Exception as e:
        print(f"Reversal lines history error: {e}")
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
    from_date = request.GET.get("from_date", today.isoformat())
    to_date   = request.GET.get("to_date",   today.isoformat())

    candles        = []
    error          = None
    instrument_key = None

    # Step 1: DB से instrument_key लो  (same as before)
    instrument_key = get_instrument_key(symbol)

    if not instrument_key:
        error = f"'{symbol}' symbol DB में नहीं मिला।"
    else:
        # Upstox API से directly fetch (Redis हटाया)
        result = fetch_candle_data(instrument_key, unit, interval, to_date, from_date)
        if not result["success"]:
            error = result["error"]
        else:
            candles = parse_candles(result["data"])
            if not candles:
                error = "इस date range में कोई candle data नहीं मिली।"

    # Step 3: Reversal lines  (same as before)
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
    """
    Chart AJAX endpoint।

    Logic:
      आज की date + cache hit  → Redis से serve  (0 Upstox calls ✅)
      आज की date + cache miss → Upstox fetch करो, Redis में save करो
      पुरानी date              → Upstox से direct (historical, rarely called)
    """
    today_str = date.today().isoformat()

    symbol    = request.GET.get("symbol",    "").strip().upper()
    unit      = request.GET.get("unit",      "minutes")
    interval  = request.GET.get("interval",  "5")
    from_date = request.GET.get("from_date", today_str)
    to_date   = request.GET.get("to_date",   today_str)
    show_reversal = request.GET.get("reversal", "1") != "0"

    if not symbol:
        return JsonResponse({"error": "symbol parameter जरूरी है।"}, status=400)

    instrument_key = get_instrument_key(symbol)
    if not instrument_key:
        return JsonResponse({"error": f"'{symbol}' symbol DB में नहीं मिला।"}, status=404)

    # Upstox API से directly fetch
    result = fetch_candle_data(instrument_key, unit, interval, to_date, from_date)
    if not result["success"]:
        return JsonResponse({"error": result["error"]}, status=400)
    candles = parse_candles(result["data"])

    # ── Reversal lines ──────────────────────────────────────
    reversal_lines = get_reversal_lines(symbol, from_date, to_date) if show_reversal else []

    return JsonResponse({
        "symbol":         symbol,
        "instrument_key": instrument_key,
        "interval":       interval,
        "unit":           unit,
        "from_date":      from_date,
        "to_date":        to_date,
        "count":          len(candles),
        "candles":        candles,
        "reversal_lines": reversal_lines,
        # Debug info (चाहें तो हटा दें production में)

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

from django.utils.timezone import localtime
import re

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
@login_required
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


# ════════════════════════════════════════════════════════════════
#  DB Cleanup API — Admin Panel से पुराना data delete करने के लिए
# ════════════════════════════════════════════════════════════════
@csrf_exempt
@login_required
def db_cleanup_api(request):
    """
    Admin Panel → DB Cleanup section से call होता है।
    किसी भी table से किसी भी date से पुराना data delete करता है।

    POST body:
        table      : "OptionChain" | "SupportResistance" |
                     "TempOptionChain" | "LiveSRData" | "ALL"
        cutoff_date: "YYYY-MM-DD"  — इस date से पहले का सब delete होगा
        optimize   : true/false    — VACUUM/ANALYZE चलाएं?
    """
    if request.method != "POST":
        return JsonResponse({"status": "error", "msg": "POST method required"}, status=405)

    try:
        data        = json.loads(request.body)
        table       = data.get("table", "ALL").strip()
        cutoff_str  = data.get("cutoff_date", "")
        run_optimize = data.get("optimize", False)

        # ── Cutoff date validate ──────────────────────────────────
        if not cutoff_str:
            return JsonResponse({"status": "error", "msg": "cutoff_date ज़रूरी है"})

        from datetime import datetime as _dt
        try:
            cutoff_date = _dt.strptime(cutoff_str, "%Y-%m-%d").date()
        except ValueError:
            return JsonResponse({"status": "error", "msg": "Date format YYYY-MM-DD होना चाहिए"})

        cutoff_time = timezone.make_aware(
            _dt.combine(cutoff_date, _dt.min.time())
        )

        # ── Table map ────────────────────────────────────────────
        TABLE_MAP = {
            "OptionChain":       OptionChain,
            "SupportResistance": SupportResistance,
            "TempOptionChain":   TempOptionChain,
            "LiveSRData":        LiveSRData,
            "PaperTrade":        PaperTrade,
        }

        allowed = list(TABLE_MAP.keys()) + ["ALL"]
        if table not in allowed:
            return JsonResponse({"status": "error", "msg": f"Invalid table: {table}"})

        targets = TABLE_MAP if table == "ALL" else {table: TABLE_MAP[table]}

    

        # ── Delete ───────────────────────────────────────────────
        results = {}
        total   = 0
        for name, model in targets.items():
            # PaperTrade टेबल में Time कॉलम नहीं है, उसमें trade_date है
            if name == "PaperTrade":
                deleted, _ = model.objects.filter(trade_date__lt=cutoff_date).delete()
            else:
                deleted, _ = model.objects.filter(Time__lt=cutoff_time).delete()
            
            results[name] = deleted
            total += deleted

        # ── Optimize (optional) ──────────────────────────────────
        optimize_msg = ""
        if run_optimize:
            from django.db import connection
            with connection.cursor() as cursor:
                db_engine = connection.vendor  # 'sqlite' या 'postgresql'
                if db_engine == "sqlite":
                    cursor.execute("PRAGMA optimize;")
                    cursor.execute("VACUUM;")
                    optimize_msg = "SQLite VACUUM + optimize चला।"
                elif db_engine == "postgresql":
                    cursor.execute("VACUUM ANALYZE;")
                    optimize_msg = "PostgreSQL VACUUM ANALYZE चला।"

        return JsonResponse({
            "status":       "success",
            "cutoff_date":  cutoff_str,
            "table":        table,
            "total_deleted": total,
            "details":      results,
            "optimize_msg": optimize_msg,
            "msg": f"✅ {total} records deleted from {table} (before {cutoff_str})"
        })

    except Exception as e:
        import traceback
        return JsonResponse({
            "status": "error",
            "msg":    str(e),
            "detail": traceback.format_exc()
        }, status=500)


@login_required
def db_cleanup_preview_api(request):
    """
    Delete से पहले count दिखाता है — confirmation के लिए।
    GET params: table, cutoff_date
    """
    table      = request.GET.get("table", "ALL")
    cutoff_str = request.GET.get("cutoff_date", "")

    if not cutoff_str:
        return JsonResponse({"status": "error", "msg": "cutoff_date ज़रूरी है"})

    from datetime import datetime as _dt
    try:
        cutoff_date = _dt.strptime(cutoff_str, "%Y-%m-%d").date()
    except ValueError:
        return JsonResponse({"status": "error", "msg": "Invalid date format"})

    cutoff_time = timezone.make_aware(_dt.combine(cutoff_date, _dt.min.time()))

    TABLE_MAP = {
        "OptionChain":       OptionChain,
        "SupportResistance": SupportResistance,
        "TempOptionChain":   TempOptionChain,
        "LiveSRData":        LiveSRData,
        "PaperTrade":        PaperTrade,
    }

    targets = TABLE_MAP if table == "ALL" else {table: TABLE_MAP.get(table)}
    if None in targets.values():
        return JsonResponse({"status": "error", "msg": f"Invalid table: {table}"})

   

    counts = {}
    total  = 0
    for name, model in targets.items():
        # यहाँ भी PaperTrade के लिए trade_date का इस्तेमाल करें
        if name == "PaperTrade":
            c = model.objects.filter(trade_date__lt=cutoff_date).count()
        else:
            c = model.objects.filter(Time__lt=cutoff_time).count()
            
        counts[name] = c
        total += c

    return JsonResponse({
        "status":      "ok",
        "cutoff_date": cutoff_str,
        "table":       table,
        "total":       total,
        "details":     counts,
    })


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
@login_required
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
  








# test code 

# views.py में जोड़ें

from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from datetime import datetime, time as dt_time
import json

# @login_required
# def market_replay_view(request):
#     """सिर्फ HTML पेज रेंडर करेगा"""
#     context = {
#         'symbols': ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'SENSEX'],
#     }
#     return render(request, 'mystock/market_replay.html', context)


# import traceback  # Bug 9 Fix: सिर्फ एक बार, यहाँ import करें
# import re


# @login_required
# def market_replay_data_api(request):
#     """
#     चुनी गई Date का सारा Option Chain और SR Data एक साथ (Bulk)
#     Frontend को भेजेगा ताकि JS उसे अपने हिसाब से (Play/Pause) चला सके।
#     (बिना ऑटो-ट्रेडिंग के)
#     """
#     symbol = request.GET.get('symbol', 'NIFTY').upper()
#     date_str = request.GET.get('date')

#     if not date_str:
#         return JsonResponse({'error': 'Date is required'}, status=400)

#     try:
#         selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
#         day_start = timezone.make_aware(datetime.combine(selected_date, dt_time(9, 15)))
#         day_end   = timezone.make_aware(datetime.combine(selected_date, dt_time(15, 30)))

#         # 1. Option Chain Data
#         oc_data = OptionChain.objects.filter(
#             Symbol=symbol, Time__gte=day_start, Time__lte=day_end
#         ).values(
#             'Time', 'Strike_Price', 'Spot_Price',
#             'CE_OI', 'PE_OI', 'CE_OI_percent', 'PE_OI_percent',
#             'CE_Volume', 'PE_Volume', 'CE_Volume_percent', 'PE_Volume_percent',
#             'CE_COI', 'PE_COI', 'CE_COI_percent', 'PE_COI_percent',
#             'Reversl_Ce', 'Reversl_Pe'
#         ).order_by('Time', 'Strike_Price')

#         if not oc_data.exists():
#             return JsonResponse({'error': 'इस तारीख का कोई Option Chain डेटा नहीं है।'}, status=404)

#         # Time के हिसाब से डेटा को ग्रुप करना
#         grouped_oc = {}
#         for row in oc_data:
#             t_str = timezone.localtime(row['Time']).strftime('%H:%M:%S')
#             if t_str not in grouped_oc:
#                 grouped_oc[t_str] = {
#                     'spot': float(row['Spot_Price']) if row['Spot_Price'] else 0.0,
#                     'chain': []
#                 }
#             # Bug 1 Fix: Time datetime object को string में convert करें
#             # वरना JsonResponse serialize नहीं कर पाता और 500 error आती है
#             row_serializable = dict(row)
#             row_serializable['Time'] = t_str
#             grouped_oc[t_str]['chain'].append(row_serializable)

#         # 2. SR Data
#         sr_data_qs = LiveSRData.objects.filter(
#             Symbol=symbol, Time__gte=day_start, Time__lte=day_end
#         ).values(
#             'Time', 'resistance_strike', 'resistance_status', 'supprt_strike', 'supprt_status'
#         ).order_by('Time')

#         sr_data_list = []
#         for row in sr_data_qs:
#             t_str = timezone.localtime(row['Time']).strftime('%H:%M:%S')
#             # Bug 1 Fix: SR में भी Time को string बनाएं
#             row_serializable = dict(row)
#             row_serializable['Time'] = t_str
#             sr_data_list.append((t_str, row_serializable))

#         # 3. Master Levels
#         master_levels = get_master_levels(symbol, selected_date)

#         # 4. रिवर्सल लाइनें (बिना बॉट के)
#         final_timeline = {}
#         sorted_oc_times = sorted(list(grouped_oc.keys()))

#         last_known_sr = {}
#         sr_idx = 0

#         for t_str in sorted_oc_times:
#             data = grouped_oc[t_str]

#             # SR डेटा का Forward Fill
#             while sr_idx < len(sr_data_list) and sr_data_list[sr_idx][0] <= t_str:
#                 last_known_sr = sr_data_list[sr_idx][1]
#                 sr_idx += 1

#             current_sr_dict = last_known_sr
#             current_lines = get_reversal_lines_for_replay(symbol, date_str, current_sr_dict, data['chain'])

#             final_timeline[t_str] = {
#                 'oc': data,
#                 'sr': current_sr_dict,
#                 'lines': current_lines
#             }

#         # 5. Upstox Chart Data Fetch
#         instrument_key = get_instrument_key(symbol)
#         candles = []
#         if instrument_key:
#             chart_res = fetch_candle_data(instrument_key, "minutes", "1", date_str, date_str)
#             if chart_res and chart_res.get("success"):
#                 candles = parse_candles(chart_res["data"])

#         return JsonResponse({
#             'success': True,
#             'symbol': symbol,
#             'date': date_str,
#             'master_levels': master_levels,
#             'timeline': sorted_oc_times,
#             'timeline_data': final_timeline,
#             'candles': candles
#         })

#     except Exception as e:
#         print(traceback.format_exc())
#         return JsonResponse({'error': str(e)}, status=500)


# def get_reversal_lines_for_replay(symbol, date_str, sr_dict, oc_list):
#     """
#     यह फंक्शन हर एक टिक (Tick) के SR Data के अनुसार डायनामिक तरीके से
#     मास्टर R और Master S निकालता है और सिर्फ काम की लाइनें ड्रा करता है।
#     """
#     try:
#         if not sr_dict or not oc_list:
#             return []

#         step = 100 if 'BANKNIFTY' in symbol or 'SENSEX' in symbol else 50
#         spot = float(oc_list[0].get('Spot_Price', 0)) if oc_list else 0

#         # 1. डायनामिक मास्टर लेवल निकालें (CURRENT Tick के SR Data से)
#         res_status = str(sr_dict.get('resistance_status', '')).upper()
#         res_base = float(sr_dict.get('resistance_strike') or 0)
#         m_res = re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
#         res_target = float(m_res.group(1)) if m_res else res_base

#         if "SHIFTED WTT" in res_status:   eff_res = res_base + step
#         elif "SHIFTED WTB" in res_status:  eff_res = res_base + step
#         elif "WTT" in res_status:          eff_res = res_target - step
#         elif "WTB" in res_status:          eff_res = res_target + step
#         elif "STRONG" in res_status:       eff_res = res_base + step
#         else:                              eff_res = res_base + step

#         sup_status = str(sr_dict.get('supprt_status', '')).upper()
#         sup_base = float(sr_dict.get('supprt_strike') or 0)
#         m_sup = re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
#         sup_target = float(m_sup.group(1)) if m_sup else sup_base

#         if "SHIFTED WTT" in sup_status:    eff_sup = sup_base - step
#         elif "SHIFTED WTB" in sup_status:  eff_sup = sup_base - step
#         elif "WTT" in sup_status:          eff_sup = sup_target - step
#         elif "WTB" in sup_status:          eff_sup = sup_target + step
#         elif "STRONG" in sup_status:       eff_sup = sup_base - step
#         else:                              eff_sup = sup_base - step

#         # अगर शुरुआत में SR डेटा नहीं है तो स्पॉट के हिसाब से सेट करें
#         if eff_res == 0: eff_res = spot + step
#         if eff_sup == 0: eff_sup = spot - step
#         if res_base == 0: res_base = spot
#         if sup_base == 0: sup_base = spot

#         # 2. लिमिट तय करें
#         ce_strikes_list = [eff_res, res_base]
#         pe_strikes_list = [eff_sup, sup_base]

#         global_low  = min(pe_strikes_list + ce_strikes_list) - step
#         global_high = max(pe_strikes_list + ce_strikes_list) + step

#         lines = []
#         seen_ce, seen_pe = set(), set()

#         # 3. FIRST PASS: Master R/S लाइनें
#         for row in oc_list:
#             strike = float(row.get('Strike_Price', 0))
#             if strike < global_low or strike > global_high: continue

#             is_top = (strike == eff_res)
#             is_bottom = (strike == eff_sup)

#             if is_top and row.get('Reversl_Ce') and float(row['Reversl_Ce']) > 0:
#                 val = float(row['Reversl_Ce'])
#                 lines.append({"price": val, "color": "#ff8c00", "width": 3, "label": f"R {strike:.0f}"})
#                 seen_ce.add(val)

#             if is_bottom and row.get('Reversl_Pe') and float(row['Reversl_Pe']) > 0:
#                 val = float(row['Reversl_Pe'])
#                 lines.append({"price": val, "color": "#00bfff", "width": 3, "label": f"S {strike:.0f}"})
#                 seen_pe.add(val)

#         # 4. SECOND PASS: बाकी लाइनें
#         for row in oc_list:
#             strike = float(row.get('Strike_Price', 0))
#             if strike < global_low or strike > global_high: continue

#             is_top = (strike == eff_res)
#             is_bottom = (strike == eff_sup)

#             if not is_top and row.get('Reversl_Ce') and float(row['Reversl_Ce']) > 0:
#                 val = float(row['Reversl_Ce'])
#                 if val >= spot and val not in seen_ce:
#                     seen_ce.add(val)
#                     lines.append({"price": val, "color": "#f85149", "width": 1, "label": f"CE {strike:.0f}"})

#             if not is_bottom and row.get('Reversl_Pe') and float(row['Reversl_Pe']) > 0:
#                 val = float(row['Reversl_Pe'])
#                 if val < spot and val not in seen_pe:
#                     seen_pe.add(val)
#                     lines.append({"price": val, "color": "#3fb950", "width": 1, "label": f"P {strike:.0f}"})

#         lines.sort(key=lambda x: x["price"], reverse=True)
#         return lines

#     except Exception as e:
#         print(f"Replay Line Error: {e}")
#         return []


import traceback
import re
import logging
from datetime import datetime, time as dt_time
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.utils import timezone
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# ✅ Fix #5: Magic Numbers को Constants में बदला
STEP_MAP = {
    'BANKNIFTY': 100,
    'SENSEX': 100,
}
DEFAULT_STEP = 50

MARKET_START = dt_time(9, 15)
MARKET_END   = dt_time(15, 30)


# ✅ Fix #3 & #1: Decimal / None → float safe conversion helper
def _safe_float(value):
    """
    Decimal, int, str, None — किसी भी value को safely float में convert करता है।
    None या invalid होने पर 0.0 return करता है।
    """
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _serialize_row(row: dict, time_str: str) -> dict:
    """
    ORM .values() से आई row की हर Decimal/None value को
    JSON-safe (float/str) में convert करता है।
    Time field को string से replace करता है।
    """
    serialized = {}
    for k, v in row.items():
        if k == 'Time':
            serialized[k] = time_str
        elif hasattr(v, '__float__'):   # Decimal, float, int
            serialized[k] = _safe_float(v)
        else:
            serialized[k] = v
    return serialized


@login_required
def market_replay_view(request):
    """सिर्फ HTML पेज रेंडर करेगा"""
    context = {
        'symbols': ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'SENSEX'],
    }
    return render(request, 'mystock/market_replay.html', context)


@login_required
def market_replay_data_api(request):
    """
    चुनी गई Date का सारा Option Chain और SR Data एक साथ (Bulk)
    Frontend को भेजेगा ताकि JS उसे अपने हिसाब से (Play/Pause) चला सके।
    (बिना ऑटो-ट्रेडिंग के)
    """
    symbol   = request.GET.get('symbol', 'NIFTY').upper()
    date_str = request.GET.get('date')

    if not date_str:
        return JsonResponse({'error': 'Date is required'}, status=400)

    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()

        # ✅ Fix #2 comment: HH:MM:SS (24-hour) format use हो रहा है
        # इसलिए string comparison lexicographically सही है।
        # कभी भी 12-hour format (%I) use मत करना वरना comparison टूट जाएगा।
        day_start = timezone.make_aware(datetime.combine(selected_date, MARKET_START))
        day_end   = timezone.make_aware(datetime.combine(selected_date, MARKET_END))

        # ── 1. Option Chain Data ─────────────────────────────────────────────
        oc_data = OptionChain.objects.filter(
            Symbol=symbol, Time__gte=day_start, Time__lte=day_end
        ).values(
            'Time', 'Strike_Price', 'Spot_Price',
            'CE_OI', 'PE_OI', 'CE_OI_percent', 'PE_OI_percent',
            'CE_Volume', 'PE_Volume', 'CE_Volume_percent', 'PE_Volume_percent',
            'CE_COI', 'PE_COI', 'CE_COI_percent', 'PE_COI_percent',
            'Reversl_Ce', 'Reversl_Pe'
        ).order_by('Time', 'Strike_Price')

        if not oc_data.exists():
            return JsonResponse(
                {'error': 'इस तारीख का कोई Option Chain डेटा नहीं है।'},
                status=404
            )

        # Time के हिसाब से डेटा को group करना
        grouped_oc = {}
        for row in oc_data:
            t_str = timezone.localtime(row['Time']).strftime('%H:%M:%S')

            if t_str not in grouped_oc:
                grouped_oc[t_str] = {
                    # ✅ Fix #3: _safe_float से Decimal → float
                    'spot': _safe_float(row['Spot_Price']),
                    'chain': []
                }

            # ✅ Fix #3: सभी fields को safely serialize करो
            grouped_oc[t_str]['chain'].append(_serialize_row(dict(row), t_str))

        # ── 2. SR Data ───────────────────────────────────────────────────────
        sr_data_qs = LiveSRData.objects.filter(
            Symbol=symbol, Time__gte=day_start, Time__lte=day_end
        ).values(
            'Time', 'resistance_strike', 'resistance_status',
            'supprt_strike', 'supprt_status'
        ).order_by('Time')

        # ✅ Fix #4: tuple list की जगह sorted keys + dict — cleaner और faster
        sr_data_by_time = {}
        for row in sr_data_qs:
            t_str = timezone.localtime(row['Time']).strftime('%H:%M:%S')
            row_s = _serialize_row(dict(row), t_str)
            # एक ही time पर multiple rows हों तो latest रखो
            sr_data_by_time[t_str] = row_s

        sr_sorted_times = sorted(sr_data_by_time.keys())   # ✅ Fix #2: sorted list

        # ── 3. Master Levels ─────────────────────────────────────────────────
        master_levels = get_master_levels(symbol, selected_date)

        # ── 4. रिवर्सल लाइनें — Forward Fill + per-tick lines ───────────────
        final_timeline  = {}
        sorted_oc_times = sorted(grouped_oc.keys())

        last_known_sr = {}
        sr_ptr        = 0                        # ✅ Fix #4: index pointer

        for t_str in sorted_oc_times:
            data = grouped_oc[t_str]

            # ✅ Fix #2 & #4: string comparison HH:MM:SS में सही है (24-hr format)
            while sr_ptr < len(sr_sorted_times) and sr_sorted_times[sr_ptr] <= t_str:
                last_known_sr = sr_data_by_time[sr_sorted_times[sr_ptr]]
                sr_ptr += 1

            current_lines = get_reversal_lines_for_replay(
                symbol, date_str, last_known_sr, data['chain']
            )

            final_timeline[t_str] = {
                'oc':    data,
                'sr':    last_known_sr,
                'lines': current_lines
            }

        # ── 5. Upstox Chart Data ─────────────────────────────────────────────
        instrument_key = get_instrument_key(symbol)
        candles = []
        if instrument_key:
            chart_res = fetch_candle_data(instrument_key, "minutes", "1", date_str, date_str)
            if chart_res and chart_res.get("success"):
                candles = parse_candles(chart_res["data"])

        return JsonResponse({
            'success':       True,
            'symbol':        symbol,
            'date':          date_str,
            'master_levels': master_levels,
            'timeline':      sorted_oc_times,
            'timeline_data': final_timeline,
            'candles':       candles
        })

    except Exception as e:
        # ✅ Fix #7 & #9: print की जगह logger.exception (full traceback भी log होगा)
        logger.exception(
            "market_replay_data_api failed | symbol=%s | date=%s", symbol, date_str
        )
        return JsonResponse({'error': str(e)}, status=500)


def get_reversal_lines_for_replay(symbol: str, date_str: str,
                                   sr_dict: dict, oc_list: list) -> list:
    """
    हर एक Tick के SR Data के अनुसार dynamically Master R और Master S निकालता है
    और सिर्फ काम की lines return करता है।
    """
    try:
        if not sr_dict or not oc_list:
            return []

        # ✅ Fix #5: Magic number हटाया, STEP_MAP use किया
        step = STEP_MAP.get(symbol, DEFAULT_STEP)

        # ✅ Fix #1 & #3: _safe_float से safe conversion
        spot = _safe_float(oc_list[0].get('Spot_Price', 0)) if oc_list else 0.0

        # ── Effective Resistance ─────────────────────────────────────────────
        res_status = str(sr_dict.get('resistance_status', '')).upper()
        res_base   = _safe_float(sr_dict.get('resistance_strike'))

        m_res      = re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
        res_target = float(m_res.group(1)) if m_res else res_base

        if   "SHIFTED WTT" in res_status: eff_res = res_base + step
        elif "SHIFTED WTB" in res_status: eff_res = res_base + step
        elif "WTT"         in res_status: eff_res = res_target - step
        elif "WTB"         in res_status: eff_res = res_target + step
        elif "STRONG"      in res_status: eff_res = res_base + step
        else:                             eff_res = res_base + step

        # ── Effective Support ────────────────────────────────────────────────
        sup_status = str(sr_dict.get('supprt_status', '')).upper()
        sup_base   = _safe_float(sr_dict.get('supprt_strike'))

        m_sup      = re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
        sup_target = float(m_sup.group(1)) if m_sup else sup_base

        if   "SHIFTED WTT" in sup_status: eff_sup = sup_base - step
        elif "SHIFTED WTB" in sup_status: eff_sup = sup_base - step
        elif "WTT"         in sup_status: eff_sup = sup_target - step
        elif "WTB"         in sup_status: eff_sup = sup_target + step
        elif "STRONG"      in sup_status: eff_sup = sup_base - step
        else:                             eff_sup = sup_base - step

        # Fallback: SR data न हो तो spot के हिसाब से set करो
        if eff_res == 0: eff_res = spot + step
        if eff_sup == 0: eff_sup = spot - step
        if res_base == 0: res_base = spot
        if sup_base == 0: sup_base = spot

        # ── Global Strike Range ──────────────────────────────────────────────
        all_levels  = [eff_res, res_base, eff_sup, sup_base]
        global_low  = min(all_levels) - step
        global_high = max(all_levels) + step

        lines    = []
        seen_ce  = set()
        seen_pe  = set()

        # ── FIRST PASS: Master R / S lines ───────────────────────────────────
        for row in oc_list:
            strike = _safe_float(row.get('Strike_Price', 0))     # ✅ Fix #1
            if strike < global_low or strike > global_high:
                continue

            if strike == eff_res:
                val = _safe_float(row.get('Reversl_Ce'))          # ✅ Fix #1
                if val > 0:
                    lines.append({
                        "price": val, "color": "#ff8c00",
                        "width": 3,  "label": f"R {strike:.0f}"
                    })
                    seen_ce.add(val)

            if strike == eff_sup:
                val = _safe_float(row.get('Reversl_Pe'))          # ✅ Fix #1
                if val > 0:
                    lines.append({
                        "price": val, "color": "#00bfff",
                        "width": 3,  "label": f"S {strike:.0f}"
                    })
                    seen_pe.add(val)

        # ── SECOND PASS: बाकी lines ──────────────────────────────────────────
        for row in oc_list:
            strike = _safe_float(row.get('Strike_Price', 0))      # ✅ Fix #1
            if strike < global_low or strike > global_high:
                continue

            if strike != eff_res:
                val = _safe_float(row.get('Reversl_Ce'))           # ✅ Fix #1
                if val > 0 and val >= spot and val not in seen_ce:
                    seen_ce.add(val)
                    lines.append({
                        "price": val, "color": "#f85149",
                        "width": 1,  "label": f"CE {strike:.0f}"
                    })

            if strike != eff_sup:
                val = _safe_float(row.get('Reversl_Pe'))           # ✅ Fix #1
                if val > 0 and val < spot and val not in seen_pe:
                    seen_pe.add(val)
                    lines.append({
                        "price": val, "color": "#3fb950",
                        "width": 1,  "label": f"P {strike:.0f}"
                    })

        lines.sort(key=lambda x: x["price"], reverse=True)
        return lines

    except Exception:
        # ✅ Fix #7 & #9: logger.exception — full traceback, extra context
        logger.exception(
            "get_reversal_lines_for_replay failed | symbol=%s | date=%s", symbol, date_str
        )
        return []
    
