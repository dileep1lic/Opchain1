import datetime
from datetime import timedelta
import json
import logging
from django.core.cache import cache
from .models import market_view

_log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
_SECOND_HIGHEST_THRESHOLD = 0.75
_CACHE_TIMEOUT            = 8 * 60 * 60   # 8 hours
_HISTORY_LOOKBACK_HOURS   = 8

# ── Utility ───────────────────────────────────────────────────────────────────
def make_json_safe(data: dict) -> dict:
    """Convert datetime/date objects in a dict to ISO-format strings."""
    return {
        k: v.isoformat() if isinstance(v, (datetime.datetime, datetime.date)) else v
        for k, v in data.items()
    }

def _ltt_to_ist_date(ltt_ms):
    """Convert epoch ms to IST (UTC+5:30) calendar date."""
    utc_dt = datetime.datetime.utcfromtimestamp(ltt_ms / 1000)
    ist_dt = utc_dt + timedelta(hours=5, minutes=30)
    return ist_dt.date()

def get_strike_diff(strikes):
    if len(strikes) < 2: return 50
    diffs = [strikes[i+1] - strikes[i] for i in range(len(strikes)-1)]
    return max(set(diffs), key=diffs.count)

# ── OI / Volume peak finder ───────────────────────────────────────────────────
def _get_highest_and_status(data_list, factor_key):
    valid_data = [d for d in data_list if d.get(factor_key) is not None]
    if not valid_data:
        return None, None, "STRONG"

    highest = max(valid_data, key=lambda x: x[factor_key])
    
    threshold = _SECOND_HIGHEST_THRESHOLD * highest[factor_key]
    above_75 = sorted([d for d in valid_data if d[factor_key] >= threshold], 
                      key=lambda x: x[factor_key], reverse=True)
    
    second = above_75[1] if len(above_75) > 1 else None

    if second is None:                                          
        status = "STRONG"
    elif second['strikePrice'] < highest['strikePrice']:          
        status = "WTB"
    else:                                                         
        status = "WTT"
    return highest, second, status

# ── OI + Volume reconciler ────────────────────────────────────────────────────
def _determine_resistance_support(oi_strike, oi_strike_2, vol_strike, vol_strike_2, oi_status, vol_status, side="Resistance"):
    is_res   = (side == "Resistance")
    compare  = min if is_res else max
    default2 = float('inf') if is_res else 0

    if oi_strike is None or vol_strike is None:
        return 0, "STRONG", None

    oi2  = oi_strike_2['strikePrice']  if oi_strike_2  is not None else default2
    vol2 = vol_strike_2['strikePrice'] if vol_strike_2 is not None else default2

    final  = compare(oi_strike['strikePrice'], vol_strike['strikePrice'])
    second = compare(oi2, vol2)

    def ret(s, st): return s, st, None if st == "STRONG" else compare(oi2, vol2)

    # ── Case 1: OI and Volume agree on the same strike
    if oi_strike['strikePrice'] == vol_strike['strikePrice']:
        if oi_status == vol_status:
            return final, oi_status, None if oi_status == "STRONG" else second
        preferred = "WTB" if is_res else "WTT"
        status = preferred if preferred in (oi_status, vol_status) else "STRONG"
        return final, status, None if status == "STRONG" else second

    # ── Case 2: OI and Volume on different strikes
    other  = max(oi_strike['strikePrice'], vol_strike['strikePrice']) if is_res \
             else min(oi_strike['strikePrice'], vol_strike['strikePrice'])
    second = compare(oi2, vol2, other)

    if oi_status == vol_status:
        return final, oi_status, None if oi_status == "STRONG" else second

    if final != second:
        dom_oi = (oi_strike['strikePrice'] < vol_strike['strikePrice']) if is_res \
                 else (oi_strike['strikePrice'] > vol_strike['strikePrice'])
        dom_st = oi_status if dom_oi else vol_status
        cond   = (second > final) if is_res else (second < final)
        if dom_st == "STRONG" and cond:
            status = dom_st
        else:
            status = "WTT" if second > final else "WTB"
        return final, status, None if status == "STRONG" else second

    # final == second
    if is_res:
        if oi_strike['strikePrice'] < vol_strike['strikePrice']:
            s2 = None if oi_status == "STRONG" else compare(oi2, vol_strike['strikePrice'])
            return final, oi_status, s2
        s2 = None if vol_status == "STRONG" else compare(vol2, oi_strike['strikePrice'])
        return final, vol_status, s2
    else:
        if oi_strike['strikePrice'] > vol_strike['strikePrice']:
            s2 = None if oi_status == "STRONG" else compare(oi2, vol_strike['strikePrice'])
            return final, oi_status, s2
        s2 = None if vol_status == "STRONG" else compare(vol2, oi_strike['strikePrice'])
        return final, vol_status, s2

