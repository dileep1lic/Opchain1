import math
import re as _re
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.utils import timezone
from django.db.models import Max
from .models import OptionChain, LiveSRData, PaperTrade
import json


# ─── Helper ────────────────────────────────────────────────────────────────────

def is_valid(val):
    return val is not None and not math.isnan(val) and not math.isinf(val)


# ─── Step size निकालना ─────────────────────────────────────────────────────────

def get_step(symbol: str) -> int:
    """Symbol के हिसाब से step size (जैसे NIFTY=50, BANKNIFTY=100)"""
    sym = symbol.upper()
    if "BANKNIFTY" in sym or "BANKEX" in sym:
        return 100
    elif "FINNIFTY" in sym or "MIDCAP" in sym:
        return 50
    else:
        return 50  # Default NIFTY


# ─── R और S के Reversal Values निकालना ────────────────────────────────────────

def get_rs_trigger_prices(symbol: str):
    """
    यह function R और S के exact reversal values निकालता है
    जिन पर trade trigger होगा।
    
    Returns:
        dict: {
            'r_trigger': float,   # इस price पर PUT buy होगा
            's_trigger': float,   # इस price पर CALL buy होगा
            'r_strike': float,    # R वाली strike
            's_strike': float,    # S वाली strike
            'spot': float,        # Current spot
            'r_status': str,
            's_status': str,
            'error': str or None
        }
    """
    # 1. Latest LiveSRData fetch करो
    sr = LiveSRData.objects.filter(
        Symbol__iexact=symbol
    ).order_by('-Time').first()

    if not sr:
        return {'error': f"LiveSRData नहीं मिला: {symbol}"}

    if not sr.resistance_strike or not sr.supprt_strike:
        return {'error': "resistance_strike या supprt_strike missing है"}

    step = get_step(symbol)

    # ─── CALL SIDE (RESISTANCE) ─────────────────────────────────────────────────
    res_status = str(sr.resistance_status).upper() if sr.resistance_status else ""
    res_base = sr.resistance_strike
    m_res = _re.search(r'(?:WTB|WTT)\s+(\d+(?:\.\d+)?)', res_status)
    res_target = float(m_res.group(1)) if m_res else res_base

    if "SHIFTED WTT" in res_status:
        effective_res_strike = res_base
    elif "SHIFTED WTB" in res_status:
        effective_res_strike = res_base + step
    elif "WTT" in res_status:
        effective_res_strike = res_target + step
    elif "WTB" in res_status:
        effective_res_strike = res_target + step
    elif "STRONG" in res_status:
        effective_res_strike = res_base + step
    else:
        effective_res_strike = res_base + step

    # ─── PUT SIDE (SUPPORT) ──────────────────────────────────────────────────────
    sup_status = str(sr.supprt_status).upper() if sr.supprt_status else ""
    sup_base = sr.supprt_strike
    m_sup = _re.search(r'(?:WTB|WTT)\s+(\d+(?:\.\d+)?)', sup_status)
    sup_target = float(m_sup.group(1)) if m_sup else sup_base

    if "SHIFTED WTT" in sup_status:
        effective_sup_strike = sup_base
    elif "SHIFTED WTB" in sup_status:
        effective_sup_strike = sup_base - step
    elif "WTT" in sup_status:
        effective_sup_strike = sup_target - step
    elif "WTB" in sup_status:
        effective_sup_strike = sup_target - step
    elif "STRONG" in sup_status:
        effective_sup_strike = sup_base - step
    else:
        effective_sup_strike = sup_base - step

    # ─── OptionChain से Reversal Values निकालो ──────────────────────────────────
    # FIX: exact Time+Strike match की जगह flexible query:
    # 1. Strike को +-1 range में ढूंढो (float precision issue avoid)
    # 2. हर strike का latest record लो (Time exact match नहीं)
    # 3. Reversl_Ce/Pe NULL न हो — यह condition जरूरी है

    # Current Spot + latest time
    spot_row = OptionChain.objects.filter(
        Symbol__iexact=symbol,
        Spot_Price__isnull=False,
    ).order_by('-Time').values('Spot_Price', 'Time').first()

    current_spot = None
    latest_time = None
    if spot_row and is_valid(spot_row.get('Spot_Price')):
        current_spot = spot_row['Spot_Price']
        latest_time = spot_row['Time']

    if not latest_time:
        return {'error': "OptionChain data नहीं मिला"}

    # R Strike का Reversl_Ce — +-1 range, latest record
    r_row = OptionChain.objects.filter(
        Symbol__iexact=symbol,
        Strike_Price__gte=effective_res_strike - 1,
        Strike_Price__lte=effective_res_strike + 1,
        Reversl_Ce__isnull=False,
    ).order_by('-Time').values('Reversl_Ce').first()

    # S Strike का Reversl_Pe — +-1 range, latest record
    s_row = OptionChain.objects.filter(
        Symbol__iexact=symbol,
        Strike_Price__gte=effective_sup_strike - 1,
        Strike_Price__lte=effective_sup_strike + 1,
        Reversl_Pe__isnull=False,
    ).order_by('-Time').values('Reversl_Pe').first()

    r_trigger = None
    if r_row and is_valid(r_row.get('Reversl_Ce')):
        r_trigger = float(r_row['Reversl_Ce'])

    s_trigger = None
    if s_row and is_valid(s_row.get('Reversl_Pe')):
        s_trigger = float(s_row['Reversl_Pe'])

    return {
        'error': None,
        'spot': current_spot,
        'r_trigger': r_trigger,       # PUT buy होगा जब spot यहाँ आएगा
        'r_strike': effective_res_strike,
        'r_status': sr.resistance_status or '',
        's_trigger': s_trigger,       # CALL buy होगा जब spot यहाँ आएगा
        's_strike': effective_sup_strike,
        's_status': sr.supprt_status or '',
        'data_time': latest_time.isoformat() if latest_time else None,
    }


