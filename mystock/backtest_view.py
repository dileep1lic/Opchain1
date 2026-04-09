# =============================================================================
# FILE: yourapp/views.py
#
# URL: urls.py में add करो:
#   from yourapp.views import backtest_view
#   path('backtest/', backtest_view, name='backtest'),
# =============================================================================

import math, re, json
from django.shortcuts import render
from django.utils.timezone import localtime
from collections import defaultdict

# ── अपने app का नाम यहाँ डालें ──────────────────────────────────────────────
from .models import OptionChain, LiveSRData
# ─────────────────────────────────────────────────────────────────────────────


def _is_valid(val):
    try:
        f = float(val)
        return not math.isnan(f) and not math.isinf(f)
    except (TypeError, ValueError):
        return False


def _get_step(strikes):
    all_s = sorted(set(float(x) for x in strikes))
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


# =============================================================================
def _run_backtest(symbol, date_str, target, sl):

    result = {
        'trades': [], 'stats': {}, 'timeline': [],
        'level_history': [], 'error': None,
    }

    # ── सभी SR records (पूरा दिन, time order) ─────────────────────────────
    all_sr = list(
        LiveSRData.objects
        .filter(Symbol__iexact=symbol, Time__date=date_str)
        .order_by('Time')
    )
    if not all_sr:
        result['error'] = f"LiveSRData नहीं मिली: {symbol} / {date_str}"
        return result

    # ── Strike step ────────────────────────────────────────────────────────
    strikes_list = list(
        OptionChain.objects
        .filter(Symbol__iexact=symbol, Time__date=date_str)
        .values_list('Strike_Price', flat=True).distinct()
    )
    step = _get_step(strikes_list)

    # ── OptionChain पूरे दिन का एक बार load (performance) ─────────────────
    oc_all = list(
        OptionChain.objects
        .filter(Symbol__iexact=symbol, Time__date=date_str)
        .values('Time', 'Strike_Price', 'Spot_Price', 'Reversl_Ce', 'Reversl_Pe')
        .order_by('Time')
    )

    # Strike → sorted rows (for binary search style lookups)
    strike_rows = defaultdict(list)
    for row in oc_all:
        strike_rows[row['Strike_Price']].append(row)

    def get_reversal_at_time(strike, t_cutoff, side='CE'):
        key  = 'Reversl_Ce' if side == 'CE' else 'Reversl_Pe'
        best = None
        for row in strike_rows.get(float(strike), []):
            if row['Time'] <= t_cutoff and _is_valid(row.get(key)):
                best = float(row[key])
        return best

    # ── Level History: हर SR record → R & S level ──────────────────────────
    level_history = []

    for sr in all_sr:
        if not sr.resistance_strike or not sr.supprt_strike:
            continue

        eff_res, eff_sup = _compute_effective_strikes(sr, step)
        sr_time = sr.Time

        r_val = get_reversal_at_time(eff_res, sr_time, 'CE')
        s_val = get_reversal_at_time(eff_sup, sr_time, 'PE')

        if r_val is None or s_val is None:
            continue

        level_history.append({
            'from_time'  : sr_time,                                  # datetime (internal)
            'from_str'   : localtime(sr_time).strftime('%H:%M:%S'),  # display
            'r_strike'   : float(eff_res),
            'r_base'     : float(sr.resistance_strike),
            'r_level'    : round(r_val, 2),
            'res_status' : str(sr.resistance_status or ''),
            's_strike'   : float(eff_sup),
            's_base'     : float(sr.supprt_strike),
            's_level'    : round(s_val, 2),
            'sup_status' : str(sr.supprt_status or ''),
        })

    if not level_history:
        result['error'] = "LiveSRData में valid Reversl values नहीं मिलीं!"
        return result

    # Template के लिए (from_time हटाकर + changed flag add करके)
    lh_for_template = []
    for i, lh in enumerate(level_history):
        entry = {k: v for k, v in lh.items() if k != 'from_time'}
        # पिछली entry से R या S strike बदला? → highlight करो
        if i == 0:
            entry['changed'] = False
        else:
            prev = level_history[i - 1]
            entry['changed'] = (
                lh['r_strike'] != prev['r_strike'] or
                lh['s_strike'] != prev['s_strike']
            )
        lh_for_template.append(entry)
    result['level_history'] = lh_for_template

    def get_current_lh(t):
        """उस time तक का latest level_history entry"""
        cur = None
        for lh in level_history:
            if lh['from_time'] <= t:
                cur = lh
            else:
                break
        return cur

    # ── Spot Timeline ───────────────────────────────────────────────────────
    timeline_dict = {}
    for row in oc_all:
        t, sp = row['Time'], row['Spot_Price']
        if t not in timeline_dict and _is_valid(sp):
            timeline_dict[t] = float(sp)

    if not timeline_dict:
        result['error'] = "OptionChain में Spot_Price data नहीं मिला!"
        return result

    sorted_times = sorted(timeline_dict.keys())

    # Chart data (spot + current R/S at each tick)
    chart_data = []
    for t in sorted_times:
        lh = get_current_lh(t)
        chart_data.append({
            'time'   : localtime(t).strftime('%H:%M'),
            'spot'   : timeline_dict[t],
            'r_level': lh['r_level'] if lh else None,
            's_level': lh['s_level'] if lh else None,
        })
    result['timeline'] = chart_data

    # ── Backtest Loop ───────────────────────────────────────────────────────
    trades     = []
    open_trade = None
    trade_no   = 0

    for t in sorted_times:
        spot = timeline_dict[t]
        lh   = get_current_lh(t)
        if lh is None:
            continue

        R_LEVEL = lh['r_level']
        S_LEVEL = lh['s_level']
        t_str   = localtime(t).strftime('%H:%M:%S')

        # Exit
        if open_trade:
            entry = open_trade['entry_spot']
            ttype = open_trade['type']
            hit_target = (spot <= entry - target) if ttype == 'PUT' else (spot >= entry + target)
            hit_sl     = (spot >= entry + sl)     if ttype == 'PUT' else (spot <= entry - sl)

            if hit_target or hit_sl:
                open_trade.update({
                    'exit_spot': round(spot, 2),
                    'exit_time': t_str,
                    'result'   : 'TARGET' if hit_target else 'SL',
                    'pnl'      : +target if hit_target else -sl,
                })
                trades.append(open_trade)
                open_trade = None
                continue

        # Entry
        if not open_trade:
            if spot >= R_LEVEL:
                trade_no += 1
                open_trade = {
                    'no': trade_no, 'type': 'PUT', 'trigger': 'R',
                    'level': R_LEVEL, 'r_strike': lh['r_strike'],
                    's_strike': lh['s_strike'],
                    'entry_spot': round(spot, 2), 'entry_time': t_str,
                    'res_status': lh['res_status'],
                }
            elif spot <= S_LEVEL:
                trade_no += 1
                open_trade = {
                    'no': trade_no, 'type': 'CALL', 'trigger': 'S',
                    'level': S_LEVEL, 'r_strike': lh['r_strike'],
                    's_strike': lh['s_strike'],
                    'entry_spot': round(spot, 2), 'entry_time': t_str,
                    'sup_status': lh['sup_status'],
                }

    # Day-end
    if open_trade:
        last_spot = timeline_dict[sorted_times[-1]]
        entry     = open_trade['entry_spot']
        pnl = (entry - last_spot) if open_trade['type'] == 'PUT' else (last_spot - entry)
        open_trade.update({
            'exit_spot': round(last_spot, 2),
            'exit_time': localtime(sorted_times[-1]).strftime('%H:%M:%S'),
            'result': 'OPEN', 'pnl': round(pnl, 2),
        })
        trades.append(open_trade)

    result['trades'] = trades

    wins    = sum(1 for tr in trades if tr.get('pnl', 0) > 0)
    losses  = sum(1 for tr in trades if tr.get('pnl', 0) < 0)
    net_pnl = round(sum(tr.get('pnl', 0) for tr in trades), 2)

    result['stats'] = {
        'total': len(trades), 'wins': wins, 'losses': losses,
        'win_rate': round(wins / len(trades) * 100, 1) if trades else 0,
        'net_pnl': net_pnl,
        'open_time' : localtime(sorted_times[0]).strftime('%H:%M:%S'),
        'close_time': localtime(sorted_times[-1]).strftime('%H:%M:%S'),
        'ticks'     : len(sorted_times),
        'sr_records': len(level_history),
    }
    return result


def backtest_view(request):
    context = {'result': None, 'form': {}}

    if request.method == 'POST':
        symbol = request.POST.get('symbol', '').strip().upper()
        date   = request.POST.get('date', '').strip()
        target = float(request.POST.get('target', 50) or 50)
        sl     = float(request.POST.get('sl', 50) or 50)

        context['form'] = {'symbol': symbol, 'date': date, 'target': target, 'sl': sl}

        if symbol and date:
            res = _run_backtest(symbol, date, target, sl)
            res['timeline_json']      = json.dumps(res['timeline'])
            res['trades_json']        = json.dumps(res['trades'])
            res['level_history_json'] = json.dumps(res['level_history'])
            context['result'] = res

    return render(request, 'mystock/backtest.html', context)

