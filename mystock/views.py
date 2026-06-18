# ── Standard Library ──────────────────────────────────────────────
import json
import logging
import math
import re
import time
import traceback
from datetime import date, datetime, timedelta, time as dt_time, timezone as dt_timezone

# ── Third-Party ───────────────────────────────────────────────────
import pytz
import requests
from requests.exceptions import ConnectionError, SSLError, Timeout

# ── Django ────────────────────────────────────────────────────────
from asgiref.sync import async_to_sync
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import caches
from django.db.models import Count, F, OuterRef, Q, Subquery, Sum
from django.db.models.functions import Abs
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.timezone import localtime
from django.views.decorators.cache import cache_page, never_cache
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

# ── Local ─────────────────────────────────────────────────────────
from .credentials import access_token
from .management.commands.async_live import (
    get_instrument_from_db,
    run_live_paper_trading,
    update_instrument_store_bulk,
)
from .models import (
    BotSettings,
    InstrumentStore,
    LiveSRData,
    OptionChain,
    PaperTrade,
    SupportResistance,
    SyncControl,
    TempOptionChain,
    TradeLevel,
    TradeStatus,
    TradeType,
    TradingJournal,
)
from .symbol import symbols as ALL_SYMBOLS
from .trade_logic import get_master_levels
from django.contrib.auth.decorators import user_passes_test
from django.views.decorators.http import require_POST
from django.contrib.auth.models import User
from django.contrib.auth.decorators import user_passes_test
from django.contrib import messages
from django.contrib.sessions.models import Session
from functools import wraps
from django.utils.module_loading import import_string
from django.conf import settings

import math