# ─── Tolerance (कितने points के अंदर trigger माना जाए) ─────────────────────────

TRIGGER_TOLERANCE = 5   # 5 points के अंदर आने पर trigger


# ─── Trade Check और Entry ──────────────────────────────────────────────────────

@csrf_exempt
def check_and_enter_trade(request):
    """
    यह API हर 5 सेकंड में call होगी।
    Spot price check करेगी और trade enter करेगी।
    """
    symbol = request.GET.get('symbol', 'NIFTY').upper()
    today = timezone.now().date()

    data = get_rs_trigger_prices(symbol)
    if data.get('error'):
        return JsonResponse({'status': 'error', 'msg': data['error']})

    spot = data['spot']
    r_trigger = data['r_trigger']
    s_trigger = data['s_trigger']

    if not spot:
        return JsonResponse({'status': 'waiting', 'msg': 'Spot price नहीं मिला'})

    entered = []

    # ─── R Touch → PUT Buy ──────────────────────────────────────────────────────
    if r_trigger and abs(spot - r_trigger) <= TRIGGER_TOLERANCE:
        # क्या पहले से open PUT trade है आज?
        existing = PaperTrade.objects.filter(
            symbol=symbol,
            trade_date=today,
            trade_type='PUT',
            result='OPEN',
            trigger_level='R',
        ).exists()

        if not existing:
            trade = PaperTrade.objects.create(
                symbol=symbol,
                trade_date=today,
                trade_type='PUT',
                entry_time=timezone.now(),
                entry_spot=spot,
                trigger_level='R',
                trigger_price=r_trigger,
                result='OPEN',
            )
            entered.append({'type': 'PUT', 'entry_spot': spot, 'trigger': r_trigger, 'id': trade.id})

    # ─── S Touch → CALL Buy ─────────────────────────────────────────────────────
    if s_trigger and abs(spot - s_trigger) <= TRIGGER_TOLERANCE:
        existing = PaperTrade.objects.filter(
            symbol=symbol,
            trade_date=today,
            trade_type='CALL',
            result='OPEN',
            trigger_level='S',
        ).exists()

        if not existing:
            trade = PaperTrade.objects.create(
                symbol=symbol,
                trade_date=today,
                trade_type='CALL',
                entry_time=timezone.now(),
                entry_spot=spot,
                trigger_level='S',
                trigger_price=s_trigger,
                result='OPEN',
            )
            entered.append({'type': 'CALL', 'entry_spot': spot, 'trigger': s_trigger, 'id': trade.id})

    return JsonResponse({
        'status': 'ok',
        'spot': spot,
        'r_trigger': r_trigger,
        's_trigger': s_trigger,
        'r_strike': data['r_strike'],
        's_strike': data['s_strike'],
        'r_status': data['r_status'],
        's_status': data['s_status'],
        'data_time': data['data_time'],
        'entered': entered,
    })


# ─── Open Trades को Monitor करना (Target/SL Hit check) ────────────────────────

