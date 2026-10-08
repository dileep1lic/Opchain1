import re

STEP_MAP = {'NIFTY': 50, 'BANKNIFTY': 100, 'FINNIFTY': 50, 'MIDCPNIFTY': 25, 'SENSEX': 100}

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

def parse_sr_status(sr):
    """
    यह फंक्शन LiveSRData ऑब्जेक्ट से Resistance और Support के स्टेटस को
    पार्स (parse) करता है और सभी जरूरी वेरिएबल्स को एक डिक्शनरी में लौटाता है।
    """
    # ==========================================
    # PARSE RESISTANCE STATUS
    # ==========================================
    res_status = str(sr.resistance_status).upper() if sr.resistance_status else ""
    res_base   = float(sr.resistance_strike) if sr.resistance_strike else 0.0
    res_type   = parse_status_type(res_status)
    
    # Resistance के लिए Target Number निकालो
    m_res = re.search(r'(\d{4,6})', res_status)
    res_target = float(m_res.group(1)) if m_res else res_base

    # ==========================================
    # PARSE SUPPORT STATUS
    # ==========================================
    sup_status = str(sr.supprt_status).upper() if sr.supprt_status else ""
    sup_base   = float(sr.supprt_strike) if sr.supprt_strike else 0.0
    sup_type   = parse_status_type(sup_status)
    
    # Support के लिए Target Number निकालो
    m_sup = re.search(r'(\d{4,6})', sup_status)
    sup_target = float(m_sup.group(1)) if m_sup else sup_base

    return {
        "res_status": res_status,
        "res_base": res_base,
        "res_type": res_type,
        "res_target": res_target,
        "sup_status": sup_status,
        "sup_base": sup_base,
        "sup_type": sup_type,
        "sup_target": sup_target,
    }

def calculate_eff_res(res_type, sup_type, res_base, res_target, step):
    
    # ==========================================
    # RESISTANCE (PUT Trade के लिए)
    # ==========================================
    # --------res wtb-------------
    if res_type == "WTB"            and sup_type == "WTB"           : eff_res = res_target + step # test ok
    elif res_type == "WTB"          and sup_type == "WTT"           : eff_res = res_base + step # test ok
    # elif res_type == "WTB"          and sup_type == "STRONG"        : eff_res = res_base - step # test ok -50 point se kam h res updet krna h
    elif res_type == "WTB"          and sup_type == "STRONG"        :
        eff_res = res_base - step if (res_base - res_target) <= step else res_base # test ok
    elif res_type == "WTB"          and sup_type == "SHIFTED WTB"   : eff_res = res_base + step # test ok
    elif res_type == "WTB"          and sup_type == "SHIFTED WTT"   : eff_res = res_base        # test ok
    #--------res wtt-------------
    elif res_type == "WTT"          and sup_type == "WTB"           : 
        eff_res = res_target if (res_target - res_base) <= step else (res_target - step) 
    elif res_type == "WTT"          and sup_type == "WTT"           : eff_res = res_target + step
    elif res_type == "WTT"          and sup_type == "STRONG"        :
        eff_res = res_target if (res_target - res_base) <= step else (res_target - step) 
    elif res_type == "WTT"          and sup_type == "SHIFTED WTB"   : eff_res = res_target + step
    elif res_type == "WTT"          and sup_type == "SHIFTED WTT"   : eff_res = res_base 
    #--------res strong-------------
    elif res_type == "STRONG"       and sup_type == "WTB"           : eff_res = res_base + step # test ok temp
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

    return eff_res

def calculate_eff_sup(res_type, sup_type, sup_base, sup_target, step):
    # ==========================================
    # SUPPORT (CALL Trade के लिए)
    # ==========================================
        # --------sup wtb-------------
    if sup_type == "WTB"            and res_type == "WTB"           : eff_sup = sup_target 
    elif sup_type == "WTB"          and res_type == "WTT"           : eff_sup = sup_target 
    elif sup_type == "WTB" and res_type == "STRONG":
        eff_sup = sup_target if (sup_base - sup_target) <= step else (sup_target + step) # test ok
    elif sup_type == "WTB"          and res_type == "SHIFTED WTB"   : eff_sup = sup_base
    elif sup_type == "WTB"          and res_type == "SHIFTED WTT"   : eff_sup = sup_target 
    #--------sup wtt-------------
    elif sup_type == "WTT"          and res_type == "WTB"           : eff_sup = sup_base - step # test ok
    elif sup_type == "WTT"          and res_type == "WTT"           : eff_sup = sup_base 
    elif sup_type == "WTT"          and res_type == "STRONG"        : eff_sup = sup_base 
    elif sup_type == "WTT"          and res_type == "SHIFTED WTB"   : eff_sup = sup_base + step
    elif sup_type == "WTT"          and res_type == "SHIFTED WTT"   : eff_sup = sup_base - step
    #--------sup strong-------------
    elif sup_type == "STRONG"       and res_type == "WTB"           : eff_sup = sup_base - step # Test ok
    elif sup_type == "STRONG"       and res_type == "WTT"           : eff_sup = sup_base - step # test ok
    elif sup_type == "STRONG"       and res_type == "STRONG"        : eff_sup = sup_base - step
    elif sup_type == "STRONG"       and res_type == "SHIFTED WTB"   : eff_sup = sup_base 
    elif sup_type == "STRONG"       and res_type == "SHIFTED WTT"   : eff_sup = sup_base - step
    #--------sup shifted wtb-------------
    elif sup_type == "SHIFTED WTB"  and res_type == "WTB"           : eff_sup = sup_target 
    elif sup_type == "SHIFTED WTB"  and res_type == "WTT"           : eff_sup = sup_target + step
    elif sup_type == "SHIFTED WTB"  and res_type == "STRONG"        : eff_sup = sup_target + step # test ok
    elif sup_type == "SHIFTED WTB"  and res_type == "SHIFTED WTB"   : eff_sup = sup_target 
    elif sup_type == "SHIFTED WTB"  and res_type == "SHIFTED WTT"   : eff_sup = sup_target - step
    #--------sup shifted wtt-------------
    elif sup_type == "SHIFTED WTT"  and res_type == "WTB"           : eff_sup = sup_base - step # test ok
    elif sup_type == "SHIFTED WTT"  and res_type == "WTT"           : eff_sup = sup_base 
    elif sup_type == "SHIFTED WTT"  and res_type == "STRONG"        : eff_sup = sup_base - step
    elif sup_type == "SHIFTED WTT"  and res_type == "SHIFTED WTB"   : eff_sup = sup_base 
    elif sup_type == "SHIFTED WTT"  and res_type == "SHIFTED WTT"   : eff_sup = sup_base - step
    else:                                          eff_sup = sup_base - step

    return eff_sup
                                    