def sanitize_json_data(data):
    """
    यह फंक्शन पूरे डेटा को स्कैन करेगा और जहाँ भी Python का 'Infinity' या 'NaN' मिलेगा,
    उसे 'None' (JSON में null) में बदल देगा ताकि फ्रंटएंड क्रैश न हो।
    """
    if isinstance(data, dict):
        return {k: sanitize_json_data(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [sanitize_json_data(v) for v in data]
    elif isinstance(data, float):
        if math.isinf(data) or math.isnan(data):
            return None  # Infinity को null में बदलें
    return data

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
# ========================================================
# 🚀 SMART CACHE OVERRIDE (FAILOVER MECHANISM)
# ========================================================
class SmartCache:
    # def get(self, key, default=None):
    #     try:
    #         val = caches['default'].get(key)
    #         if val is not None:
    #             # 🟢 flush=True टर्मिनल को तुरंत प्रिंट दिखाने के लिए मजबूर करेगा
    #             # print(f"✅ Cache Hit in REDIS for key: {key}", flush=True)
    #             return val
    #         # print(f"⚠️ Cache Miss in REDIS for key: {key}", flush=True)
    #     except Exception as e:
    #         print(f"🔴 REDIS SET ERROR: {e}", flush=True)
    #         pass 
        
    #     try:
    #         val = caches['db_cache'].get(key)
    #         if val is not None:
    #             # print(f"✅ Cache Hit in DATABASE for key: {key}", flush=True)
    #             return val
    #         # print(f"⚠️ Cache Miss in DATABASE for key: {key}", flush=True)
    #     except Exception as e:
    #         print(f"🔴 DB CACHE GET ERROR: {e}", flush=True)
    #         pass
            
    #     return default

    # def set(self, key, value, timeout=None):
    #     try:
    #         caches['default'].set(key, value, timeout)
    #         # print(f"✅ Cache Set in Redis for key: {key}", flush=True)
    #     except Exception as e:
    #         print(f"🔴 REDIS SET ERROR ({key}): {e}", flush=True)
    #         # pass
    #     try:
    #         caches['db_cache'].set(key, value, timeout)
    #         # print(f"✅ Cache Set in Database for key: {key}", flush=True)
    #     except Exception as e:
    #         print(f"🔴 DB CACHE SET ERROR ({key}): {e}", flush=True)
    #         # pass

    def get(self, key, default=None):
        # ✅ पहले Redis try करो
        try:
            val = caches['default'].get(key)
            if val is not None:
                return val
            # Redis connected है, miss हुआ — DB try मत करो
            return default          # ← यही बदलाव है
        except Exception as e:
            # Redis ही down है — तब DB try करो
            print(f"🔴 REDIS DOWN, DB fallback: {e}", flush=True)

        try:
            return caches['db_cache'].get(key, default)
        except Exception as e:
            print(f"🔴 DB CACHE GET ERROR: {e}", flush=True)
            return default
        
    def set(self, key, value, timeout=None):
        # ✅ पहले Redis में set करें
        redis_ok = False
        try:
            caches['default'].set(key, value, timeout)
            redis_ok = True
        except Exception as e:
            print(f"🔴 REDIS SET ERROR ({key}): {e}", flush=True)

        # ✅ सिर्फ तब DB cache में set करें जब Redis fail हो (Fallback only)
        if not redis_ok:
            try:
                caches['db_cache'].set(key, value, timeout)
            except Exception as e:
                print(f"🔴 DB CACHE SET ERROR ({key}): {e}", flush=True)

cache = SmartCache()
# ========================================================
# ── 1. Admin Panel Page ─────────────────────────────────────
import asyncio
from functools import wraps

def admin_only(view_func):
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.is_superuser:
            return render(request, 'registration/403.html', status=403)
        return view_func(request, *args, **kwargs)

    # async views के लिए
    @wraps(view_func)
    async def _async_wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect('login')
        if not request.user.is_superuser:
            return render(request, 'registration/403.html', status=403)
        return await view_func(request, *args, **kwargs)

    if asyncio.iscoroutinefunction(view_func):
        return _async_wrapped_view
    return _wrapped_view

@admin_only
def admin_panel_view(request):
    """Admin control panel — सिर्फ page render करता है, data JS से आता है।"""
    return render(request, 'mystock/admin_panel.html')

from django.contrib.auth import authenticate, login

def login_view(request):
    # अगर यूजर पहले से लॉगिन है, तो उसे सीधे सही पेज पर भेज दें
    if request.user.is_authenticated:
        if request.user.is_superuser:
            return redirect('admin_panel') 
        else:
            return redirect('dashboard') 

    if request.method == 'POST':
        username_or_email = request.POST.get('username')
        passw = request.POST.get('password')

        user = authenticate(request, username=username_or_email, password=passw)

        if user is not None:
            if user.is_active:
                
                # 🚀 --- बुलेटप्रूफ 'किक-आउट' (Kick-out) लॉजिक ---
                # नए लॉगिन से पहले इस यूज़र के पुराने सभी एक्टिव सेशन्स को डिलीट करें
                active_sessions = Session.objects.filter(expire_date__gt=timezone.now())
                for session in active_sessions:
                    try:
                        session_data = session.get_decoded()
                        if str(user.pk) == str(session_data.get('_auth_user_id')):
                            session.delete()
                    except Exception:
                        pass
                # 🚀 -------------------------------------------

                # अब नए डिवाइस/ब्राउज़र में सुरक्षित लॉगिन करें
                login(request, user)
                
                next_url = request.POST.get('next')
                if next_url:
                    return redirect(next_url)

                if user.is_superuser:
                    return redirect('admin_panel')
                else:
                    return redirect('dashboard')
            else:
                messages.error(request, "आपका अकाउंट अभी एडमिन द्वारा एक्टिवेट नहीं किया गया है।")
        else:
            messages.error(request, "यूज़रनेम/ईमेल या密码 गलत है।")

    return render(request, 'registration/login.html')

# ── 2. Admin Status API ─────────────────────────────────────
@admin_only                          # ← Security fix: पहले यह था ही नहीं!
def admin_status_api(request):
    """सभी loops का status + trade stats + Bot Settings"""

    CACHE_KEY = 'admin_status_api_v1'
    cached = cache.get(CACHE_KEY)
    if cached:
        return JsonResponse(cached)   # ← Cache hit: ~0.01s

    # ── 1. Loop Status ──
    loop_names = ['nifty_loop', 'others_loop', 'bot_loop']
    try:
        existing = {c.name: c.is_active 
                    for c in SyncControl.objects.filter(name__in=loop_names)}
        for name in loop_names:
            if name not in existing:
                ctrl, _ = SyncControl.objects.get_or_create(
                    name=name, defaults={'is_active': True})
                existing[name] = ctrl.is_active
    except Exception:
        existing = {name: False for name in loop_names}

    today = timezone.now().date()

    # ── 2. Trade Stats ──
    stats_qs = (PaperTrade.objects
        .filter(trade_date=today)
        .exclude(result='SKIPPED')
        .aggregate(
            total=Count('id'),
            wins=Count('id', filter=Q(result='TARGET') | Q(result='MANUAL_EXIT', pnl__gt=0)),
            losses=Count('id', filter=Q(result='SL') | Q(result='MANUAL_EXIT', pnl__lt=0)),
            pnl=Sum('pnl')
        ))

    # ── 3. Spot Price — पहले Cache से, फिर DB से (INDEX-friendly query) ──
    current_spot = cache.get('live_nifty_spot_NIFTY')    # bot loop यह set करता है

    if not current_spot:
        # ✅ Time__date की जगह __gte/__lte range — INDEX use होगा
        day_start = timezone.make_aware(datetime.combine(today, dt_time.min))
        latest_oc = (OptionChain.objects
            .filter(Symbol='NIFTY', Time__gte=day_start)  # ← Fast index scan
            .only('Spot_Price')
            .order_by('-Time')
            .first())
        current_spot = float(latest_oc.Spot_Price) if latest_oc else None

    # ── 4. Bot Settings — Cache करें ──
    settings_cache_key = 'bot_settings_v1'
    settings_data = cache.get(settings_cache_key)
    if not settings_data:
        settings, _ = BotSettings.objects.get_or_create(id=1)
        settings_data = {
            'target':    settings.default_target,
            'sl':        settings.default_sl,
            'buffer':    settings.reversal_buffer,
            'user_name': getattr(settings, 'user_name', 'बॉस'),
        }
        cache.set(settings_cache_key, settings_data, 60)  # 1 मिनट cache

    # ── 5. Response build ──
    response_data = {
        'loops': existing,
        'stats': {
            'total':  stats_qs['total']  or 0,
            'wins':   stats_qs['wins']   or 0,
            'losses': stats_qs['losses'] or 0,
            'pnl':    round(stats_qs['pnl'] or 0, 2),
            'spot':   current_spot,
        },
        'settings': settings_data,
    }

    cache.set(CACHE_KEY, response_data, 3)  # 3 सेकंड cache (polling rate से match)
    return JsonResponse(response_data)

# 2. यह नया फंक्शन सबसे नीचे जोड़ दें:
# @login_required
@admin_only
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
            if 'user_name' in data: settings.user_name = str(data['user_name'])
            
            settings.save()
            return JsonResponse({'status': 'success', 'msg': 'Settings Updated Successfully!'})
        except Exception as e:
            return JsonResponse({'status': 'error', 'msg': str(e)})
    return JsonResponse({'status': 'invalid method'})
# @login_required
@admin_only
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

# @user_passes_test(is_superuser_check, login_url='/dashboard/')
@admin_only
def user_approval_list(request):
    """सभी नॉर्मल यूज़र्स की लिस्ट दिखाने के लिए (सुपरयूज़र को छोड़कर)"""
    # नए रजिस्टर हुए यूज़र्स सबसे ऊपर दिखेंगे
    managed_users = User.objects.filter(is_superuser=False).order_by('-date_joined')
    return render(request, 'registration/user_approval.html', {'managed_users': managed_users})

# @user_passes_test(is_superuser_check, login_url='/dashboard/')
@admin_only
@require_POST
def toggle_user_status(request, user_id):
    """यूज़र का स्टेटस बदलने के लिए (Active / Inactive)"""
    user_to_modify = get_object_or_404(User, id=user_id)
    
    # स्टेटस को टॉगल (उल्टा) करें
    user_to_modify.is_active = not user_to_modify.is_active
    user_to_modify.save()
    
    status_str = "एक्टिवेट (Approved)" if user_to_modify.is_active else "इनएक्टिवेट (Deactivated)"
    messages.success(request, f"यूज़र {user_to_modify.first_name} ({user_to_modify.email}) को सफलतापूर्वक {status_str} कर दिया गया है।")
    
    return redirect('user_approval_list')

# यूजर रजिस्ट्रेशन के लिए एक नया view function:
def register_user(request):
    if request.method == 'POST':
        name = request.POST.get('name')
        email = request.POST.get('email')
        password = request.POST.get('password')

        # चेक करें कि ईमेल पहले से मौजूद तो नहीं है
        if User.objects.filter(username=email).exists():
            messages.error(request, 'यह ईमेल पहले से रजिस्टर्ड है।')
            return redirect('register_user') # अपने URL का नाम डालें

        # यूजर बनाएँ, लेकिन उसे inactive रखें (is_active=False)
        user = User.objects.create_user(
            username=email, 
            email=email, 
            password=password, 
            first_name=name
        )
        user.is_active = False  # ⚠️ इसे False रखने से बिना एडमिन के लॉगिन नहीं होगा
        user.save()

        messages.success(request, 'रजिस्ट्रेशन सफल! एडमिन के अप्रूवल का इंतज़ार करें।')
        return redirect('login') # अपने लॉगिन पेज के URL का नाम डालें

    return render(request, 'registration/register.html')


# Constants
TIME_WINDOW = timedelta(seconds=1)
PAST_WINDOW = timedelta(seconds=2)

def calculate_percentage(change, max_change):
    return (change / max_change) * 100 if max_change > 0 and change else 0

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
    """
    # 🟢 सीधे मेमोरी (Cache) से लाइव डेटा उठाएं जो Async लूप ने सेव किया है
    live_data = cache.get(f'live_nifty_data_{symbol}')
    spot_price = cache.get(f'live_nifty_spot_{symbol}')

    if live_data and spot_price:
        # print(f"🟢 SUCCESS: {symbol} Data served from Async Cache (0 DB Queries)")
        
        latest_time = live_data[0].get('Time') 
        expiry_date = live_data[0].get('expiry') or live_data[0].get('Expiry_Date')
        
        
        # 🚀 === NEW FIX: अगर Redis से String आई है, तो उसे Date में बदलें === 🚀
        if isinstance(expiry_date, str):
            from datetime import datetime
            try:
                # "YYYY-MM-DD" फॉर्मेट को Date ऑब्जेक्ट में बदलें
                expiry_date = datetime.strptime(expiry_date, '%Y-%m-%d').date()
            except Exception:
                pass

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
        return result
    
    else:
        # 🔴 Fallback: जब Cache खाली हो तब Database से डेटा लाएं
        # print(f"🔴 MISS: {symbol} Cache Empty! Fetching from Database (Fallback)")
        
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
        
        from datetime import timedelta
        TIME_WINDOW = timedelta(seconds=1)
        all_data = list(
            OptionChain.objects.filter(
                Symbol=symbol,
                Time__range=(latest_time - TIME_WINDOW, latest_time + TIME_WINDOW)
            ).only(*NEEDED_FIELDS).order_by('Strike_Price')
        )

        all_metrics = [
            'CE_OI_percent', 'CE_Volume_percent', 'CE_COI_percent',
            'PE_OI_percent', 'PE_Volume_percent', 'PE_COI_percent'
        ]
        for metric in all_metrics:
            apply_ranking_styles(all_data, metric)

        display_data = []
        if all_data:
            closest_idx = min(range(len(all_data)), key=lambda i: abs(all_data[i].Strike_Price - spot_price))
            display_data = all_data[max(0, closest_idx - 15): min(len(all_data), closest_idx + 16)]
            
            for row in display_data:
                if row.Strike_Price > spot_price:
                    row.is_spot_divider = True
                    break

        result = (latest_time, spot_price, expiry_date, display_data, all_data)
        return result


@login_required
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
@login_required
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
    
@login_required
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
@login_required
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
@login_required
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

# 1. API View (जो सिर्फ डेटा देगा)
def specific_strike_ltp_data(request):
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
            "ce_ltp": entry.CE_LTP,
            "pe_ltp": entry.PE_LTP,
            "ce_cltp": entry.CE_CLTP, # यहाँ मॉडल के हिसाब से फील्ड नाम चेक कर लें
            "pe_cltp": entry.PE_CLTP,
        }

    # 4. Forward Fill Logic (पिछली वैल्यू भरना)
    ce_ltp_list, pe_ltp_list, ce_cltp_list, pe_cltp_list = [], [], [], []
    
    last_val = None # पिछली उपलब्ध वैल्यू स्टोर करने के लिए
    found_first_data = False

    for t in master_times:
        if t in data_map:
            # नया डेटा मिला, इसे सेव करें और लिस्ट में डालें
            last_val = data_map[t]
            found_first_data = True
            ce_ltp_list.append(last_val["ce_ltp"])
            pe_ltp_list.append(last_val["pe_ltp"])
            ce_cltp_list.append(last_val["ce_cltp"])
            pe_cltp_list.append(last_val["pe_cltp"])
        
        elif found_first_data and t <= latest_db_time:
            # बीच में डेटा गायब है, तो पिछली वैल्यू (Last Known) भरें
            ce_ltp_list.append(last_val["ce_ltp"])
            pe_ltp_list.append(last_val["pe_ltp"])
            ce_cltp_list.append(last_val["ce_cltp"])
            pe_cltp_list.append(last_val["pe_cltp"])
        
        else:
            # 9:15 से पहले या भविष्य के समय के लिए None भेजें
            ce_ltp_list.append(None)
            pe_ltp_list.append(None)
            ce_cltp_list.append(None)
            pe_cltp_list.append(None)

    return JsonResponse({
        "times": master_times,
        "ce_ltp": ce_ltp_list,
        "pe_ltp": pe_ltp_list,
        "ce_cltp": ce_cltp_list,
        "pe_cltp": pe_cltp_list,
    })

# 2. अपने चार्ट पेज वाले फंक्शन के ऊपर यह लाइन लिखें
@xframe_options_exempt
def render_chart_page_ltp(request):
    return render(request, 'mystock/ltp_chart_js.html', {
        'symbol': request.GET.get('symbol'),
        'strike': request.GET.get('strike')
    })





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
    from datetime import date as _date
    today_str = _date.today().isoformat()
    cache_key = f"final_rev_lines_v4_{symbol}_{from_date}_{to_date}"

    cached_lines = cache.get(cache_key)
    if cached_lines is not None:
        return cached_lines

    try:
        step = 100 if 'BANKNIFTY' in symbol or 'SENSEX' in symbol else 50
        def valid(v): return v is not None and not math.isnan(float(v)) and not math.isinf(float(v))

        day_start = timezone.make_aware(datetime.combine(
            datetime.strptime(from_date, '%Y-%m-%d').date(), dt_time.min))
        day_end   = timezone.make_aware(datetime.combine(
            datetime.strptime(to_date,   '%Y-%m-%d').date(), dt_time.max))

        # =======================================================
        # 1. 🟢 मास्टर S/R स्ट्राइक निकालना (trade_logic से, सही logic)
        #    पुराना sr_timeline loop हटाया — वो simplified था और mismatch देता था
        # =======================================================
        latest_sr = LiveSRData.objects.filter(
            Symbol=symbol, Time__gte=day_start, Time__lte=day_end
        ).order_by('-Time').first()

        if not latest_sr:
            return []

        master_levels = get_master_levels(symbol, day_start.date())
        if not master_levels:
            return []

        # ✅ FIX 1: eff_res_strike और eff_sup_strike सिर्फ master_levels से लो
        # (trade_logic.py का full 25+ condition वाला logic यहाँ पहले से run हो चुका है)
        # 🚀 THE FIX: यहाँ float() लगाना बहुत ज़रूरी है ताकि आगे TypeError न आए
        eff_res_strike = float(master_levels["R"]["strike"] or 0)
        eff_sup_strike = float(master_levels["S"]["strike"] or 0)
        # print(f"🔹 {symbol} | Master Levels: R={eff_res_strike}, S={eff_sup_strike}")

        # ✅ इसे भी float में कर लें ताकि एकदम सुरक्षित रहे
        current_res_val = float(master_levels["R"]["entry"] or 0)
        current_sup_val = float(master_levels["S"]["entry"] or 0)
        # print(f"🔹 {symbol} | Master Levels: R_val={current_res_val}, S_val={current_sup_val}")

        # DB query range: effective strikes के आसपास की सभी strikes
        global_low  = min(eff_sup_strike, eff_res_strike) - step
        global_high = max(eff_sup_strike, eff_res_strike) + step

        # =======================================================
        # 2. 🟢 आसपास की strikes की Latest Reversal Value निकालना
        #    (secondary CE/PE lines के लिए)
        # =======================================================
        strike_history = {}
        spot_price = None

        if from_date == today_str:
            # 🚀 Redis से पढ़ें (0 DB Query)
            history_key = f"moving_history_all_{symbol.upper()}"
            redis_data = cache.get(history_key)
            if redis_data:
                for k, v in redis_data.items():
                    try:
                        s = float(k)
                        if global_low <= s <= global_high:
                            # Redis में ce_hist/pe_hist का last value ही latest है
                            ce_hist = v.get('ce_hist', [])
                            pe_hist = v.get('pe_hist', [])

                            # 🛡️ FIX: Redis से आए float को Infinity/NaN से बचाएं
                            def _safe(val):
                                try:
                                    f = float(val)
                                    return f if math.isfinite(f) else None
                                except (TypeError, ValueError):
                                    return None

                            strike_history[s] = {
                                'latest_ce': _safe(ce_hist[-1]['value']) if ce_hist else _safe(v.get('latest_ce')),
                                'latest_pe': _safe(pe_hist[-1]['value']) if pe_hist else _safe(v.get('latest_pe')),
                            }
                    except (ValueError, TypeError, KeyError):
                        continue
                spot_price = cache.get(f'live_nifty_spot_{symbol.upper()}') or 0

        # अगर Redis खाली है या पुरानी डेट है → Database से लें
        if not strike_history:
            oc_qs = OptionChain.objects.filter(
                Symbol=symbol, Time__gte=day_start, Time__lte=day_end,
                Strike_Price__gte=global_low, Strike_Price__lte=global_high
            ).values('Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe').order_by('Time')

            for row in oc_qs:
                s = float(row['Strike_Price'])
                if s not in strike_history:
                    strike_history[s] = {'latest_ce': None, 'latest_pe': None}

                if row.get('Spot_Price'):
                    spot_price = float(row['Spot_Price'])

                rev_ce = row.get('Reversl_Ce')
                if valid(rev_ce):
                    strike_history[s]['latest_ce'] = float(rev_ce)

                rev_pe = row.get('Reversl_Pe')
                if valid(rev_pe):
                    strike_history[s]['latest_pe'] = float(rev_pe)

        # =======================================================
        # 3. 🎨 फाइनल लाइनें जनरेट करना
        # =======================================================
        lines = []
        seen_ce, seen_pe = set(), set()

        # ✅ मुख्य R लाइन — master_levels का सही entry value
        if current_res_val > 0:
            lines.append({
                "price": current_res_val,
                "strike": eff_res_strike,
                "type": "CE",
                "color": "#ff8c00",
                "width": 4,
                "dash": 0,
                "label": f"R {eff_res_strike:.0f}",
            })
            seen_ce.add(current_res_val)

        # ✅ मुख्य S लाइन — master_levels का सही entry value
        if current_sup_val > 0:
            lines.append({
                "price": current_sup_val,
                "strike": eff_sup_strike,
                "type": "PE",
                "color": "#00bfff",
                "width": 4,
                "dash": 0,
                "label": f"S {eff_sup_strike:.0f}",
            })
            seen_pe.add(current_sup_val)

        # Secondary CE/PE lines (spot के आसपास की बाकी strikes)
        for strike, data in sorted(strike_history.items()):
            if not spot_price:
                continue
            is_top    = (strike == eff_res_strike)
            is_bottom = (strike == eff_sup_strike)

            latest_ce = data.get('latest_ce')
            if latest_ce is not None and latest_ce not in seen_ce:
                if latest_ce >= spot_price or is_top:
                    seen_ce.add(latest_ce)
                    lines.append({
                        "price": latest_ce,
                        "strike": float(strike),
                        "type": "CE",
                        "color": "#f85149",
                        "width": 1,
                        "dash": 0,
                        "label": f"CE {strike:.0f}",
                    })

            latest_pe = data.get('latest_pe')
            if latest_pe is not None and latest_pe not in seen_pe:
                if latest_pe < spot_price or is_bottom:
                    seen_pe.add(latest_pe)
                    lines.append({
                        "price": latest_pe,
                        "strike": float(strike),
                        "type": "PE",
                        "color": "#3fb950",
                        "width": 1,
                        "dash": 0,
                        "label": f"P {strike:.0f}",
                    })

        lines.sort(key=lambda x: x["price"], reverse=True)

        # 🛡️ FIX: Cache में store करने और return करने से पहले Infinity/NaN साफ़ करें
        lines = sanitize_json_data(lines)

        timeout = 45 if from_date == today_str else 86400
        cache.set(cache_key, lines, timeout=timeout)

        return lines

    except Exception as e:
        import traceback
        print(f"Reversal lines error: {e}")
        traceback.print_exc()
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
@login_required
@xframe_options_exempt
def chart_view(request):
    # आज की तारीख (YYYY-MM-DD फॉर्मेट में)
    today_str = date.today().isoformat()

    symbol    = request.GET.get("symbol", "NIFTY").strip().upper()
    unit      = request.GET.get("unit", "minutes")
    interval  = request.GET.get("interval", "5")
    
    # अगर URL में डेट नहीं है, तो आज की डेट (today_str) का उपयोग करें
    from_date = request.GET.get("from_date", today_str)
    to_date   = request.GET.get("to_date", today_str)
    
    context = {
        "symbol":         symbol,
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
    today_str = date.today().isoformat()

    symbol    = request.GET.get("symbol",    "").strip().upper()
    unit      = request.GET.get("unit",      "minutes")
    interval  = request.GET.get("interval",  "5")
    from_date = request.GET.get("from_date", today_str)
    to_date   = request.GET.get("to_date",   today_str)
    show_reversal = request.GET.get("reversal", "1") != "0"

    if not symbol:
        return JsonResponse({"error": "symbol parameter जरूरी है।"}, status=400)

    cache_key = f"upstox_chart_{symbol}_{unit}_{interval}_{from_date}_{to_date}"

    cached_data = cache.get(cache_key)
    if cached_data:
        if show_reversal:
            cached_data = dict(cached_data)
            cached_data["reversal_lines"] = get_reversal_lines(symbol, from_date, to_date)
        print(f"⚡ FAST CHART: {symbol} ({interval}m) served from CACHE 🚀")
        return JsonResponse(cached_data)

    print(f"🔴 MISS: {symbol} ({interval}m) Fetching from UPSTOX 🌐")

    instrument_key = get_instrument_key(symbol)
    if not instrument_key:
        return JsonResponse({"error": f"'{symbol}' symbol DB में नहीं मिला।"}, status=404)

    result = fetch_candle_data(instrument_key, unit, interval, to_date, from_date)
    if not result["success"]:
        return JsonResponse({"error": result["error"]}, status=400)

    candles = parse_candles(result["data"])
    reversal_lines = get_reversal_lines(symbol, from_date, to_date) if show_reversal else []

    response_data = {
        "symbol":         symbol,
        "instrument_key": instrument_key,
        "interval":       interval,
        "unit":           unit,
        "from_date":      from_date,
        "to_date":        to_date,
        "count":          len(candles),
        "candles":        candles,
        "reversal_lines": reversal_lines,
    }

    # 🟢 BACKEND FIX: JSON में भेजने से पहले Infinity को साफ़ करें
    response_data = sanitize_json_data(response_data)

    candles_only = {k: v for k, v in response_data.items() if k != "reversal_lines"}

    # ✅ FIX: Empty candles को cache मत करो
    if len(candles) == 0:
        print(f"⚠️ SKIP CACHE: {symbol} — candles empty, not caching.")
        return JsonResponse(response_data)

    if from_date == today_str:
        current_time = datetime.now().time()
        market_open_time = dt_time(9, 15)   # ✅ NEW: Market open time
        market_end_time  = dt_time(15, 30)

        if current_time < market_open_time:
            # ✅ Pre-market: बहुत कम cache (जल्दी refresh होगा)
            cache_timeout = 30
            time_msg = "30 seconds (Pre-Market)"

        elif current_time > market_end_time:
            cache_timeout = 28800
            time_msg = "8 hours (Market Closed)"

        else:
            # ✅ Live Market: 30s cache — refresh interval से आधा रखो
            cache_timeout = 50
            time_msg = "50 seconds (Live Market)"

        cache.set(cache_key, candles_only, timeout=cache_timeout)
        print(f"✅ CACHED: {symbol} ({interval}m) for {time_msg}.")
    else:
        cache.set(cache_key, candles_only, timeout=86400)
        print(f"✅ CACHED: {symbol} ({interval}m) Historical for 24h.")

    return JsonResponse(response_data)



# ─────────────────────────────────────────────
# View 3: Symbol Autocomplete Search
# ─────────────────────────────────────────────
@login_required
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


@login_required
@xframe_options_exempt
def dashboard_chart_view(request):
    """
    यह व्यू अब सिर्फ खाली HTML शेल रेंडर करेगा।
    डेटा लोड करने का सारा काम JS (AJAX) करेगा जिससे पेज इंस्टेंट खुलेगा।
    """
    today = date.today()
    symbol    = request.GET.get("symbol", "NIFTY").strip().upper()
    unit      = request.GET.get("unit", "minutes")
    interval  = request.GET.get("interval", "5")
    
    req_from  = request.GET.get("from_date")
    req_to    = request.GET.get("to_date")
    
    from_date = req_from if req_from else today.isoformat()
    to_date   = req_to if req_to else today.isoformat()

    # बस पैरामीटर्स पास कर रहे हैं, कोई API या DB Call नहीं!
    context = {
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

"""""
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
"""
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
@login_required
def resistance_dashboard(request):
    return render(request, "mystock/resistance_dashboard.html")

# Dashboard के लिए एक अलग व्यू जो Support और Resistance दोनों दिखाएगा। 
# यह व्यू एक HTML पेज रेंडर करेगा जिसमें एक कैलेंडर होगा, जिससे यूज़र किसी भी दिन का डेटा देख सकेगा। 
# डेटाबेस से डेटा फ़िल्टर करने के लिए चुनी गई तारीख का उपयोग किया जाएगा।
@login_required
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
    
    return render(request, 'mystock/sr_data_page.html', context)


@login_required
def live_trades_view(request):
    symbol = request.GET.get('symbol', 'NIFTY').upper()
    
    # ✅ हमेशा आज की date
    today = timezone.now().date()
    day_start = timezone.make_aware(datetime.combine(today, dt_time.min))
    day_end   = timezone.make_aware(datetime.combine(today, dt_time.max))

    trades = PaperTrade.objects.filter(
        symbol=symbol, trade_date=today
    ).order_by('-entry_time')

    total_trades = trades.count()
    wins   = trades.filter(result='TARGET').count()
    losses = trades.filter(result='SL').count()
    net_pnl = trades.exclude(result='SKIPPED').aggregate(total=Sum('pnl'))['total'] or 0.0

    # Cache से Spot Price
    current_spot = cache.get(f'live_nifty_spot_{symbol}')
    if not current_spot:
        latest_oc = OptionChain.objects.filter(
            Symbol=symbol, Time__gte=day_start, Time__lte=day_end
        ).only('Spot_Price', 'Time').order_by('-Time').first()
        current_spot = latest_oc.Spot_Price if latest_oc else None

    settings, _ = BotSettings.objects.get_or_create(id=1)
    db_user_name = getattr(settings, 'user_name', 'बॉस')

    context = {
        'trades': trades,
        'symbol': symbol,
        'selected_date': today.strftime('%Y-%m-%d'),
        'total_trades': total_trades,
        'wins': wins,
        'losses': losses,
        'net_pnl': round(net_pnl, 2),
        'spot': current_spot,
        'r_level': None,
        's_level': None,
        'abs_dist_r': None,
        'abs_dist_s': None,
        'dir_r': '',
        'dir_s': '',
        'is_r_closer': False,
        'user_name': db_user_name,
    }

    return render(request, 'mystock/live_trades.html', context)


from asgiref.sync import sync_to_async
from django.http import JsonResponse

@login_required
async def dashboard_data_api(request):
    """
    Async wrapper — ASGI (Daphne) server में sync DB calls को
    thread pool में चलाता है ताकि event loop block न हो।
    """
    return await sync_to_async(_dashboard_data_api_sync, thread_sensitive=True)(request)


def _dashboard_data_api_sync(request):
    symbol = request.GET.get('symbol', 'NIFTY').upper()
    date_str = request.GET.get('date')

    # 🚀 1. कैश की (Cache Key) बनाएँ
    cache_key = f"dashboard_api_{symbol}_{date_str}"
    
    # 🚀 2. चेक करें कि क्या डेटा पहले से कैश में है?
    cached_data = cache.get(cache_key)
    if cached_data:
        return JsonResponse(sanitize_json_data(cached_data)) # ✅ Infinity fix: cache hit पर भी sanitize
    
    if date_str:
        selected_date = timezone.datetime.strptime(date_str, '%Y-%m-%d').date()
    else:
        selected_date = timezone.now().date()

    day_start = timezone.make_aware(datetime.combine(selected_date, dt_time.min))
    day_end   = timezone.make_aware(datetime.combine(selected_date, dt_time.max))

    # 🚀 3. Latest Spot Price (Super Fast Approach)
    current_spot = cache.get(f'live_nifty_spot_{symbol}')
    latest_oc = None  # UnboundLocalError से बचने के लिए इसे None सेट किया गया है
    
    if not current_spot:
        print(f"Cache में Spot Price नहीं मिला, DB से ले रहे हैं... ({symbol} {selected_date})")
        latest_oc = OptionChain.objects.filter(
            Symbol=symbol, Time__gte=day_start, Time__lte=day_end
        ).only('Spot_Price', 'Time').order_by('-Time').first()
        current_spot = latest_oc.Spot_Price if latest_oc else None

    # 4. MASTER LEVELS
    master_levels = get_master_levels(symbol, selected_date)
    step = 100 if 'BANKNIFTY' in symbol or 'SENSEX' in symbol else 50

    # 5. Trades Query
    trades_qs = PaperTrade.objects.filter(
        symbol=symbol, trade_date=selected_date
    ).order_by('-entry_time')

    
    total_pnl = 0.0
    total_pnl_rupees = 0.0 
    trades_list = []

    # 🟢 1. InstrumentStore से डायनामिक लॉट साइज़ निकालें
    try:
        inst = InstrumentStore.objects.get(symbol=symbol)
        dynamic_lot_size = inst.lot_size
    except InstrumentStore.DoesNotExist:
        # 🟢 डिक्शनरी का उपयोग करें
        LOT_DEFAULTS = {'NIFTY': 65, 'BANKNIFTY': 30, 'FINNIFTY': 60, 'MIDCPNIFTY': 120}
        dynamic_lot_size = LOT_DEFAULTS.get(symbol, 65)  # अगर किसी वजह से DB में न मिले, तो डिफ़ॉल्ट 65 रखें

    for tr in trades_qs:
        current_pnl = float(tr.pnl) if tr.pnl else 0.0
        
        # 🟢 2. अगर ट्रेड में लॉट साइज़ सेव है तो वो लें, वरना InstrumentStore वाला असली लॉट साइज़ लें
        trade_lot_size = tr.lot_size if getattr(tr, 'lot_size', None) else dynamic_lot_size

        if tr.result == 'OPEN' and current_spot:
            if tr.trade_type == 'PUT':
                current_pnl = float(tr.entry_spot) - float(current_spot)
            elif tr.trade_type == 'CALL':
                current_pnl = float(current_spot) - float(tr.entry_spot)
        
       
                
        # 🟢 रुपयों में कैलकुलेशन
        if tr.result == 'OPEN':
            current_pnl_rupees = current_pnl * trade_lot_size
        else:
            current_pnl_rupees = float(tr.pnl_rupees) if getattr(tr, 'pnl_rupees', None) else (current_pnl * trade_lot_size)

        total_pnl += current_pnl
        total_pnl_rupees += current_pnl_rupees # 👈 टोटल रुपयों में जोड़ें

        trade_side = "R" if tr.trade_type == "PUT" else "S"
        entry_strike = float(tr.entry_strike) if tr.entry_strike else None

        # ✅ SUPER OPTIMIZATION: क्लोज़्ड ट्रेड के लिए बार-बार लाइव डेटा नहीं निकालना है
        trade_target = None
        trade_sl = None

        if tr.result == 'OPEN':
            # 🟢 सिर्फ OPEN ट्रेड के लिए लाइव Cache/DB से डेटा निकालें
            if entry_strike:
                Buffer = 10
                if tr.trade_type == 'PUT':
                    trade_target = get_rev_val_for_dashboard(symbol, selected_date, entry_strike - step, 'CE')
                    trade_target = trade_target + Buffer if trade_target else None
                    trade_sl     = get_rev_val_for_dashboard(symbol, selected_date, entry_strike + step, 'CE')
                    trade_sl = trade_sl - Buffer if trade_sl else None
                else:
                    trade_target = get_rev_val_for_dashboard(symbol, selected_date, entry_strike + step, 'PE')
                    trade_target = trade_target - Buffer if trade_target else None
                    trade_sl     = get_rev_val_for_dashboard(symbol, selected_date, entry_strike - step, 'PE')
                    trade_sl = trade_sl + Buffer if trade_sl else None
                
                # Fallback: अगर लाइव वैल्यू न मिले
                if trade_target is None:
                    trade_target = master_levels[trade_side]["target"]
                if trade_sl is None:
                    trade_sl = master_levels[trade_side]["sl"]
        else:
            # 🔴 CLOSED ट्रेड के लिए: सीधे ट्रेड की एंट्री प्राइस से फिक्स (स्टैटिक) टारगेट/SL निकाल लें 
            if tr.trade_type == 'PUT':
                trade_target = float(tr.entry_spot) - step
                trade_sl     = float(tr.entry_spot) + step
            else:
                trade_target = float(tr.entry_spot) + step
                trade_sl     = float(tr.entry_spot) - step

        
        if trade_target is None:
            trade_target = (float(tr.entry_spot) - step) if tr.trade_type == 'PUT' else (float(tr.entry_spot) + step)
        if trade_sl is None:
            trade_sl = (float(tr.entry_spot) + step) if tr.trade_type == 'PUT' else (float(tr.entry_spot) - step)

        trades_list.append({
            'type': tr.trade_type,
            'entry_time': localtime(tr.entry_time).strftime('%H:%M:%S') if tr.entry_time else '—',
            'trigger_level': tr.trigger_level,
            'trigger_price': round(float(tr.trigger_price), 2) if tr.trigger_price else 0,
            'entry_spot': round(float(tr.entry_spot), 2) if tr.entry_spot else 0,
            'exit_time': localtime(tr.exit_time).strftime('%H:%M:%S') if tr.exit_time else '—',
            'exit_spot': round(float(tr.exit_spot), 2) if tr.exit_spot else None,
            'result': tr.result,
            'pnl': round(current_pnl, 2),
            'pnl_rupees': round(current_pnl_rupees, 2), # 👈 JSON में रुपये भेजें
            'target': round(trade_target, 2),
            'sl': round(trade_sl, 2),
            'entry_strike': tr.entry_strike,
        })

    # 6. Bot Status
    try:
        ctrl, _ = SyncControl.objects.get_or_create(name="bot_loop")
        bot_active = ctrl.is_active
    except Exception:
        bot_active = False

    
    # 🚀 7. रिस्पॉन्स डेटा
    response_data = {
        'server_time': localtime(timezone.now()).strftime('%H:%M:%S'),
        'bot_active': bot_active,
        'total_pnl': round(total_pnl, 2),
        'total_pnl_rupees': round(total_pnl_rupees, 2),
        'triggers': {
            'spot': current_spot,
            'r_trigger': master_levels["R"]["entry"],
            'r_strike': master_levels["R"]["strike"],
            'r_status': master_levels["R"]["status"] or '—',
            's_trigger': master_levels["S"]["entry"],
            's_strike': master_levels["S"]["strike"],
            's_status': master_levels["S"]["status"] or '—',
            'data_time': latest_oc.Time.isoformat() if latest_oc else localtime(timezone.now()).isoformat(),
        },
        'trades': trades_list,
    }

    # 🟢 BACKEND FIX: JSON में भेजने से पहले Infinity को साफ़ करें
    response_data = sanitize_json_data(response_data)

    # 🚀 8. डायनामिक कैश टाइमिंग (Smart Caching)
    has_open_trade = any(t['result'] == 'OPEN' for t in trades_list)
    cache_timeout = 2 if has_open_trade else 5 
    
    cache.set(cache_key, response_data, cache_timeout)
    
    return JsonResponse(response_data)


def get_rev_val_for_dashboard(symbol, selected_date, strike, side):
    """
    Dashboard के लिए किसी specific strike की reversal value निकालना।
    (Optimized & Bug-Free Version)
    """
    symbol_upper = symbol.upper()
    cache_key = f"rev_val_{symbol_upper}_{selected_date}_{strike}_{side}"
    
    cached_val = cache.get(cache_key)
    if cached_val is not None:
        return cached_val

    # ✅ Bug #4 Fix: try block से बाहर
    today = timezone.now().date()
    result = None

    try:
        if selected_date == today:
            history_key = f"moving_history_all_{symbol_upper}"
            # ✅ Bug #1 Fix: SmartCache इस्तेमाल करें
            history_data = cache.get(history_key)
            
            if history_data:
                strike_float = float(strike)
                if strike_float in history_data:
                    hist_key = 'ce_hist' if side == 'CE' else 'pe_hist'
                    full_hist = history_data[strike_float].get(hist_key, [])
                    for tick in reversed(full_hist):
                        val = float(tick.get('value', 0))
                        if val > 0:
                            result = round(val, 2)
                            break
                            
        if result is None:
            print(f"🔴 DB HIT for {symbol_upper} {strike} {side}")
            col_name = 'Reversl_Ce' if side == 'CE' else 'Reversl_Pe'
            val = (
                OptionChain.objects
                .filter(Symbol=symbol_upper, Time__date=selected_date, Strike_Price=strike)
                .order_by('-Time')
                .values_list(col_name, flat=True)
                .first()
            )
            if val and float(val) > 0:
                result = round(float(val), 2)
                
    # ✅ Bug #2 Fix: Exception log करें
    except Exception as e:
        print(f"⚠️ get_rev_val_for_dashboard Error | {symbol_upper} {strike} {side} | {e}")
        
    if result is not None:
        # ✅ Bug #3 Fix: Dynamic timeout (Live = 5s, Historical = 5mins)
        timeout = 5 if selected_date == today else 300
        cache.set(cache_key, result, timeout)
        
    return result



@admin_only
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
@admin_only
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
                    # cursor.execute("VACUUM;")
                    cursor.execute("VACUUM FULL;")
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


@admin_only
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
@login_required
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
    # from django.db.models import Sum, Count, Q
    # stats = trades.aggregate(
    #     total_pnl  = Sum('pnl'),
    #     total      = Count('id'),
    #     # wins       = Count('id', filter=Q(result='TARGET')),
    #     # losses     = Count('id', filter=Q(result='SL')),
    #     wins       = Count('id', filter=Q(result='TARGET') | (Q(result='MANUAL_EXIT', pnl__gt=0))),
    #     losses     = Count('id', filter=Q(result='SL') | (Q(result='MANUAL_EXIT', pnl__lt=0))),
    # )

    # total_pnl    = round(stats['total_pnl'] or 0, 2)
    # total_trades = stats['total']
    # wins         = stats['wins']
    # losses       = stats['losses']
    # closed_trades = wins + losses
    # win_rate     = round((wins / closed_trades * 100), 1) if closed_trades > 0 else 0

    # ✅ Fix 1: एक ही aggregate call में सब कैलकुलेट करो
    from django.db.models import Sum, Count, Q
    stats = trades.aggregate(
        total_pnl  = Sum('pnl'),
        total_pnl_rupees = Sum('pnl_rupees'), # 👈 यह नई लाइन जोड़ें
        total      = Count('id'),
        wins       = Count('id', filter=Q(result='TARGET') | (Q(result='MANUAL_EXIT', pnl__gt=0))),
        losses     = Count('id', filter=Q(result='SL') | (Q(result='MANUAL_EXIT', pnl__lt=0))),
    )

    total_pnl        = round(stats['total_pnl'] or 0, 2)
    total_pnl_rupees = round(stats['total_pnl_rupees'] or 0, 2) # 👈 यह भी जोड़ें
    total_trades     = stats['total']
    wins             = stats['wins']
    losses           = stats['losses']
    closed_trades    = wins + losses
    win_rate         = round((wins / closed_trades * 100), 1) if closed_trades > 0 else 0

    # ✅ Fix 2: unique_symbols — SKIPPED-only symbols filter होंगे, order_by भी
    unique_symbols = (
        PaperTrade.objects
        .exclude(result="SKIPPED")
        .values_list('symbol', flat=True)
        .distinct()
        .order_by('symbol')
    )

    # return render(request, 'mystock/trade_dashboard.html', {
    #     'trades'         : trades.order_by('-trade_date', '-entry_time'),
    #     'start_date'     : start_date,
    #     'end_date'       : end_date,
    #     'selected_symbol': symbol_filter or 'ALL',
    #     'unique_symbols' : unique_symbols,
    #     'total_pnl'      : total_pnl,
    #     'total_trades'   : total_trades,   # ✅ Fix 1: template में trades.count नहीं चलेगा
    #     'wins'           : wins,            # ✅ Fix 2: नये stat cards के लिए
    #     'losses'         : losses,
    #     'win_rate'       : win_rate,
    # })
    return render(request, 'mystock/trade_dashboard.html', {
        'trades'         : trades.order_by('-trade_date', '-entry_time'),
        'start_date'     : start_date,
        'end_date'       : end_date,
        'selected_symbol': symbol_filter or 'ALL',
        'unique_symbols' : unique_symbols,
        'total_pnl'      : total_pnl,
        'total_pnl_rupees': total_pnl_rupees, # 👈 इसे HTML के लिए पास करें
        'total_trades'   : total_trades,
        'wins'           : wins,
        'losses'         : losses,
        'win_rate'       : win_rate,
    })



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


def _calc_eff_strikes_from_dict(sr_dict: dict, symbol: str) -> tuple:
    """
    ✅ get_master_levels का lightweight version — DB query नहीं।
    Replay के लिए: हर tick का sr_dict पहले से loaded है।
    trade_logic.py का exact same 25+ condition matrix use करता है।
    """
    step = STEP_MAP.get(symbol, DEFAULT_STEP)

    res_status = str(sr_dict.get('resistance_status', '')).upper()
    sup_status = str(sr_dict.get('supprt_status', '')).upper()
    res_base   = _safe_float(sr_dict.get('resistance_strike'))
    sup_base   = _safe_float(sr_dict.get('supprt_strike'))

    # Number निकालो
    m_res = re.search(r'(\d{4,6})', res_status)
    m_sup = re.search(r'(\d{4,6})', sup_status)
    res_target = float(m_res.group(1)) if m_res else res_base
    sup_target = float(m_sup.group(1)) if m_sup else sup_base

    # ✅ parse_status_type — SHIFTED WTB को WTB से अलग करता है
    def _type(s):
        if "SHIFTED" in s and "WTT" in s: return "SHIFTED WTT"
        if "SHIFTED" in s and "WTB" in s: return "SHIFTED WTB"
        if "WTT" in s:                    return "WTT"
        if "WTB" in s:                    return "WTB"
        if "STRONG" in s:                 return "STRONG"
        return ""

    res_type = _type(res_status)
    sup_type = _type(sup_status)

    # ── Resistance (trade_logic.py की exact copy) ──
    # --------res wtb-------------
    if res_type == "WTB"            and sup_type == "WTB"           : eff_res = res_target + step # test ok
    elif res_type == "WTB"          and sup_type == "WTT"           : eff_res = res_base 
    elif res_type == "WTB"          and sup_type == "STRONG"        : eff_res = res_base - step # test ok
    elif res_type == "WTB"          and sup_type == "SHIFTED WTB"   : eff_res = res_base + step # test ok
    elif res_type == "WTB"          and sup_type == "SHIFTED WTT"   : eff_res = res_base # test ok
    #--------res wtt-------------
    elif res_type == "WTT"          and sup_type == "WTB"           : eff_res = res_target - step
    elif res_type == "WTT"          and sup_type == "WTT"           : eff_res = res_target + step
    elif res_type == "WTT"          and sup_type == "STRONG"        : eff_res = res_target - step
    elif res_type == "WTT"          and sup_type == "SHIFTED WTB"   : eff_res = res_target + step
    elif res_type == "WTT"          and sup_type == "SHIFTED WTT"   : eff_res = res_base 
    #--------res strong-------------
    elif res_type == "STRONG"       and sup_type == "WTB"           : eff_res = res_base 
    elif res_type == "STRONG"       and sup_type == "WTT"           : eff_res = res_base + step
    elif res_type == "STRONG"       and sup_type == "STRONG"        : eff_res = res_base + step
    elif res_type == "STRONG"       and sup_type == "SHIFTED WTB"   : eff_res = res_base + step
    elif res_type == "STRONG"       and sup_type == "SHIFTED WTT"   : eff_res = res_base
    #--------res shifted wtb-------------
    elif res_type == "SHIFTED WTB" and sup_type == "WTB"            : eff_res = res_base 
    elif res_type == "SHIFTED WTB" and sup_type == "WTT"            : eff_res = res_base + step
    elif res_type == "SHIFTED WTB" and sup_type == "STRONG"         : eff_res = res_base + step
    elif res_type == "SHIFTED WTB" and sup_type == "SHIFTED WTB"    : eff_res = res_base + step
    elif res_type == "SHIFTED WTB" and sup_type == "SHIFTED WTT"    : eff_res = res_base
    #--------res shifted wtt-------------
    elif res_type == "SHIFTED WTT" and sup_type == "WTB"            : eff_res = res_target - step
    elif res_type == "SHIFTED WTT" and sup_type == "WTT"            : eff_res = res_target
    elif res_type == "SHIFTED WTT" and sup_type == "STRONG"         : eff_res = res_target 
    elif res_type == "SHIFTED WTT" and sup_type == "SHIFTED WTB"    : eff_res = res_target - step
    elif res_type == "SHIFTED WTT" and sup_type == "SHIFTED WTT"    : eff_res = res_base
    else: eff_res =                                 res_base + step


    # ── Support (trade_logic.py की exact copy) ──
     # --------sup wtb-------------
    if sup_type == "WTB"            and res_type == "WTB"           : eff_sup = sup_target - step
    elif sup_type == "WTB"          and res_type == "WTT"           : eff_sup = sup_target 
    elif sup_type == "WTB"          and res_type == "STRONG"        : eff_sup = sup_target + step # test ok
    elif sup_type == "WTB"          and res_type == "SHIFTED WTB"   : eff_sup = sup_base
    elif sup_type == "WTB"          and res_type == "SHIFTED WTT"   : eff_sup = sup_target 
    #--------sup wtt-------------
    elif sup_type == "WTT"          and res_type == "WTB"           : eff_sup = sup_base
    elif sup_type == "WTT"          and res_type == "WTT"           : eff_sup = sup_base 
    elif sup_type == "WTT"          and res_type == "STRONG"        : eff_sup = sup_base 
    elif sup_type == "WTT"          and res_type == "SHIFTED WTB"   : eff_sup = sup_base + step
    elif sup_type == "WTT"          and res_type == "SHIFTED WTT"   : eff_sup = sup_base - step
    #--------sup strong-------------
    elif sup_type == "STRONG"       and res_type == "WTB"           : eff_sup = sup_base - step
    elif sup_type == "STRONG"       and res_type == "WTT"           : eff_sup = sup_base 
    elif sup_type == "STRONG"       and res_type == "STRONG"        : eff_sup = sup_base - step
    elif sup_type == "STRONG"       and res_type == "SHIFTED WTB"   : eff_sup = sup_base 
    elif sup_type == "STRONG"       and res_type == "SHIFTED WTT"   : eff_sup = sup_base - step
    #--------sup shifted wtb-------------
    elif sup_type == "SHIFTED WTB"  and res_type == "WTB"           : eff_sup = sup_target 
    elif sup_type == "SHIFTED WTB"  and res_type == "WTT"           : eff_sup = sup_target + step
    elif sup_type == "SHIFTED WTB"  and res_type == "STRONG"        : eff_sup = sup_target + step # test ok
    elif sup_type == "SHIFTED WTB"  and res_type == "SHIFTED WTB"   : eff_sup = sup_base - step
    elif sup_type == "SHIFTED WTB"  and res_type == "SHIFTED WTT"   : eff_sup = sup_target - step
    #--------sup shifted wtt-------------
    elif sup_type == "SHIFTED WTT"  and res_type == "WTB"           : eff_sup = sup_base - step # test ok
    elif sup_type == "SHIFTED WTT"  and res_type == "WTT"           : eff_sup = sup_base 
    elif sup_type == "SHIFTED WTT"  and res_type == "STRONG"        : eff_sup = sup_base - step
    elif sup_type == "SHIFTED WTT"  and res_type == "SHIFTED WTB"   : eff_sup = sup_base 
    elif sup_type == "SHIFTED WTT"  and res_type == "SHIFTED WTT"   : eff_sup = sup_base - step
    else:                                          eff_sup = sup_base - step

    return eff_res, eff_sup


def get_reversal_lines_for_replay(symbol: str, date_str: str,
                                   sr_dict: dict, oc_list: list) -> list:
    """
    ✅ FIXED: get_master_levels का DB-free version use करता है।
    पुराने simplified (गलत) SHIFTED logic की जगह
    trade_logic.py का exact 25-condition matrix use होता है।
    """
    try:
        if not sr_dict or not oc_list:
            return []

        step = STEP_MAP.get(symbol, DEFAULT_STEP)
        spot = _safe_float(oc_list[0].get('Spot_Price', 0)) if oc_list else 0.0

        # ✅ FIX: सही strike calculation — trade_logic.py की exact copy
        eff_res, eff_sup = _calc_eff_strikes_from_dict(sr_dict, symbol)

        # Fallback
        if eff_res == 0: eff_res = spot + step
        if eff_sup == 0: eff_sup = spot - step

        global_low  = min(eff_res, eff_sup) - step
        global_high = max(eff_res, eff_sup) + step

        lines   = []
        seen_ce = set()
        seen_pe = set()

        # ── FIRST PASS: Master R / S lines ──
        for row in oc_list:
            strike = _safe_float(row.get('Strike_Price', 0))
            if not (global_low <= strike <= global_high):
                continue

            if strike == eff_res:
                val = _safe_float(row.get('Reversl_Ce'))
                if val > 0:
                    lines.append({"price": val, "color": "#ff8c00", "width": 3, "label": f"R {strike:.0f}"})
                    seen_ce.add(val)

            if strike == eff_sup:
                val = _safe_float(row.get('Reversl_Pe'))
                if val > 0:
                    lines.append({"price": val, "color": "#00bfff", "width": 3, "label": f"S {strike:.0f}"})
                    seen_pe.add(val)

        # ── SECOND PASS: बाकी lines ──
        for row in oc_list:
            strike = _safe_float(row.get('Strike_Price', 0))
            if not (global_low <= strike <= global_high):
                continue

            if strike != eff_res:
                val = _safe_float(row.get('Reversl_Ce'))
                if val > 0 and val >= spot and val not in seen_ce:
                    seen_ce.add(val)
                    lines.append({"price": val, "color": "#f85149", "width": 1, "label": f"CE {strike:.0f}"})

            if strike != eff_sup:
                val = _safe_float(row.get('Reversl_Pe'))
                if val > 0 and val < spot and val not in seen_pe:
                    seen_pe.add(val)
                    lines.append({"price": val, "color": "#3fb950", "width": 1, "label": f"P {strike:.0f}"})

        lines.sort(key=lambda x: x["price"], reverse=True)
        return lines

    except Exception:
        logger.exception("get_reversal_lines_for_replay failed | symbol=%s | date=%s", symbol, date_str)
        return []
    



# ─────────────────────────────────────────────────────────────────────
# फाइल: mystock/views.py में add करें
# URL:   path('backtest/', backtest_view, name='backtest'),
#        path('api/backtest/run/', backtest_run_api, name='backtest_run'),
# ─────────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────────
# View 1: HTML Page
# ─────────────────────────────────────────────────────────────────────
@login_required
def backtest_view(request):
    return render(request, 'mystock/backtesta.html')


# ─────────────────────────────────────────────────────────────────────
# View 2: AJAX API — Backtest Run
# ─────────────────────────────────────────────────────────────────────
@login_required
@require_GET
def backtest_run_api(request):
    symbol     = request.GET.get('symbol', 'NIFTY').strip().upper()
    date_str   = request.GET.get('date', '').strip()
    interval   = int(request.GET.get('interval', 1))
    buffer_pts = float(request.GET.get('buffer', 2.0))
    target_pts = float(request.GET.get('target_pts', 50))
    sl_pts     = float(request.GET.get('sl_pts', 50))

    if not date_str:
        return JsonResponse({'error': 'date parameter जरूरी है'}, status=400)

    try:
        selected_date = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return JsonResponse({'error': f'Date format गलत: {date_str}'}, status=400)

    step         = 100 if 'BANKNIFTY' in symbol or 'SENSEX' in symbol else 50
    tolerance    = 20.0
    MARKET_START = dt_time(9, 15)
    MARKET_END   = dt_time(15, 30)

    day_start = timezone.make_aware(datetime.combine(selected_date, dt_time.min))
    day_end   = timezone.make_aware(datetime.combine(selected_date, dt_time.max))

    # ── 1. SR Data ──
    sr_rows = list(
        LiveSRData.objects
        .filter(Symbol__iexact=symbol, Time__gte=day_start, Time__lte=day_end)
        .order_by('Time')
    )
    if not sr_rows:
        return JsonResponse({'error': f'{date_str} को {symbol} का SR Data नहीं मिला'}, status=404)

    # ── 2. OptionChain ticks ──
    all_oc = list(
        OptionChain.objects
        .filter(Symbol__iexact=symbol, Time__gte=day_start, Time__lte=day_end)
        .order_by('Time')
        .values('Time', 'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe')
    )
    if not all_oc:
        return JsonResponse({'error': f'{date_str} को {symbol} का OptionChain data नहीं मिला'}, status=404)

    spot_by_time   = {}
    skipped_outside = 0
    for row in all_oc:
        t     = row['Time']
        t_ist = timezone.localtime(t)
        if not (MARKET_START <= t_ist.time() <= MARKET_END):
            skipped_outside += 1
            continue
        if t_ist.minute % interval == 0:
            if t not in spot_by_time:
                spot_by_time[t] = {'spot': float(row['Spot_Price'] or 0), 'ist': t_ist, 'strikes': {}}
            s = float(row['Strike_Price'] or 0)
            if s > 0:
                spot_by_time[t]['strikes'][s] = {
                    'ce': float(row['Reversl_Ce'] or 0),
                    'pe': float(row['Reversl_Pe'] or 0),
                }

    sorted_times = sorted(spot_by_time.keys())

    # ── Helpers ──
    def get_sr_at_time(tick_time):
        active = None
        for sr in sr_rows:
            if sr.Time <= tick_time:
                active = sr
            else:
                break
        return active

    def calc_eff_strikes(sr):
        if not sr:
            return 0, 0
        res_status = str(sr.resistance_status or '').upper()
        sup_status = str(sr.supprt_status or '').upper()
        res_base   = float(sr.resistance_strike or 0)
        sup_base   = float(sr.supprt_strike or 0)
        m_res = re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
        m_sup = re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
        res_target = float(m_res.group(1)) if m_res else res_base
        sup_target = float(m_sup.group(1)) if m_sup else sup_base

        if   'WTB' in res_status and 'WTB' in sup_status: eff_res = res_target
        elif 'WTB' in res_status and 'WTT' in sup_status: eff_res = res_base
        elif 'WTB' in res_status and 'STRONG' in sup_status: eff_res = res_base
        elif 'WTB' in res_status and 'SHIFTED WTB' in sup_status: eff_res = res_base + step
        elif 'WTB' in res_status and 'SHIFTED WTT' in sup_status: eff_res = res_base - step
        elif 'WTT' in res_status and 'WTB' in sup_status: eff_res = res_target
        elif 'WTT' in res_status and 'WTT' in sup_status: eff_res = res_target + step
        elif 'WTT' in res_status and 'STRONG' in sup_status: eff_res = res_target
        elif 'WTT' in res_status and 'SHIFTED WTB' in sup_status: eff_res = res_target + step
        elif 'WTT' in res_status and 'SHIFTED WTT' in sup_status: eff_res = res_base
        elif 'STRONG' in res_status and 'WTB' in sup_status: eff_res = res_base
        elif 'STRONG' in res_status and 'WTT' in sup_status: eff_res = res_base + step
        elif 'STRONG' in res_status and 'STRONG' in sup_status: eff_res = res_base + step
        elif 'STRONG' in res_status and 'SHIFTED WTB' in sup_status: eff_res = res_base + step
        elif 'STRONG' in res_status and 'SHIFTED WTT' in sup_status: eff_res = res_base
        elif 'SHIFTED WTB' in res_status and 'WTB' in sup_status: eff_res = res_base
        elif 'SHIFTED WTB' in res_status and 'WTT' in sup_status: eff_res = res_base + step
        elif 'SHIFTED WTB' in res_status and 'STRONG' in sup_status: eff_res = res_base + step
        elif 'SHIFTED WTB' in res_status and 'SHIFTED WTB' in sup_status: eff_res = res_base + step
        elif 'SHIFTED WTB' in res_status and 'SHIFTED WTT' in sup_status: eff_res = res_base
        elif 'SHIFTED WTT' in res_status and 'WTB' in sup_status: eff_res = res_target - step
        elif 'SHIFTED WTT' in res_status and 'WTT' in sup_status: eff_res = res_target
        elif 'SHIFTED WTT' in res_status and 'STRONG' in sup_status: eff_res = res_target - step
        elif 'SHIFTED WTT' in res_status and 'SHIFTED WTB' in sup_status: eff_res = res_target - step
        elif 'SHIFTED WTT' in res_status and 'SHIFTED WTT' in sup_status: eff_res = res_base
        else: eff_res = res_base + step

        if   'WTB' in sup_status and 'WTB' in res_status: eff_sup = sup_target - step
        elif 'WTB' in sup_status and 'WTT' in res_status: eff_sup = sup_target
        elif 'WTB' in sup_status and 'STRONG' in res_status: eff_sup = sup_target
        elif 'WTB' in sup_status and 'SHIFTED WTB' in res_status: eff_sup = sup_target - step
        elif 'WTB' in sup_status and 'SHIFTED WTT' in res_status: eff_sup = sup_base
        elif 'WTT' in sup_status and 'WTB' in res_status: eff_sup = sup_base
        elif 'WTT' in sup_status and 'WTT' in res_status: eff_sup = sup_base + step
        elif 'WTT' in sup_status and 'STRONG' in res_status: eff_sup = sup_base
        elif 'WTT' in sup_status and 'SHIFTED WTB' in res_status: eff_sup = sup_base + step
        elif 'WTT' in sup_status and 'SHIFTED WTT' in res_status: eff_sup = sup_base - step
        elif 'STRONG' in sup_status and 'WTB' in res_status: eff_sup = sup_base - step
        elif 'STRONG' in sup_status and 'WTT' in res_status: eff_sup = sup_base
        elif 'STRONG' in sup_status and 'STRONG' in res_status: eff_sup = sup_base - step
        elif 'STRONG' in sup_status and 'SHIFTED WTB' in res_status: eff_sup = sup_base
        elif 'STRONG' in sup_status and 'SHIFTED WTT' in res_status: eff_sup = sup_base - step
        elif 'SHIFTED WTB' in sup_status and 'WTB' in res_status: eff_sup = sup_target
        elif 'SHIFTED WTB' in sup_status and 'WTT' in res_status: eff_sup = sup_target + step
        elif 'SHIFTED WTB' in sup_status and 'STRONG' in res_status: eff_sup = sup_target + step
        elif 'SHIFTED WTB' in sup_status and 'SHIFTED WTB' in res_status: eff_sup = sup_base
        elif 'SHIFTED WTB' in sup_status and 'SHIFTED WTT' in res_status: eff_sup = sup_target + step
        elif 'SHIFTED WTT' in sup_status and 'WTB' in res_status: eff_sup = sup_base - step
        elif 'SHIFTED WTT' in sup_status and 'WTT' in res_status: eff_sup = sup_base
        elif 'SHIFTED WTT' in sup_status and 'STRONG' in res_status: eff_sup = sup_base - step
        elif 'SHIFTED WTT' in sup_status and 'SHIFTED WTB' in res_status: eff_sup = sup_base
        elif 'SHIFTED WTT' in sup_status and 'SHIFTED WTT' in res_status: eff_sup = sup_base - step
        else: eff_sup = sup_base - step

        return eff_res, eff_sup

    def get_rev_val_at_time(tick_time, strike, side, period=10):
        col = 'Reversl_Ce' if side == 'CE' else 'Reversl_Pe'
        rows = (
            OptionChain.objects
            .filter(Symbol__iexact=symbol, Time__lte=tick_time, Time__gte=day_start, Strike_Price=strike)
            .order_by('-Time').values(col)[:period]
        )
        vals = [float(r[col]) for r in rows if r[col] and float(r[col]) > 0]
        return round(sum(vals) / len(vals), 2) if vals else None

    # ── 3. Simulation ──
    trades     = []
    ticks_log  = []   # show-all data
    open_trade = None
    warnings   = []

    for tick_time in sorted_times:
        tick_data = spot_by_time[tick_time]
        spot      = tick_data['spot']
        if spot <= 0:
            continue

        sr = get_sr_at_time(tick_time)
        if not sr:
            continue

        eff_res, eff_sup = calc_eff_strikes(sr)
        if not eff_res or not eff_sup:
            continue

        r_level = get_rev_val_at_time(tick_time, eff_res, 'CE')
        s_level = get_rev_val_at_time(tick_time, eff_sup, 'PE')
        t_ist   = timezone.localtime(tick_time).strftime('%H:%M')

        ticks_log.append({
            'time': t_ist,
            'spot': spot,
            'eff_res': eff_res, 'r_level': r_level,
            'eff_sup': eff_sup, 's_level': s_level,
            'res_status': str(sr.resistance_status or '')[:20],
            'sup_status': str(sr.supprt_status or '')[:20],
            'open_trade': open_trade['type'] if open_trade else None,
        })

        # EXIT
        if open_trade:
            entry = open_trade['entry_spot']
            ttype = open_trade['type']
            target = open_trade.get('target')
            sl     = open_trade.get('sl')

            if ttype == 'PUT':
                if not target: target = entry - target_pts
                if not sl:     sl     = entry + sl_pts
                if sl <= entry:
                    sl = entry + sl_pts
                    warnings.append(f"{t_ist} PUT SL override (entry={entry:.0f})")
                hit_target = spot <= (target + buffer_pts)
                hit_sl     = spot >= (sl - buffer_pts)
            else:
                if not target: target = entry + target_pts
                if not sl:     sl     = entry - sl_pts
                if sl >= entry:
                    sl = entry - sl_pts
                    warnings.append(f"{t_ist} CALL SL override (entry={entry:.0f})")
                hit_target = spot >= (target - buffer_pts)
                hit_sl     = spot <= (sl + buffer_pts)

            if hit_target or hit_sl:
                pnl = (spot - entry) if ttype == 'CALL' else (entry - spot)
                open_trade.update({
                    'exit_time': timezone.localtime(tick_time).strftime('%H:%M'),
                    'exit_spot': spot,
                    'result': 'TARGET' if hit_target else 'SL',
                    'pnl': round(pnl, 2),
                    'target_used': round(target, 2),
                    'sl_used': round(sl, 2),
                })
                trades.append(open_trade)
                open_trade = None
                continue

        if open_trade:
            continue

        # ENTRY
        last_put_sl  = next((t for t in reversed(trades) if t['type'] == 'PUT'  and t['result'] == 'SL'), None)
        last_call_sl = next((t for t in reversed(trades) if t['type'] == 'CALL' and t['result'] == 'SL'), None)
        r_paused = last_put_sl  and float(last_put_sl.get('entry_strike', 0)) == eff_res
        s_paused = last_call_sl and float(last_call_sl.get('entry_strike', 0)) == eff_sup

        if not r_paused and r_level:
            r_traded = any(abs(t['trigger'] - r_level) <= tolerance for t in trades if t['type'] == 'PUT')
            if r_traded:
                eff_res = eff_res + step
                r_level = get_rev_val_at_time(tick_time, eff_res, 'CE')

        if not s_paused and s_level:
            s_traded = any(abs(t['trigger'] - s_level) <= tolerance for t in trades if t['type'] == 'CALL')
            if s_traded:
                eff_sup = eff_sup - step
                s_level = get_rev_val_at_time(tick_time, eff_sup, 'PE')

        if not r_paused and r_level and spot >= r_level:
            open_trade = {
                'type': 'PUT',
                'entry_time': t_ist, 'entry_spot': spot, 'entry_strike': eff_res,
                'trigger': r_level,
                'target': get_rev_val_at_time(tick_time, eff_res - step, 'CE'),
                'sl':     get_rev_val_at_time(tick_time, eff_res + step, 'CE'),
                'exit_time': None, 'exit_spot': None, 'result': 'OPEN', 'pnl': 0,
            }
        elif not s_paused and s_level and spot <= s_level:
            open_trade = {
                'type': 'CALL',
                'entry_time': t_ist, 'entry_spot': spot, 'entry_strike': eff_sup,
                'trigger': s_level,
                'target': get_rev_val_at_time(tick_time, eff_sup + step, 'PE'),
                'sl':     get_rev_val_at_time(tick_time, eff_sup - step, 'PE'),
                'exit_time': None, 'exit_spot': None, 'result': 'OPEN', 'pnl': 0,
            }

    # EOD
    if open_trade and sorted_times:
        last_spot = spot_by_time[sorted_times[-1]]['spot']
        pnl = (last_spot - open_trade['entry_spot']) if open_trade['type'] == 'CALL' else (open_trade['entry_spot'] - last_spot)
        open_trade.update({
            'exit_time': timezone.localtime(sorted_times[-1]).strftime('%H:%M'),
            'exit_spot': last_spot, 'result': 'EOD', 'pnl': round(pnl, 2),
        })
        trades.append(open_trade)

    # ── 4. Summary ──
    wins     = [t for t in trades if t['result'] == 'TARGET']
    losses   = [t for t in trades if t['result'] == 'SL']
    eod_list = [t for t in trades if t['result'] == 'EOD']
    net_pnl  = round(sum(t['pnl'] for t in trades), 2)
    win_rate = round(len(wins) / len(trades) * 100, 1) if trades else 0

    # Spot range
    all_spots = [spot_by_time[t]['spot'] for t in sorted_times]

    return JsonResponse({
        'symbol': symbol, 'date': date_str, 'interval': interval,
        'meta': {
            'sr_rows': len(sr_rows),
            'total_ticks': len(sorted_times),
            'skipped_outside': skipped_outside,
            'spot_high': max(all_spots) if all_spots else 0,
            'spot_low':  min(all_spots) if all_spots else 0,
        },
        'summary': {
            'total': len(trades), 'wins': len(wins),
            'losses': len(losses), 'eod': len(eod_list),
            'win_rate': win_rate, 'net_pnl': net_pnl,
        },
        'trades': trades,
        'ticks': ticks_log,
        'warnings': warnings,
    })



# Trade journal views Start Here



def _form_context(instance=None):
    return {
        "status_choices": TradeStatus.choices,
        "type_choices":   TradeType.choices,
        "level_choices":  TradeLevel.choices,
        "entry":          instance,
    }


# ── LIST  /journal/ ───────────────────────────────────────────
def journal_list(request):
    entries = TradingJournal.objects.all()

    # ── Filters ──
    trade_type         = request.GET.get('trade_type', '')
    trade_level        = request.GET.get('trade_level', '')
    resistance_status  = request.GET.get('resistance_status', '')
    support_status     = request.GET.get('support_status', '')

    if trade_type:
        entries = entries.filter(trade_type=trade_type)
    if trade_level:
        entries = entries.filter(trade_level=trade_level)
    if resistance_status:
        entries = entries.filter(resistance_status=resistance_status)
    if support_status:
        entries = entries.filter(support_status=support_status)

    # ── Stats (always on full DB) ──
    all_entries = TradingJournal.objects.all()

    return render(request, 'journal/journal_list.html', {
        "entries":            entries,
        "status_choices":     TradeStatus.choices,
        "type_choices":       TradeType.choices,
        "level_choices":      TradeLevel.choices,

        # active filter values (for keeping dropdowns selected)
        "filter_type":              trade_type,
        "filter_level":             trade_level,
        "filter_resistance_status": resistance_status,
        "filter_support_status":    support_status,

        # stats
        "total_count": all_entries.count(),
        "call_count":  all_entries.filter(trade_type='call').count(),
        "put_count":   all_entries.filter(trade_type='put').count(),
    })


# ── CREATE  /journal/add/ ─────────────────────────────────────
def journal_create(request):
    if request.method == 'POST':
        try:
            TradingJournal.objects.create(
                resistance_status  = request.POST['resistance_status'],
                resistance_strike  = request.POST['resistance_strike'],
                resistance_strike2 = request.POST.get('resistance_strike2') or None,
                support_status     = request.POST['support_status'],
                support_strike     = request.POST['support_strike'],
                support_strike2    = request.POST.get('support_strike2') or None,
                trade_type         = request.POST['trade_type'],
                trade_level        = request.POST['trade_level'],
                notes              = request.POST.get('notes', ''),
            )
            messages.success(request, "✅ Trade entry save हो गई!")
            return redirect('journal_list')
        except Exception as e:
            messages.error(request, f"❌ Error: {e}")

    return render(request, 'journal/journal_form.html', _form_context())


# ── EDIT  /journal/edit/<pk>/ ─────────────────────────────────
def journal_edit(request, pk):
    entry = get_object_or_404(TradingJournal, pk=pk)

    if request.method == 'POST':
        try:
            entry.resistance_status  = request.POST['resistance_status']
            entry.resistance_strike  = request.POST['resistance_strike']
            entry.resistance_strike2 = request.POST.get('resistance_strike2') or None
            entry.support_status     = request.POST['support_status']
            entry.support_strike     = request.POST['support_strike']
            entry.support_strike2    = request.POST.get('support_strike2') or None
            entry.trade_type         = request.POST['trade_type']
            entry.trade_level        = request.POST['trade_level']
            entry.notes              = request.POST.get('notes', '')
            entry.save()
            messages.success(request, "✅ Trade entry update हो गई!")
            return redirect('journal_list')
        except Exception as e:
            messages.error(request, f"❌ Error: {e}")

    return render(request, 'journal/journal_form.html', _form_context(entry))


# ── DELETE  /journal/delete/<pk>/ ────────────────────────────
def journal_delete(request, pk):
    entry = get_object_or_404(TradingJournal, pk=pk)
    if request.method == 'POST':
        entry.delete()
        messages.success(request, "🗑️ Trade entry delete हो गई।")
        return redirect('journal_list')
    return render(request, 'journal/journal_confirm_delete.html', {"entry": entry})




# Trade journal views End Here