# ── Shifting state-machine handlers ──────────────────────────────────────────
def handle_strong(prev, strike, status, status_strike, side):
    ps  = prev.get(f'{side}_strike')
    err = ("Error", prev.get(f'{side}_view'))

    if status == "STRONG":
        if strike > ps: return "Shifted to Top & Became STRONG", "BULLISH"
        if strike < ps: return "Shifted to Bottom & Became STRONG", "BEARISH"

    elif status == "WTT":
        if strike == ps and status_strike > strike:
            return "Became WTT at New Place", "BULLISH"
        if strike > ps and status_strike > strike:
            return "Shifted to Top & Became WTT at New Place", "BULLISH"
        if strike < ps:
            if status_strike == ps:
                return f'Shifted to Bottom & Become WTT at Same Strike of Previous {side}', "BEARISH"
            return "Shifted to Bottom & Become WTT at New Place", "BULLISH"

    elif status == "WTB":
        if strike == ps and status_strike < strike:
            return "Became WTB at New Place", "BEARISH"
        if strike > ps:
            if status_strike == ps:
                return f'Shifted to Top & Become WTB at Same Strike of Previous {side}', "BULLISH"
            return "Shifted to Top & Become WTB at New Place", "BEARISH"
        if strike < ps and status_strike < strike:
            return "Shifted to Bottom & Become WTB at New Place", "BEARISH"

    return err

def handle_wtt(prev, strike, status, status_strike, side):
    ps   = prev.get(f'{side}_strike')
    pss  = prev.get(f'{side}_status_strike')
    err  = ("Error", prev.get(f'{side}_view'))

    if status == "STRONG":
        if strike == ps: return "Became STRONG at Same Strike", "BEARISH"
        if strike > ps:
            return ("Shifted to Top at WTT Strike & Became STRONG" if strike == pss
                    else "Shifted to Top at New Strike & Became STRONG"), "BULLISH"
        if strike < ps: return "Shifted to Bottom at New Strike & Became STRONG", "BEARISH"

    elif status == "WTT":
        if strike == ps and status_strike != pss:
            return "Became WTT at New Place", "BULLISH"
        if strike > ps:
            if strike == pss:          return "Shifted to Top at WTT & Became WTT at New Place", "BULLISH"
            if status_strike == pss:   return "Shifted to Top at New Place & Became WTT", "BULLISH"
            return "Shifted to Top at New Place & Became WTT at New Place", "BULLISH"
        if strike < ps:
            if status_strike == ps:
                return f'Shifted to Bottom at New Place & Became WTT at Same Strike of Previous {side}', "BEARISH"
            if status_strike == pss:   return "Shifted to Bottom at New Place & Became WTT", "BULLISH"
            return "Shifted to Bottom at New Place & Became WTT at New Place", "BULLISH"

    elif status == "WTB":
        if strike == ps: return "Became WTB at New Place", "BEARISH"
        if strike > ps:
            if strike == pss:
                if status_strike == ps:
                    return f'Shifted to Top at WTT & Became WTB at Same Strike of Previous {side}', "BULLISH"
                return "Shifted to Top at WTT & Became WTB at New Place", "BEARISH"
            if status_strike == ps:
                return f'Shifted to Top at New Place & Became WTB at Same Strike of Previous {side}', "BULLISH"
            return "Shifted to Top at New Place & Became WTB at New Place", "BEARISH"
        if strike < ps:
            return "Shifted to Bottom at New Strike & Became WTB at New Place", "BEARISH"

    return err

