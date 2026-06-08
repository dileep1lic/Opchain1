from django.test import TestCase

import re
from django.utils import timezone
from django.core.cache import caches

from .models import OptionChain, LiveSRData, PaperTrade


# ========================================================
# SMART CACHE OVERRIDE (FAILOVER MECHANISM)
# ========================================================
class SmartCache:
    def get(self, key, default=None):
        try:
            val = caches['default'].get(key)
            if val is not None:
                return val
        except Exception:
            pass
        try:
            val = caches['db_cache'].get(key)
            if val is not None:
                return val
        except Exception:
            pass
        return default

    def set(self, key, value, timeout=None):
        try:
            caches['default'].set(key, value, timeout)
        except Exception:
            pass
        try:
            caches['db_cache'].set(key, value, timeout)
        except Exception:
            pass


cache = SmartCache()


# ==========================================
# ACTIVE TRADE OVERRIDE HELPER
# ==========================================
def get_active_trade_data(symbol, selected_date):
    open_trade = PaperTrade.objects.filter(
        symbol=symbol,
        trade_date=selected_date,
        result__in=["OPEN", "PENDING"],
    ).first()
    if open_trade and open_trade.entry_strike:
        return float(open_trade.entry_strike), open_trade.trade_type
    return None, None


def get_master_levels(symbol, selected_date=None):
    if selected_date is None:
        selected_date = timezone.now().date()

    step      = 100 if "BANKNIFTY" in symbol or "SENSEX" in symbol else 50
    tolerance = 20.0

    levels = {
        "R": {"strike": 0, "entry": None, "target": None, "sl": None, "status": ""},
        "S": {"strike": 0, "entry": None, "target": None, "sl": None, "status": ""},
    }

    sr = (
        LiveSRData.objects
        .filter(Symbol__iexact=symbol, Time__date=selected_date)
        .order_by('-Time')
        .first()
    )
    if not sr:
        return levels

    def get_rev_val(strike, side, period=1):
        today = timezone.now().date()

        if selected_date == today:
            history_key  = f"moving_history_all_{symbol.upper()}"
            history_data = cache.get(history_key)
            strike_float = float(strike)

            if history_data and strike_float in history_data:
                full_hist    = history_data[strike_float].get('ce_hist' if side == 'CE' else 'pe_hist', [])
                last_n_ticks = full_hist[-period:]
                if last_n_ticks:
                    total_val, valid_count = 0.0, 0
                    for tick in last_n_ticks:
                        val = float(tick.get('value', 0))
                        if val > 0:
                            total_val  += val
                            valid_count += 1
                    if valid_count > 0:
                        return round(total_val / valid_count, 2)

        rows = (
            OptionChain.objects
            .filter(Symbol__iexact=symbol, Time__date=selected_date, Strike_Price=strike)
            .order_by('-Time')[:period]
        )
        print(f"DB Query for {symbol} {strike} {side} on {selected_date}")

        total_val, valid_count = 0.0, 0
        for row in rows:
            val = float(row.Reversl_Ce) if side == 'CE' else float(row.Reversl_Pe)
            if val and val > 0:
                total_val  += val
                valid_count += 1

        return round(total_val / valid_count, 2) if valid_count > 0 else None

    # ==========================================
    # PARSE SR STATUS
    # ==========================================
    res_status = str(sr.resistance_status).upper() if sr.resistance_status else ""
    res_base   = float(sr.resistance_strike) if sr.resistance_strike else 0
    m_res      = re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
    res_target = float(m_res.group(1)) if m_res else res_base

    sup_status = str(sr.supprt_status).upper() if sr.supprt_status else ""
    sup_base   = float(sr.supprt_strike) if sr.supprt_strike else 0
    m_sup      = re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
    sup_target = float(m_sup.group(1)) if m_sup else sup_base

    def parse_status_type(status: str) -> str:
        """
        "Support (Vol) Shifted WTT 24400" → "SHIFTED WTT"
        "Support (Vol) WTB 24300"         → "WTB"
        "Strong Support"                  → "STRONG"
        """
        s = status.upper()
        if "SHIFTED" in s and "WTT" in s: return "SHIFTED WTT"
        if "SHIFTED" in s and "WTB" in s: return "SHIFTED WTB"
        if "WTT" in s:                    return "WTT"
        if "WTB" in s:                    return "WTB"
        if "STRONG" in s:                 return "STRONG"
        return ""

    # Parse करो
    res_type = parse_status_type(res_status)
    sup_type = parse_status_type(sup_status)

    # Number निकालो (Regex अब सिर्फ number के लिए)
    m_res = re.search(r'(\d{4,6})', res_status)
    m_sup = re.search(r'(\d{4,6})', sup_status)
    res_target = float(m_res.group(1)) if m_res else res_base
    sup_target = float(m_sup.group(1)) if m_sup else sup_base

    # ==========================================
    # RESISTANCE (PUT Trade के लिए)
    # ==========================================
    if res_type == "SHIFTED WTT": eff_res = res_base + step
    elif res_type == "SHIFTED WTB" : eff_res = res_base + step
    elif res_type == "WTT" : eff_res = res_target - step
    elif res_type == "WTB" : eff_res = res_base
    elif res_type == "STRONG" : eff_res = res_base + step
     
    else: eff_res = res_base + step

    # ✅ FIX Bug 2: float() cast — Decimal vs float mismatch fix
    last_put   = (
        PaperTrade.objects
        .filter(symbol=symbol, trade_date=selected_date, trade_type='PUT')
        .exclude(result='OPEN')
        .order_by('-exit_time')
        .first()
    )
    is_r_paused = (
        last_put is not None
        and last_put.result == 'SL'
        and float(last_put.entry_strike or 0) == float(eff_res)   # ← Fix
    )
    if is_r_paused:
        res_status += " SL HIT (PAUSED)"

    # ✅ FIX Bug 5: get_rev_val सिर्फ एक बार — result reuse
    r_entry_val = None
    # Repeat Shift सिर्फ तब जब SL नहीं लगा
    if not is_r_paused:
        r_entry_val = get_rev_val(eff_res, 'CE')
        if r_entry_val:                              # ← सिर्फ एक check
            r_already_traded = PaperTrade.objects.filter(
                symbol=symbol, trade_date=selected_date, trade_type='PUT',
                trigger_price__gte=r_entry_val - tolerance,
                trigger_price__lte=r_entry_val + tolerance,
            ).exists()
            if r_already_traded:                     # ← r_entry_val के अंदर
                # ✅ Shift से पहले check: नई shifted strike पर भी SL था?
                new_eff_res = eff_res + step
                last_put_on_new = PaperTrade.objects.filter(
                    symbol=symbol, trade_date=selected_date,
                    trade_type='PUT', result='SL',
                    entry_strike=new_eff_res
                ).exists()
                
                if last_put_on_new:
                    # नई strike पर भी SL था — और shift मत करो
                    is_r_paused = True
                    res_status += " SL HIT SHIFTED (PAUSED)"
                else:
                    eff_res = new_eff_res
                    res_status += " (REPEAT SHIFT)"
                    r_entry_val = get_rev_val(eff_res, 'CE')

    levels["R"]["status"] = res_status
    levels["R"]["strike"] = eff_res
    levels["R"]["entry"]  = r_entry_val                          # ← reuse, no duplicate call
    levels["R"]["target"] = get_rev_val(eff_res - step, 'CE')
    levels["R"]["sl"]     = get_rev_val(eff_res + step, 'CE')

    # ==========================================
    # SUPPORT (CALL Trade के लिए)
    # ==========================================
    if      sup_type =="SHIFTED WTT"    : eff_sup = sup_base - step
    elif    sup_type == "SHIFTED WTB"   : eff_sup = sup_base - step
    elif    sup_type == "WTT"           : eff_sup = sup_base 
    elif    sup_type == "WTB"           : eff_sup = sup_target + step
    elif    sup_type == "STRONG"        : eff_sup = sup_base - step
    else: eff_sup = sup_base - step

    # ✅ FIX Bug 2: float() cast
    last_call   = (
        PaperTrade.objects
        .filter(symbol=symbol, trade_date=selected_date, trade_type='CALL')
        .exclude(result='OPEN')
        .order_by('-exit_time')
        .first()
    )
    is_s_paused = (
        last_call is not None
        and last_call.result == 'SL'
        and float(last_call.entry_strike or 0) == float(eff_sup)  # ← Fix
    )
    if is_s_paused:
        sup_status += " SL HIT (PAUSED)"

    # ✅ FIX Bug 5: get_rev_val सिर्फ एक बार
    s_entry_val = None
    if not is_s_paused:
        s_entry_val = get_rev_val(eff_sup, 'PE')
        if s_entry_val:
            s_already_traded = PaperTrade.objects.filter(
                symbol=symbol, trade_date=selected_date, trade_type='CALL',
                trigger_price__gte=s_entry_val - tolerance,
                trigger_price__lte=s_entry_val + tolerance,
            ).exists()
            if s_already_traded:
                # ✅ Shift से पहले check: नई shifted strike पर भी SL था?
                new_eff_sup = eff_sup - step
                last_call_on_new = PaperTrade.objects.filter(
                    symbol=symbol, trade_date=selected_date,
                    trade_type='CALL', result='SL',
                    entry_strike=new_eff_sup
                ).exists()
                if last_call_on_new:
                    # नई strike पर भी SL था — और shift मत करो
                    is_s_paused = True
                    sup_status += " SL HIT SHIFTED (PAUSED)"
                else:  
                    eff_sup     = eff_sup - step
                    sup_status += " (REPEAT SHIFT)"
                    s_entry_val = get_rev_val(eff_sup, 'PE')   # shift के बाद नई strike

    levels["S"]["status"] = sup_status
    levels["S"]["strike"] = eff_sup
    levels["S"]["entry"]  = s_entry_val                          # ← reuse
    levels["S"]["target"] = get_rev_val(eff_sup + step, 'PE')
    levels["S"]["sl"]     = get_rev_val(eff_sup - step, 'PE')

    return levels