@csrf_exempt
def monitor_open_trades(request):
    """
    Open trades को monitor करो — Target (+50) या SL (-50) hit चेक करो।
    """
    symbol = request.GET.get('symbol', 'NIFTY').upper()
    today = timezone.now().date()

    open_trades = PaperTrade.objects.filter(
        symbol=symbol,
        trade_date=today,
        result='OPEN',
    )

    if not open_trades.exists():
        return JsonResponse({'status': 'ok', 'msg': 'कोई open trade नहीं', 'closed': []})

    # Current Spot
    latest_oc = OptionChain.objects.filter(
        Symbol__iexact=symbol
    ).aggregate(max_time=Max('Time'))

    if not latest_oc['max_time']:
        return JsonResponse({'status': 'error', 'msg': 'Spot नहीं मिला'})

    spot_row = OptionChain.objects.filter(
        Symbol__iexact=symbol,
        Time=latest_oc['max_time'],
    ).values('Spot_Price').first()

    if not spot_row:
        return JsonResponse({'status': 'error', 'msg': 'Spot row नहीं मिला'})

    current_spot = spot_row['Spot_Price']
    closed = []

    for trade in open_trades:
        entry = trade.entry_spot
        result = None
        pnl = 0.0

        if trade.trade_type == 'PUT':
            # PUT: market नीचे जाए तो profit
            # Target: entry_spot - 50
            # SL:     entry_spot + 50
            if current_spot <= entry - 50:
                result = 'TARGET'
                pnl = +50.0
            elif current_spot >= entry + 50:
                result = 'SL'
                pnl = -50.0

        elif trade.trade_type == 'CALL':
            # CALL: market ऊपर जाए तो profit
            # Target: entry_spot + 50
            # SL:     entry_spot - 50
            if current_spot >= entry + 50:
                result = 'TARGET'
                pnl = +50.0
            elif current_spot <= entry - 50:
                result = 'SL'
                pnl = -50.0

        if result:
            trade.result = result
            trade.pnl = pnl
            trade.exit_time = timezone.now()
            trade.exit_spot = current_spot
            trade.save()
            closed.append({
                'id': trade.id,
                'type': trade.trade_type,
                'result': result,
                'pnl': pnl,
                'entry': entry,
                'exit': current_spot,
            })

    return JsonResponse({
        'status': 'ok',
        'current_spot': current_spot,
        'closed': closed,
    })


# ─── Dashboard Data (सारी info एक साथ) ─────────────────────────────────────────

def paper_trade_dashboard(request):
    """HTML Dashboard page serve करो"""
    from django.shortcuts import render
    return render(request, 'paper_trade_dashboard.html')


def dashboard_data(request):
    """
    Frontend dashboard के लिए सारा data एक API में।
    """
    symbol = request.GET.get('symbol', 'NIFTY').upper()
    today = timezone.now().date()

    # R/S Trigger Prices
    triggers = get_rs_trigger_prices(symbol)

    # Today's Trades
    trades_qs = PaperTrade.objects.filter(
        symbol=symbol,
        trade_date=today,
    ).order_by('-entry_time').values(
        'id', 'trade_type', 'entry_time', 'entry_spot',
        'trigger_level', 'trigger_price', 'exit_time',
        'exit_spot', 'result', 'pnl'
    )

    trades = []
    for t in trades_qs:
        # Current PnL for open trades
        current_pnl = t['pnl']
        if t['result'] == 'OPEN' and triggers.get('spot'):
            spot = triggers['spot']
            if t['trade_type'] == 'PUT':
                current_pnl = t['entry_spot'] - spot  # +ve = profit
            else:
                current_pnl = spot - t['entry_spot']   # +ve = profit

        trades.append({
            'id': t['id'],
            'type': t['trade_type'],
            'entry_time': t['entry_time'].strftime('%H:%M:%S') if t['entry_time'] else '',
            'entry_spot': t['entry_spot'],
            'trigger_level': t['trigger_level'],
            'trigger_price': t['trigger_price'],
            'exit_time': t['exit_time'].strftime('%H:%M:%S') if t['exit_time'] else '',
            'exit_spot': t['exit_spot'],
            'result': t['result'],
            'pnl': round(current_pnl, 2),
            'target': round(t['entry_spot'] + (50 if t['trade_type'] == 'CALL' else -50), 2),
            'sl': round(t['entry_spot'] + (-50 if t['trade_type'] == 'CALL' else 50), 2),
        })

    # Total PnL
    total_pnl = sum(t['pnl'] for t in trades if t['result'] != 'OPEN')

    return JsonResponse({
        'symbol': symbol,
        'triggers': triggers,
        'trades': trades,
        'total_pnl': round(total_pnl, 2),
        'server_time': timezone.now().strftime('%H:%M:%S'),
    })
