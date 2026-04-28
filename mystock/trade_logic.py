import re
from django.utils import timezone
from .models import OptionChain, LiveSRData, PaperTrade
from django.core.cache import cache
from django.utils import timezone
# ─── ACTIVE TRADE OVERRIDE FUNCTION ───
# इसे मुख्य फंक्शन के बाहर (ऊपर) रखा गया है ताकि कोड साफ रहे
# ==========================================
def get_active_trade_data(symbol, selected_date):
    """
    अगर कोई ट्रेड OPEN है, तो उसकी एंट्री स्ट्राइक और टाइप (CALL/PUT) रिटर्न करेगा।
    अगर कोई ट्रेड OPEN नहीं है, तो None रिटर्न करेगा।
    """
    open_trade = PaperTrade.objects.filter(symbol=symbol, trade_date=selected_date, result__in=["OPEN", "PENDING"]).first()
    
    if open_trade and open_trade.entry_strike:
        saved_entry_strike = float(open_trade.entry_strike)
        ttype = open_trade.trade_type
        return saved_entry_strike, ttype
    
    return None, None

def get_master_levels(symbol, selected_date=None):
    """
    पूरे प्रोजेक्ट का इकलौता मास्टर फंक्शन।
    यह चार्ट, बॉट और डैशबोर्ड सभी को एक ही समान (Common) लेवल्स देगा।
    """
    if selected_date is None:
        selected_date = timezone.now().date()
        
    step = 100 if "BANKNIFTY" in symbol or "SENSEX" in symbol else 50
    tolerance = 20.0 # रिपीट ट्रेड को इग्नोर करने के लिए टॉलरेंस पॉइंट्स
    
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
    def get_rev_val1(strike, side):
        row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=selected_date, Strike_Price=strike).order_by('-Time').first()
        if not row: return None
        return float(row.Reversl_Ce) if side == 'CE' else float(row.Reversl_Pe)
    
    # हेल्पर: डेटाबेस से Reversal वैल्यू का Average (SMA) लाने के लिए
    def get_rev_val2(strike, side, period=1):
        """
        period=5 का मतलब है कि यह पिछले 5 डेटा पॉइंट्स का एवरेज निकालेगा।
        आप इसे अपनी सुविधानुसार 3, 5 या 10 कर सकते हैं।
        """
        rows = OptionChain.objects.filter(
            Symbol__iexact=symbol, 
            Time__date=selected_date, 
            Strike_Price=strike
        ).order_by('-Time')[:period]
        
        if not rows: 
            return None
            
        total_val = 0.0
        valid_count = 0
        
        for row in rows:
            # चेक करें कि CE चाहिए या PE
            val = float(row.Reversl_Ce) if side == 'CE' else float(row.Reversl_Pe)
            
            # सिर्फ तभी जोड़ें जब वैल्यू 0 से बड़ी और वैलिड हो
            if val and val > 0:
                total_val += val
                valid_count += 1
                
        # एवरेज निकाल कर 2 डेसिमल तक राउंड करें
        if valid_count > 0:
            return round(total_val / valid_count, 2)
        else:
            return None

    def get_rev_val(strike, side, period=1):
        """
        period=5 का मतलब है कि यह पिछले 5 डेटा पॉइंट्स का एवरेज निकालेगा।
        Hybrid Logic:
        - अगर period=1 और आज की डेट है -> Cache से लाएगा (0 DB Queries, Super Fast)
        - अगर period > 1 या पुरानी डेट है -> Database से लाएगा (Fallback)
        """
        # 1. 🟢 Cache Logic (सिर्फ आज के लेटेस्ट डेटा के लिए)
        today = timezone.now().date()
        # मान लेते हैं selected_date और symbol आपके outer function से आ रहे हैं
        if period == 1 and selected_date == today:
            live_data = cache.get(f'live_nifty_data_{symbol.upper()}')
            
            if live_data:
                # Cache (List of Dicts) में से वो row ढूंढें जिसका Strike मैच करता हो
                for row in live_data:
                    if row.get('Strike_Price') == strike:
                        # CE या PE वैल्यू निकालें
                        val = row.get('Reversl_Ce') if side == 'CE' else row.get('Reversl_Pe')
                        
                        if val is not None and float(val) > 0:
                            return round(float(val), 2)
                        return None # अगर वैल्यू 0 या None है

        # 2. 🔴 Database Logic (SMA के लिए या पुरानी डेट के लिए)
        rows = OptionChain.objects.filter(
            Symbol__iexact=symbol, 
            Time__date=selected_date, 
            Strike_Price=strike
        ).order_by('-Time')[:period]
        
        if not rows: 
            return None
            
        total_val = 0.0
        valid_count = 0
        
        for row in rows:
            val = float(row.Reversl_Ce) if side == 'CE' else float(row.Reversl_Pe)
            if val and val > 0:
                total_val += val
                valid_count += 1
                
        if valid_count > 0:
            return round(total_val / valid_count, 2)
        else:
            return None
    
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

   # 👇 1. नया रेजिस्टेंस SL शिफ्टिंग लॉजिक 
    # चेक करें कि क्या आखिरी PUT ट्रेड में इसी स्ट्राइक पर SL लगा था?
    last_put = PaperTrade.objects.filter(symbol=symbol, trade_date=selected_date, trade_type='PUT').exclude(result='OPEN').order_by('-exit_time').first()
    
    if last_put and last_put.result == 'SL' and last_put.entry_strike == eff_res:
        eff_res = eff_res + step # रेजिस्टेंस को एक स्ट्राइक ऊपर खिसका दें
        res_status = res_status + " (POST-SL SHIFT)"
 
    # 👇 2. AVOID REPEAT लॉजिक (नया)
    r_entry_val = get_rev_val(eff_res, 'CE')
    if r_entry_val:
        # चेक करें कि क्या इस भाव के आस-पास पहले ही PUT ट्रेड हो चुका है?
        r_already_traded = PaperTrade.objects.filter(
            symbol=symbol, trade_date=selected_date, trade_type='PUT',
            trigger_price__gte=r_entry_val + tolerance, 
            trigger_price__lte=r_entry_val - tolerance
        ).exists()
        
        if r_already_traded:
            eff_res = eff_res + step # रेजिस्टेंस को एक स्ट्राइक ऊपर खिसका दें
            res_status += " (REPEAT SHIFT)"

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

    # 👇 3. नया सपोर्ट SL शिफ्टिंग लॉजिक 
    # चेक करें कि क्या आखिरी CALL ट्रेड में इसी स्ट्राइक पर SL लगा था?
    last_call = PaperTrade.objects.filter(symbol=symbol, trade_date=selected_date, trade_type='CALL').exclude(result='OPEN').order_by('-exit_time').first()
    
    if last_call and last_call.result == 'SL' and last_call.entry_strike == eff_sup:
        eff_sup = eff_sup - step # सपोर्ट को एक स्ट्राइक नीचे खिसका दें
        sup_status = sup_status + " (POST-SL SHIFT)"

    # 👇 4. AVOID REPEAT लॉजिक (नया)
    s_entry_val = get_rev_val(eff_sup, 'PE')
    if s_entry_val:
        # चेक करें कि क्या इस भाव के आस-पास पहले ही CALL ट्रेड हो चुका है?
        s_already_traded = PaperTrade.objects.filter(
            symbol=symbol, trade_date=selected_date, trade_type='CALL',
            trigger_price__gte=s_entry_val - tolerance, 
            trigger_price__lte=s_entry_val + tolerance
        ).exists()
        
        if s_already_traded:
            eff_sup = eff_sup - step # सपोर्ट को एक स्ट्राइक नीचे खिसका दें
            sup_status += " (REPEAT SHIFT)"

    levels["S"]["status"] = sup_status
    levels["S"]["strike"] = eff_sup
    levels["S"]["entry"] = get_rev_val(eff_sup, 'PE')
    levels["S"]["target"] = get_rev_val(eff_sup + step, 'PE') # CALL का टारगेट ऊपर होता है
    levels["S"]["sl"] = get_rev_val(eff_sup - step, 'PE')     # CALL का SL नीचे होता है

    # ==========================================
    # ─── 🚀 ACTIVE TRADE OVERRIDE EXECUTION ───
    # ==========================================
    # यह चेक करेगा कि डेटाबेस में कोई ट्रेड चल रहा है या नहीं
    saved_entry_strike, ttype = get_active_trade_data(symbol, selected_date)

    if saved_entry_strike is not None:
        if ttype == 'CALL':
            # अगर CALL ट्रेड OPEN है, तो पूरा का पूरा 'S' ब्लॉक डेटाबेस वाली स्ट्राइक पर लॉक कर दें
            # levels["S"]["strike"] = saved_entry_strike
            # levels["S"]["entry"] = get_rev_val(saved_entry_strike, 'PE')
            levels["S"]["target"] = get_rev_val(saved_entry_strike + step, 'PE')
            levels["S"]["sl"] = get_rev_val(saved_entry_strike - step, 'PE')
            levels["S"]["status"] = f"{sup_status or ''} ACTIVE CALL TRADE [DB LOCKED] {saved_entry_strike}"
            
        elif ttype == 'PUT':
            # अगर PUT ट्रेड OPEN है, तो पूरा का पूरा 'R' ब्लॉक डेटाबेस वाली स्ट्राइक पर लॉक कर दें
            # levels["R"]["strike"] = saved_entry_strike
            # levels["R"]["entry"] = get_rev_val(saved_entry_strike, 'CE')
            levels["R"]["target"] = get_rev_val(saved_entry_strike - step, 'CE')
            levels["R"]["sl"] = get_rev_val(saved_entry_strike + step, 'CE')
            levels["R"]["status"] = f"{res_status or ''} ACTIVE PUT TRADE [DB LOCKED] {saved_entry_strike}"

    return levels

    