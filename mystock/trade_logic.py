import re
from django.utils import timezone
from .models import OptionChain, LiveSRData

def get_master_levels(symbol, selected_date=None):
    """
    पूरे प्रोजेक्ट का इकलौता मास्टर फंक्शन।
    यह चार्ट, बॉट और डैशबोर्ड सभी को एक ही समान (Common) लेवल्स देगा।
    """
    if selected_date is None:
        selected_date = timezone.now().date()
        
    step = 100 if "BANKNIFTY" in symbol or "SENSEX" in symbol else 50
    
    # डिफ़ॉल्ट आउटपुट डिक्शनरी
    levels = {
        "R": {"strike": 0, "entry": None, "target": None, "sl": None, "status": ""},
        "S": {"strike": 0, "entry": None, "target": None, "sl": None, "status": ""}
    }

    # 1. SR डेटा लाएं
    sr = LiveSRData.objects.filter(Symbol__iexact=symbol, Time__date=selected_date).order_by('-Time').first()
    if not sr:
        return levels

    # हेल्पर: डेटाबेस से Reversal वैल्यू लाने के लिए
    def get_rev_val(strike, side):
        row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=selected_date, Strike_Price=strike).order_by('-Time').first()
        if not row: return None
        return float(row.Reversl_Ce) if side == 'CE' else float(row.Reversl_Pe)

    # ==========================================
    # ─── RESISTANCE LOGIC (CALL SIDE) ───
    # ==========================================
    res_status = str(sr.resistance_status).upper() if sr.resistance_status else ""
    res_base = float(sr.resistance_strike) if sr.resistance_strike else 0
    m_res = re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
    res_target = float(m_res.group(1)) if m_res else res_base

    # आपकी फिक्स की गई शिफ्टिंग लॉजिक
    if "SHIFTED WTT" in res_status: eff_res = res_base + step
    elif "SHIFTED WTB" in res_status: eff_res = res_base + step
    elif "WTT" in res_status: eff_res = res_target - step
    elif "WTB" in res_status: eff_res = res_target + step
    elif "STRONG" in res_status: eff_res = res_base + step
    else: eff_res = res_base + step

    levels["R"]["status"] = res_status
    levels["R"]["strike"] = eff_res
    levels["R"]["entry"] = get_rev_val(eff_res, 'CE')
    levels["R"]["target"] = get_rev_val(eff_res - step, 'CE') # PUT का टारगेट नीचे होता है
    levels["R"]["sl"] = get_rev_val(eff_res + step, 'CE')     # PUT का SL ऊपर होता है

    # ==========================================
    # ─── SUPPORT LOGIC (PUT SIDE) ───
    # ==========================================
    sup_status = str(sr.supprt_status).upper() if sr.supprt_status else ""
    sup_base = float(sr.supprt_strike) if sr.supprt_strike else 0
    m_sup = re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
    sup_target = float(m_sup.group(1)) if m_sup else sup_base

    # आपकी फिक्स की गई शिफ्टिंग लॉजिक
    if "SHIFTED WTT" in sup_status: eff_sup = sup_base - step
    elif "SHIFTED WTB" in sup_status: eff_sup = sup_base - step
    elif "WTT" in sup_status: eff_sup = sup_target - step
    elif "WTB" in sup_status: eff_sup = sup_target + step
    elif "STRONG" in sup_status: eff_sup = sup_base - step
    else: eff_sup = sup_base - step

    levels["S"]["status"] = sup_status
    levels["S"]["strike"] = eff_sup
    levels["S"]["entry"] = get_rev_val(eff_sup, 'PE')
    levels["S"]["target"] = get_rev_val(eff_sup + step, 'PE') # CALL का टारगेट ऊपर होता है
    levels["S"]["sl"] = get_rev_val(eff_sup - step, 'PE')     # CALL का SL नीचे होता है

    return levels

    