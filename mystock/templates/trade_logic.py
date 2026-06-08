from django.test import TestCase

import re
from django.utils import timezone
from django.core.cache import caches
from django.db.models import Q
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
    tolerance = 30.0

    levels = {
        "R": {"strike": 0, "entry": None, "target": None, "sl": None, "status": "", "tag": "R"},
        "S": {"strike": 0, "entry": None, "target": None, "sl": None, "status": "", "tag": "S"},
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

    # ✅ FIX Bug 2: float() cast — Decimal vs float mismatch fix
    last_put   = (
        PaperTrade.objects
        .filter(symbol=symbol, trade_date=selected_date, trade_type='PUT')
        .exclude(result='OPEN')
        .order_by('-exit_time')
        .first()
    )

   
    # 🟢 सबसे पहले मेन (API वाले) रेजिस्टेंस को याद कर लो
    original_eff_res = eff_res  
    
    # ── 1. पॉज़ चेक ──
    is_r_paused = (
        last_put is not None
        and last_put.result == 'SL'
        and (
            float(last_put.entry_strike or 0) == float(eff_res) 
            or float((last_put.entry_strike or 0) + step) == float(eff_res)
        )
    )

    # ── 2. Forced Shift (यहाँ eff_res बदल सकता है, लेकिन original_eff_res 22000 ही रहेगा) ──
    if is_r_paused:
        last_entry_strike = float(last_put.entry_strike or 0)
        actual_sl_strike = last_entry_strike + step
        new_safe_res = actual_sl_strike + step
        
        eff_res = new_safe_res  # eff_res बदल गया
        is_r_paused = False     
        res_status += f" (FORCED SHIFT TO {eff_res})"

    # ── 3. Repeat Shift और Loop ──
    r_entry_val = None
    if not is_r_paused:
        r_entry_val = get_rev_val(eff_res, 'CE')
        
        while r_entry_val: 
            # 🟢 टैग का फैसला: क्या मौजूदा eff_res, असली original_eff_res के बराबर है?
            current_tag = 'R' if eff_res == original_eff_res else 'R_SHIFTED'
            
            r_already_traded = PaperTrade.objects.filter(
                symbol=symbol, trade_date=selected_date, trade_type='PUT',
                trigger_level=current_tag,  # 👈 सिर्फ इस टैग के ट्रेड चेक करेगा
                trigger_price__gte=r_entry_val - tolerance,
                trigger_price__lte=r_entry_val + tolerance,
            ).exists()
            
            is_dangerous_level = PaperTrade.objects.filter(
                Q(entry_strike=eff_res) | Q(entry_strike=eff_res - step), 
                symbol=symbol, trade_date=selected_date,
                trade_type='PUT', result='SL'
            ).exists()
            
            if r_already_traded or is_dangerous_level:
                eff_res = eff_res + step
                res_status += " (SHIFTED UP)"
                r_entry_val = get_rev_val(eff_res, 'CE') 
            else:
                break


    levels["R"]["status"] = res_status
    levels["R"]["strike"] = eff_res
    levels["R"]["entry"]  = r_entry_val                          # ← reuse, no duplicate call
    levels["R"]["target"] = get_rev_val(eff_res - step, 'CE')
    levels["R"]["sl"]     = get_rev_val(eff_res + step, 'CE')
    levels["R"]["tag"]    = 'R' if eff_res == original_eff_res else 'R_SHIFTED'

    # ==========================================
    # SUPPORT (CALL Trade के लिए)
    # ==========================================
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
    elif sup_type == "STRONG"       and res_type == "WTB"           : eff_sup = sup_base - step # Test ok
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

    # ✅ FIX Bug 2: float() cast
    last_call   = (
        PaperTrade.objects
        .filter(symbol=symbol, trade_date=selected_date, trade_type='CALL')
        .exclude(result='OPEN')
        .order_by('-exit_time')
        .first()
    )

 
    # 🟢 1. लूप शुरू होने से पहले असली (Main) सपोर्ट को याद रखें
    original_eff_sup = eff_sup  
    
    # ── 1. पॉज़ चेक (Pause Check) ──
    is_s_paused = (
        last_call is not None
        and last_call.result == 'SL'
        and (
            float(last_call.entry_strike or 0) == float(eff_sup) 
            or float((last_call.entry_strike or 0) - step) == float(eff_sup)
        )
    )

    # ── 2. Forced Shift Logic (अगर सपोर्ट पर SL कटा है, तो नीचे शिफ्ट हो जाओ) ──
    if is_s_paused:
        # 1. पता करें कि पिछली एंट्री कहाँ हुई थी? (उदा: 22000)
        last_entry_strike = float(last_call.entry_strike or 0)
        
        # 2. असली SL कहाँ हिट हुआ था? (Entry से 1 step नीचे, उदा: 21950)
        actual_sl_strike = last_entry_strike - step
        
        # 3. नया सुरक्षित सपोर्ट इस SL वाली स्ट्राइक से भी 1 step नीचे होगा (उदा: 21900)
        new_safe_sup = actual_sl_strike - step
        
        eff_sup = new_safe_sup  # हमारा नया सपोर्ट अब यह है
        is_s_paused = False     # बॉट को अन-पॉज़ (Unpause) कर दें
        sup_status += f" (FORCED SHIFT TO {eff_sup})"

    # ── 3. Repeat Shift और 'Skip Bad Level' Logic (while लूप के साथ) ──
    s_entry_val = None
    if not is_s_paused:
        s_entry_val = get_rev_val(eff_sup, 'PE')  # CALL की एंट्री PE के रिवर्सल से मिलती है
        
        while s_entry_val: 
            # 🟢 4. तय करें कि यह मेन लेवल है या शिफ्टेड? (Context Tag)
            current_tag = 'S' if eff_sup == original_eff_sup else 'S_SHIFTED'
            
            # चेक: क्या *इस टैग* के साथ यहाँ पहले ट्रेड हुआ है?
            s_already_traded = PaperTrade.objects.filter(
                symbol=symbol, trade_date=selected_date, trade_type='CALL',
                trigger_level=current_tag,  # 👈 सिर्फ मैचिंग टैग को देखेगा
                trigger_price__gte=s_entry_val - tolerance,
                trigger_price__lte=s_entry_val + tolerance,
            ).exists()
            
            # चेक: क्या यह लेवल खतरनाक है? (यहाँ पहले SL कटा था?)
            # ध्यान दें: सपोर्ट में ऊपर वाला लेवल (eff_sup + step) चेक करते हैं, क्योंकि गिरते हुए मार्केट में SL नीचे हिट होता है
            is_dangerous_level = PaperTrade.objects.filter(
                Q(entry_strike=eff_sup) | Q(entry_strike=eff_sup + step), 
                symbol=symbol, trade_date=selected_date,
                trade_type='CALL', result='SL'
            ).exists()
            
            # 🚀 अगर ट्रेड हो चुका है या लेवल खतरनाक है, तो - step करके नीचे खिसक जाओ!
            if s_already_traded or is_dangerous_level:
                eff_sup = eff_sup - step
                sup_status += " (SHIFTED DOWN)"
                s_entry_val = get_rev_val(eff_sup, 'PE') # नया प्राइस निकालो
            else:
                # लेवल एकदम फ्रेश और सुरक्षित है! लूप से बाहर आ जाओ।
                break
            # end testing================

    levels["S"]["status"] = sup_status
    levels["S"]["strike"] = eff_sup
    levels["S"]["entry"]  = s_entry_val                          # ← reuse
    levels["S"]["target"] = get_rev_val(eff_sup + step, 'PE')
    levels["S"]["sl"]     = get_rev_val(eff_sup - step, 'PE')
    levels["S"]["tag"]    = 'S' if eff_sup == original_eff_sup else 'S_SHIFTED'

    return levels