def handle_wtb(prev, strike, status, status_strike, side):
    ps   = prev.get(f'{side}_strike')
    pss  = prev.get(f'{side}_status_strike')
    err  = ("Error", prev.get(f'{side}_view'))

    if status == "STRONG":
        if strike == ps: return "Became STRONG at Same Strike", "BULLISH"
        if strike > ps:  return "Shifted to Top & Became STRONG", "BULLISH"
        if strike < ps:
            if strike == pss: return "Shifted to Bottom at WTB & Became STRONG", "BEARISH"
            return "Shifted to Bottom at New Strike & Became STRONG", "BEARISH"

    elif status == "WTB":
        if strike == ps and status_strike != pss:
            return "Became WTB at New Place", "BEARISH"
        if strike > ps:
            if status_strike == ps:
                return f'Shifted to Top at New Place & Became WTB at Same Strike of Previous {side}', "BULLISH"
            return "Shifted to Top at New Place & Became WTB at New Place", "BEARISH"
        if strike < ps:
            if strike == pss: return "Shifted to Bottom at WTB & Became WTB at New Place", "BEARISH"
            return "Shifted to Bottom at New Place & Became WTB at New Place", "BEARISH"

    elif status == "WTT":
        if strike == ps: return "Became WTT at New Place", "BULLISH"
        if strike > ps:  return "Shifted to Top at New Place & Became WTT at New Place", "BULLISH"
        if strike < ps:
            if strike == pss:
                if status_strike == ps:
                    return f'Shifted to Bottom at WTB & Became WTT at Same Strike of Previous {side}', "BEARISH"
                return "Shifted to Bottom at WTB & Became WTT at New Place", "BULLISH"
            if status_strike == ps:
                return f'Shifted to Bottom at New Place & Became WTT at Same Strike of Previous {side}', "BEARISH"
            return "Shifted to Bottom at New Place & Became WTT at New Place", "BULLISH"

    return err

def determine_shifting(prev, strike, status, status_strike, side):
    if (strike == prev.get(f'{side}_strike')
            and status == prev.get(f'{side}_status')
            and status_strike == prev.get(f'{side}_status_strike')):
        return "Continue", prev.get(f'{side}_view')

    handler = {"STRONG": handle_strong, "WTT": handle_wtt, "WTB": handle_wtb}.get(prev.get(f'{side}_status'))
    return handler(prev, strike, status, status_strike, side) if handler \
           else ("Error", prev.get(f'{side}_view'))

# ── Market view helpers ───────────────────────────────────────────────────────
def _resolve_final_view(r_view, s_view):
    view_matrix = {
        ("BULLISH", "BULLISH"): "BULLISH",
        ("BEARISH", "BEARISH"): "BEARISH",
        ("STRONG",  "STRONG" ): "STRONG",
        
        ("BULLISH", "STRONG" ): "BULLISH",
        ("STRONG",  "BULLISH"): "BULLISH",
        
        ("BEARISH", "STRONG" ): "BEARISH",
        ("STRONG",  "BEARISH"): "BEARISH",
        
        ("BULLISH", "BEARISH"): "AVOID",
        ("BEARISH", "BULLISH"): "AVOID",
    }
    return view_matrix.get((r_view, s_view), "STRONG")

def relative_market_view(symbol, expiry_date, current_timestamp,
                         r_strike, r_status, r_status_strike,
                         s_strike, s_status, s_status_strike):
    cache_key = f'prev_market_view_entry_{symbol}_{expiry_date}'

    eight_hours_ms = _HISTORY_LOOKBACK_HOURS * 3600 * 1000
    from_ltt       = current_timestamp - eight_hours_ms
    current_date   = _ltt_to_ist_date(current_timestamp)

    # ── Step 1: try cache ────────────────────────────────────────────────────
    prev = cache.get(cache_key)
    if prev:
        prev_ltt = prev.get('ltt_index')
        # Discard if from a different IST date OR older than 8 hours
        if (prev_ltt is None
                or prev_ltt < from_ltt
                or _ltt_to_ist_date(prev_ltt) != current_date):
            prev = None

    # ── Step 2: DB fallback if cache miss / invalid ──────────────────────────
    if not prev:
        prev_obj = market_view.objects.filter(
            underlyingAsset=symbol,
            expiryDate=expiry_date,
            ltt_index__range=(from_ltt, current_timestamp),
        ).order_by('-ltt_index').values(
            'ltt_index',
            'resistance_strike', 'resistance_status', 'resistance_status_strike',
            'support_strike', 'support_status', 'support_status_strike',
            'resistance_view', 'support_view', 'final_view'
        ).first()
        # Validate: must be same IST date (8-hour range already enforced above)
        if prev_obj and _ltt_to_ist_date(prev_obj['ltt_index']) != current_date:
            prev_obj = None
        prev = prev_obj

    view_map = {"WTT": "BULLISH", "WTB": "BEARISH"}

    if prev:
        res_shift, res_view = determine_shifting(prev, r_strike, r_status, r_status_strike, "resistance")
        sup_shift, sup_view = determine_shifting(prev, s_strike, s_status, s_status_strike, "support")
        res_view = {"BULLISH": "BULLISH", "BEARISH": "BEARISH"}.get(res_view, "STRONG")
        sup_view = {"BULLISH": "BULLISH", "BEARISH": "BEARISH"}.get(sup_view, "STRONG")
    else:
        res_shift = f'Initially Resistance is {r_status}'
        sup_shift = f'Initially Support is {s_status}'
        res_view  = view_map.get(r_status, "STRONG")
        sup_view  = view_map.get(s_status, "STRONG")

    return res_shift, res_view, sup_shift, sup_view, _resolve_final_view(res_view, sup_view)

# ── Main entry point ──────────────────────────────────────────────────────────
def process_market_view(symbol, underlying_key, expiry_date, oc_data, spot):
    """
    Main entry point for market view calculations.
    Replaces the old dataframe approach with direct dictionary processing.
    """
    if spot is None:
        return None
        
    # Extract ltt from INDEX if available
    ltt_ms = oc_data.get("INDEX", {}).get("ltt")
    if ltt_ms:
        try:
            current_timestamp = int(ltt_ms)
        except (TypeError, ValueError):
            import time
            current_timestamp = int(time.time() * 1000)
    else:
        import time
        current_timestamp = int(time.time() * 1000)
        
    strikes = sorted(k for k in oc_data if k != "INDEX" and isinstance(k, (int, float)))
    if not strikes:
        return None

    spd = get_strike_diff(strikes)
    
    # Helper for casting
    def _num(v):
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    # Build list of dicts for peak finding
    ed_list = []
    for strike in strikes:
        data = oc_data[strike]
        ce = data.get("CE", {})
        pe = data.get("PE", {})
        
        ed_list.append({
            'strikePrice': strike,
            'call_oi': _num(ce.get("oi")),
            'call_cvol': _num(ce.get("vtt")), # Using vtt
            'put_oi': _num(pe.get("oi")),
            'put_cvol': _num(pe.get("vtt")),
            'call_reversal': _num(ce.get("call_reversal")),
            'put_reversal': _num(pe.get("put_reversal"))
        })

    # OI & Volume peaks
    h_ce_oi,  s_ce_oi,  ce_oi_st  = _get_highest_and_status(ed_list, 'call_oi')
    h_ce_vol, s_ce_vol, ce_vol_st = _get_highest_and_status(ed_list, 'call_cvol')
    h_pe_oi,  s_pe_oi,  pe_oi_st  = _get_highest_and_status(ed_list, 'put_oi')
    h_pe_vol, s_pe_vol, pe_vol_st = _get_highest_and_status(ed_list, 'put_cvol')

    # Combine into single level
    r_strike, r_status, r_status_strike = _determine_resistance_support(
        h_ce_oi, s_ce_oi, h_ce_vol, s_ce_vol, ce_oi_st, ce_vol_st, side="Resistance")
    s_strike, s_status, s_status_strike = _determine_resistance_support(
        h_pe_oi, s_pe_oi, h_pe_vol, s_pe_vol, pe_oi_st, pe_vol_st, side="Support")

    ce_oi_at  = s_ce_oi['strikePrice']  if s_ce_oi  is not None else None
    ce_vol_at = s_ce_vol['strikePrice'] if s_ce_vol is not None else None
    pe_oi_at  = s_pe_oi['strikePrice']  if s_pe_oi  is not None else None
    pe_vol_at = s_pe_vol['strikePrice'] if s_pe_vol is not None else None

    # Relative market view
    res_shift, res_view, sup_shift, sup_view, final_view = relative_market_view(
        symbol, expiry_date, current_timestamp,
        r_strike, r_status, r_status_strike,
        s_strike, s_status, s_status_strike)

    def get_range_boundary(r_status, res_view, r_strike, r_status_strike, s_status, sup_view, s_strike, s_status_strike, step=50):
        # Prevent TypeError during dictionary eager evaluation if status_strike is None
        r_target = r_status_strike if r_status_strike is not None else 0
        s_target = s_status_strike if s_status_strike is not None else 0
        r_base = r_strike
        s_base = s_strike

        # Single merged lookup: (r_status, res_view, s_status, sup_view) → (safe_top, safe_bot)
        lookup = {
            # ── r=WTT/BULLISH ────────────────────────────────────────
            ("WTT", "BULLISH", "WTT",    "BULLISH"): (r_target + step,      s_base,            1),   # OK 7 S_OK 7
            ("WTT", "BULLISH", "WTT",    "BEARISH"): (r_base,               s_base,            2),   # OK 10 S_OK 22
            ("WTT", "BULLISH", "WTB",    "BULLISH"): (r_target + step,      s_target + step,   3),   # OK 9 S_OK 17
            ("WTT", "BULLISH", "WTB",    "BEARISH"): (r_target - step,      s_target,          4),   # R_OK 6 S_OK_2
            ("WTT", "BULLISH", "STRONG", "BULLISH"): (r_target + step,      s_base - step,     5),   # OK SR 3
            ("WTT", "BULLISH", "STRONG", "BEARISH"): (r_base,               s_base - step,     6),   # OK SR 4
            ("WTT", "BULLISH", "STRONG", "STRONG"):  (r_target - step,      s_base,            7),   # OK 8 S_OK 12

            # ── r=WTT/BEARISH ────────────────────────────────────────
            ("WTT", "BEARISH", "WTT",    "BULLISH"): (r_target,             s_base - step,     8),   # OK 22 S_OK 10
            ("WTT", "BEARISH", "WTT",    "BEARISH"): (r_base,               s_base - step,     9),   # OK 25 S_OK 25
            ("WTT", "BEARISH", "WTB",    "BULLISH"): (r_target - step,      s_target - step,   10),  # OK 24 S_OK 20
            ("WTT", "BEARISH", "WTB",    "BEARISH"): (r_target - step,      s_target - step,   11),  # OK 21 S_OK 5
            ("WTT", "BEARISH", "STRONG", "BULLISH"): (r_target,             s_base - step,     12),  # SR 7
            ("WTT", "BEARISH", "STRONG", "BEARISH"): (r_base + step,        s_base - step,     13),  # SR 8
            ("WTT", "BEARISH", "STRONG", "STRONG"):  (r_target,             s_base - step,     14),  # OK 23 S_OK 15

            # ── r=WTB/BULLISH ────────────────────────────────────────
            ("WTB", "BULLISH", "WTT",    "BULLISH"): (r_base + step,        s_base + step,     15),  # OK 17    S_OK 9
            ("WTB", "BULLISH", "WTT",    "BEARISH"): (r_base,               s_base,            16),  # OK 20    S_OK 24
            ("WTB", "BULLISH", "WTB",    "BULLISH"): (r_base + step,        s_base - step,     17),  # OK 19 S_OK 19
            ("WTB", "BULLISH", "WTB",    "BEARISH"): (r_base,               s_base,            18),  # OK 16 S_OK 4
            ("WTB", "BULLISH", "STRONG", "BULLISH"): (r_base + step,        s_base,            19),  # OK SR 9
            ("WTB", "BULLISH", "STRONG", "BEARISH"): (r_base + step,        s_base - step,     20),  # OK SR 10
            ("WTB", "BULLISH", "STRONG", "STRONG"):  (r_base + step,        s_base,            21),  # OK 18 S_OK 14

            # ── r=WTB/BEARISH ────────────────────────────────────────
            ("WTB", "BEARISH", "WTT",    "BULLISH"): (r_base,               s_base,            22),  # R_OK 2 S_OK 6
            ("WTB", "BEARISH", "WTT",    "BEARISH"): (r_base,               s_base - step,     23),  # R_OK 5 S_OK 21
            ("WTB", "BEARISH", "WTB",    "BULLISH"): (r_base + step,        s_target,          24),  # R_OK 4  S_OK 16
            ("WTB", "BEARISH", "WTB",    "BEARISH"): (r_target + step,      s_target - step,   25),  # R_OK 1 S_OK 1
            ("WTB", "BEARISH", "STRONG", "BULLISH"): (r_base + step,        s_base - step,     26),  # R_S_OK 1
            ("WTB", "BEARISH", "STRONG", "BEARISH"): (r_base - step,        s_base - step,     27),  # R_S_OK 2
            ("WTB", "BEARISH", "STRONG", "STRONG"):  (r_base - step,        s_base - step,     28),  # R_OK 3 S_OK 11

            # ── r=STRONG/BULLISH ─────────────────────────────────────
            ("STRONG", "BULLISH", "WTT",    "BULLISH"): (r_base + step,     s_target - step,   29),  # SR 17
            ("STRONG", "BULLISH", "WTT",    "BEARISH"): (r_base + step,     s_base - step,     30),  # SR 23
            ("STRONG", "BULLISH", "WTB",    "BULLISH"): (r_base + step,     s_base - step,     31),  # SR 21
            ("STRONG", "BULLISH", "WTB",    "BEARISH"): (r_base,            s_target + step,   32),  # SR 15
            ("STRONG", "BULLISH", "STRONG", "BULLISH"): (r_base + step,     s_base,            33),  # SR 11
            ("STRONG", "BULLISH", "STRONG", "BEARISH"): (r_base + step,     s_base - step,     34),  # SR 13
            ("STRONG", "BULLISH", "STRONG", "STRONG"):  (r_base + step,     s_base + step,     35),  # SR 19

            # ── r=STRONG/BEARISH ─────────────────────────────────────
            ("STRONG", "BEARISH", "WTT",    "BULLISH"): (r_base,            s_base,            36),  # SR 18
            ("STRONG", "BEARISH", "WTT",    "BEARISH"): (r_base - step,     s_base - step,     37),  # SR 24
            ("STRONG", "BEARISH", "WTB",    "BULLISH"): (r_base,            s_base - step,     38),  # SR 22
            ("STRONG", "BEARISH", "WTB",    "BEARISH"): (r_base,            s_target - step,   39),  # SR 16
            ("STRONG", "BEARISH", "STRONG", "BULLISH"): (r_base + step,     s_base - step,     40),  # SR 12
            ("STRONG", "BEARISH", "STRONG", "BEARISH"): (r_base,            s_base - step,     41),  # SR 14
            ("STRONG", "BEARISH", "STRONG", "STRONG"):  (r_base - step,     s_base - step,     42),  # SR 20

            # ── r=STRONG/STRONG ──────────────────────────────────────
            ("STRONG", "STRONG", "WTT",    "BULLISH"): (r_base + step,      s_base,            43),  # R_OK 12 S_OK 9
            ("STRONG", "STRONG", "WTT",    "BEARISH"): (r_base,             s_base - step,     44),  # R_OK 15 S_OK 23
            ("STRONG", "STRONG", "WTB",    "BULLISH"): (r_base + step,      s_target + step,   45),  # R_OK 14   S_OK 18
            ("STRONG", "STRONG", "WTB",    "BEARISH"): (r_base,             s_target + step,   46),  # R_OK 11 S_OK 3
            ("STRONG", "STRONG", "STRONG", "BULLISH"): (r_base + step,      s_base,            47),  # RS 5
            ("STRONG", "STRONG", "STRONG", "BEARISH"): (r_base,             s_base - step,     48),  # RS 6
            ("STRONG", "STRONG", "STRONG", "STRONG"):  (r_base + step,      s_base - step,     49),  # R_OK 13 S_OK 13
        }

        # lookup = {
        #     # ── r=WTT/BULLISH ────────────────────────────────────────
        #     ("WTT", "BULLISH", "WTT",    "BULLISH"): (r_target + step,      s_base,            1),   # OK 7 S_OK 7
        #     ("WTT", "BULLISH", "WTT",    "BEARISH"): (r_base,               s_base,            2),   # OK 10 S_OK 22
        #     ("WTT", "BULLISH", "WTB",    "BULLISH"): (r_target + step,      s_target + step,   3),   # OK 9 S_OK 17
        #     ("WTT", "BULLISH", "WTB",    "BEARISH"): (r_target - step,      s_target,          4),   # R_OK 6 S_OK_2
        #     ("WTT", "BULLISH", "STRONG", "BULLISH"): (r_target + step,      s_base - step,     5),   # OK SR 3
        #     ("WTT", "BULLISH", "STRONG", "BEARISH"): (r_base,               s_base - step,     6),   # OK SR 4
        #     ("WTT", "BULLISH", "STRONG", "STRONG"):  (r_target - step,      s_base,            7),   # OK 8 S_OK 12

        #     # ── r=WTT/BEARISH ────────────────────────────────────────
        #     ("WTT", "BEARISH", "WTT",    "BULLISH"): (r_target,             s_base - step,     8),   # OK 22 S_OK 10
        #     ("WTT", "BEARISH", "WTT",    "BEARISH"): (r_base,               s_base - step,     9),   # OK 25 S_OK 25
        #     ("WTT", "BEARISH", "WTB",    "BULLISH"): (r_target - step,      s_target - step,   10),  # OK 24 S_OK 20
        #     ("WTT", "BEARISH", "WTB",    "BEARISH"): (r_target - step,      s_target - step,   11),  # OK 21 S_OK 5
        #     ("WTT", "BEARISH", "STRONG", "BULLISH"): (r_target,             s_base - step,     12),  # SR 7
        #     ("WTT", "BEARISH", "STRONG", "BEARISH"): (r_base + step,        s_base - step,     13),  # SR 8
        #     ("WTT", "BEARISH", "STRONG", "STRONG"):  (r_target,             s_base - step,     14),  # OK 23 S_OK 15

        #     # ── r=WTB/BULLISH ────────────────────────────────────────
        #     ("WTB", "BULLISH", "WTT",    "BULLISH"): (r_base + step,        s_base + step,     15),  # OK 17    S_OK 9
        #     ("WTB", "BULLISH", "WTT",    "BEARISH"): (r_base,               s_base,            16),  # OK 20    S_OK 24
        #     ("WTB", "BULLISH", "WTB",    "BULLISH"): (r_base + step,        s_base - step,     17),  # OK 19 S_OK 19
        #     ("WTB", "BULLISH", "WTB",    "BEARISH"): (r_base,               s_base,            18),  # OK 16 S_OK 4
        #     ("WTB", "BULLISH", "STRONG", "BULLISH"): (r_base + step,        s_base,            19),  # OK SR 9
        #     ("WTB", "BULLISH", "STRONG", "BEARISH"): (r_base + step,        s_base - step,     20),  # OK SR 10
        #     ("WTB", "BULLISH", "STRONG", "STRONG"):  (r_base + step,        s_base,            21),  # OK 18 S_OK 14

        #     # ── r=WTB/BEARISH ────────────────────────────────────────
        #     ("WTB", "BEARISH", "WTT",    "BULLISH"): (r_base,               s_base,            22),  # R_OK 2 S_OK 6
        #     ("WTB", "BEARISH", "WTT",    "BEARISH"): (r_base,               s_base - step,     23),  # R_OK 5 S_OK 21
        #     ("WTB", "BEARISH", "WTB",    "BULLISH"): (r_base + step,        s_target,          24),  # R_OK 4  S_OK 16
        #     ("WTB", "BEARISH", "WTB",    "BEARISH"): (r_target + step,      s_target - step,   25),  # R_OK 1 S_OK 1
        #     ("WTB", "BEARISH", "STRONG", "BULLISH"): (r_base + step,        s_base - step,     26),  # R_S_OK 1
        #     ("WTB", "BEARISH", "STRONG", "BEARISH"): (r_base - step,        s_base - step,     27),  # R_S_OK 2
        #     ("WTB", "BEARISH", "STRONG", "STRONG"):  (r_base - step,        s_base - step,     28),  # R_OK 3 S_OK 11

        #     # ── r=STRONG/BULLISH ─────────────────────────────────────
        #     ("STRONG", "BULLISH", "WTT",    "BULLISH"): (r_base + step,     s_target - step,   29),  # SR 17
        #     ("STRONG", "BULLISH", "WTT",    "BEARISH"): (r_base + step,     s_base - step,     30),  # SR 23
        #     ("STRONG", "BULLISH", "WTB",    "BULLISH"): (r_base + step,     s_base - step,     31),  # SR 21
        #     ("STRONG", "BULLISH", "WTB",    "BEARISH"): (r_base,            s_target + step,   32),  # SR 15
        #     ("STRONG", "BULLISH", "STRONG", "BULLISH"): (r_base + step,     s_base,            33),  # SR 11
        #     ("STRONG", "BULLISH", "STRONG", "BEARISH"): (r_base + step,     s_base - step,     34),  # SR 13
        #     ("STRONG", "BULLISH", "STRONG", "STRONG"):  (r_base + step,     s_base + step,     35),  # SR 19

        #     # ── r=STRONG/BEARISH ─────────────────────────────────────
        #     ("STRONG", "BEARISH", "WTT",    "BULLISH"): (r_base,            s_base,            36),  # SR 18
        #     ("STRONG", "BEARISH", "WTT",    "BEARISH"): (r_base - step,     s_base - step,     37),  # SR 24
        #     ("STRONG", "BEARISH", "WTB",    "BULLISH"): (r_base,            s_base - step,     38),  # SR 22
        #     ("STRONG", "BEARISH", "WTB",    "BEARISH"): (r_base,            s_target - step,   39),  # SR 16
        #     ("STRONG", "BEARISH", "STRONG", "BULLISH"): (r_base + step,     s_base - step,     40),  # SR 12
        #     ("STRONG", "BEARISH", "STRONG", "BEARISH"): (r_base,            s_base - step,     41),  # SR 14
        #     ("STRONG", "BEARISH", "STRONG", "STRONG"):  (r_base - step,     s_base - step,     42),  # SR 20

        #     # ── r=STRONG/STRONG ──────────────────────────────────────
        #     ("STRONG", "STRONG", "WTT",    "BULLISH"): (r_base + step,      s_base,            43),  # R_OK 12 S_OK 9
        #     ("STRONG", "STRONG", "WTT",    "BEARISH"): (r_base,             s_base - step,     44),  # R_OK 15 S_OK 23
        #     ("STRONG", "STRONG", "WTB",    "BULLISH"): (r_base + step,      s_target + step,   45),  # R_OK 14   S_OK 18
        #     ("STRONG", "STRONG", "WTB",    "BEARISH"): (r_base,             s_target + step,   46),  # R_OK 11 S_OK 3
        #     ("STRONG", "STRONG", "STRONG", "BULLISH"): (r_base + step,      s_base,            47),  # RS 5
        #     ("STRONG", "STRONG", "STRONG", "BEARISH"): (r_base,             s_base - step,     48),  # RS 6
        #     ("STRONG", "STRONG", "STRONG", "STRONG"):  (r_base + step,      s_base - step,     49),  # R_OK 13 S_OK 13
        # }

        key = (r_status, res_view, s_status, sup_view)
        result = lookup.get(key)
        if result is None:
            # Fallback in case of an unmatched state
            safe_top, safe_bot, scenario_no = r_base + step, s_base - step, 0
        else:
            safe_top, safe_bot, scenario_no = result

        # Expand safe boundaries outwards in a tight squeeze
        gap = safe_top - safe_bot
        if gap == 0:
            # Expand by 1 step. This will result in exactly 3 distinct lines:
            # e.g., 22050 (safe_top), 22000 (both risky), 21950 (safe_bot)
            safe_top += step
            safe_bot -= step
        # elif gap <= 2 * step:
        #     # Expand by 1 step. This guarantees 4 distinct lines.
        #     safe_top += step
        #     safe_bot -= step

        # Two-line risky calculation
        risky_top = safe_top - step
        risky_bot = safe_bot + step

        return safe_top, risky_top, safe_bot, risky_bot, scenario_no

    safe_top, risky_top, safe_bot, risky_bot, scenario_no = get_range_boundary(r_status, res_view, r_strike, r_status_strike, s_status, sup_view, s_strike, s_status_strike, step=50)
    

    def _rev(strike, col):
        item = next((x for x in ed_list if x['strikePrice'] == strike), None)
        return item.get(col) if item else None

    safe_top_rev  = _rev(safe_top,  'call_reversal')
    risky_top_rev = _rev(risky_top, 'call_reversal')
    safe_bot_rev  = _rev(safe_bot,  'put_reversal')
    risky_bot_rev = _rev(risky_bot, 'put_reversal')

    # Shared payload
    payload = dict(
        underlyingAsset=symbol,           
        underlying_key=underlying_key,
        expiryDate=expiry_date,
        spotPrice=spot,                   
        ltt_index=current_timestamp,

        resistance_oi_strike=h_ce_oi['strikePrice'] if h_ce_oi else 0,   
        resistance_vol_strike=h_ce_vol['strikePrice'] if h_ce_vol else 0,
        resistance_oi_status=ce_oi_st,                 
        resistance_vol_status=ce_vol_st,
        resistance_oi_status_strike=ce_oi_at,          
        resistance_vol_status_strike=ce_vol_at,

        resistance_strike=r_strike,       
        resistance_status=r_status,
        resistance_status_strike=r_status_strike,

        support_oi_strike=h_pe_oi['strikePrice'] if h_pe_oi else 0,      
        support_vol_strike=h_pe_vol['strikePrice'] if h_pe_vol else 0,
        support_oi_status=pe_oi_st,                    
        support_vol_status=pe_vol_st,
        support_oi_status_strike=pe_oi_at,             
        support_vol_status_strike=pe_vol_at,

        support_strike=s_strike,          
        support_status=s_status,
        support_status_strike=s_status_strike,

        resistance_shifting_status=res_shift,          
        support_shifting_status=sup_shift,

        resistance_view=res_view,         
        support_view=sup_view,         
        final_view=final_view,
        scenario_no=scenario_no,

        risky_top_strike=risky_top,       
        safe_top_strike=safe_top,
        risky_bottom_strike=risky_bot,    
        safe_bottom_strike=safe_bot,
        
        risky_top_price=risky_top_rev,    
        safe_top_price=safe_top_rev,
        risky_bottom_price=risky_bot_rev, 
        safe_bottom_price=safe_bot_rev,
    )

    return payload

import asyncio
from asgiref.sync import sync_to_async

def _save_market_view_sync(payload, symbol, expiry_date):
    """Saves to DB and Cache synchronously."""
    try:
        # Create without auto fields
        market_view.objects.create(**payload)
        
        # Make a copy for cache because Django might add internal state
        cache_payload = make_json_safe(payload)
        cache_key = f'prev_market_view_entry_{symbol}_{expiry_date}'
        cache.set(cache_key, cache_payload, timeout=_CACHE_TIMEOUT)
    except Exception as e:
        _log.error(f"Error saving market view: {e}")

async def async_save_market_view(payload, symbol, expiry_date):
    """Wrapper to run the DB/Cache save asynchronously."""
    await sync_to_async(_save_market_view_sync)(payload, symbol, expiry_date)