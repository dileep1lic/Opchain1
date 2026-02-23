# async_live.py के टॉप पर
import logging
import aiohttp
import asyncio
import pandas as pd
from django.utils import timezone
from mystock.credentials import access_token  # सीधे क्रेडेंशियल्स से लें
from .symbol import symbols as SYMBOLS        # सिंबल लिस्ट के लिए
from asgiref.sync import sync_to_async
import numpy as np
from mystock.models import SupportResistance, ExpiryCache 
import requests
from datetime import timedelta


logger = logging.getLogger(__name__)

def get_instrument_key(symbol):
    """
    सिंबल के लिए Instrument Key निकालता है।
    Indices के लिए फिक्स्ड मैप और Stocks के लिए CSV (instrument_df) का उपयोग करता है।
    """
    global instrument_df
    
    # 1. Indices के लिए हार्डकोडेड मैपिंग (यह सबसे सुरक्षित और तेज़ है)
    indices_map = {
        'NIFTY': 'NSE_INDEX|Nifty 50',
        'BANKNIFTY': 'NSE_INDEX|Nifty Bank',
        'FINNIFTY': 'NSE_INDEX|Nifty Fin Service',
        'MIDCPNIFTY': 'NSE_INDEX|NIFTY MID SELECT',
        'SAMMAAN': 'NSE_EQ|INE148I01020',
        'M&M': 'NSE_EQ|INE101A01026',  
        'L&T': 'NSE_EQ|INE018A01030',
    }
    
    if symbol in indices_map:
        return indices_map[symbol]

    # 2. अगर फाइल लोड नहीं है, तो लोड करें
    if instrument_df is None:
        load_master_contract()

    try:
        # 3. Stocks के लिए 'NSE_EQ' (Equity) सेगमेंट में ढूंढें
        # Option Chain के लिए हमें Underlying (Equity) की Key चाहिए होती है।
        
        # फिल्टर: ट्रेडिंग सिंबल मैच हो और एक्सचेंज NSE_EQ हो
        stock_row = instrument_df[
            (instrument_df['tradingsymbol'] == symbol) & 
            (instrument_df['exchange'] == 'NSE_EQ')
        ]

        if not stock_row.empty:
            return stock_row.iloc[0]['instrument_key']
        
        # 4. अगर NSE_EQ में नहीं मिला, तो BSE_EQ या किसी और में ढूंढें (Fallback)
        fallback_row = instrument_df[instrument_df['tradingsymbol'] == symbol]
        if not fallback_row.empty:
            return fallback_row.iloc[0]['instrument_key']

    except Exception as e:
        print(f"❌ Key Error for {symbol}: {e}")

    # अगर कुछ नहीं मिला
    return None

def get_Name_Lot_size(symbol):
    key = get_instrument_key(symbol)
    if not key:
        return None, None

    url = "https://api.upstox.com/v2/option/contract"
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {access_token}'
    }

    try:
        # Using requests.get instead of undefined safe_get
        res = requests.get(
            url,
            headers=headers,
            params={'instrument_key': key},
            timeout=10
        )
        response = res.json() if res.status_code == 200 else None

        # Agar response None hai ya data nahi mila
        if not response or "data" not in response or not response["data"]:
            print(f"⚠ No contract data found for {symbol}")
            return None, None

        # Pehla instrument lein (usually index ya stock contract)
        contract_data = response["data"][0]
        
        underlying = contract_data.get("underlying_symbol")
        lot_size = contract_data.get("lot_size")

        return underlying, lot_size

    except Exception as e:
        print(f"⚠ Lot size fetch failed for {symbol}: {str(e)}")
        return None, None


# ग्लोबल वेरिएबल ताकि फाइल एक ही बार लोड हो
instrument_df = None

def get_Name_Lot_size_Fast(symbol):
    """F&O लॉट साइज को प्राथमिकता देने वाला फ़ंक्शन"""
    global instrument_df
    if instrument_df is None:
        load_master_contract()

    try:
        # 1. सबसे पहले डेरिवेटिव्स (Options/Futures) में ढूंढें 
        # ताकि सही लॉट साइज (जैसे 3750, 71475) मिले
        derivatives = instrument_df[
            (instrument_df['tradingsymbol'].str.startswith(symbol, na=False)) & 
            (instrument_df['instrument_type'].isin(['OPTSTK', 'FUTSTK', 'OPTIDX', 'FUTIDX']))
        ]

        if not derivatives.empty:
            # पहली वैलिड रो चुनें जहाँ लॉट साइज हो
            row = derivatives.dropna(subset=['lot_size']).iloc[0]
            name = row['name']
            lot_size = int(row['lot_size'])
            return name, lot_size

        # 2. अगर डेरिवेटिव नहीं मिला, तो कैश (EQUITY) में ढूंढें
        exact_match = instrument_df[instrument_df['tradingsymbol'] == symbol]
        if not exact_match.empty:
            row = exact_match.iloc[0]
            name = row.get('name', symbol)
            lot_size = int(row.get('lot_size', 1)) if pd.notna(row.get('lot_size')) else 1
            return name, lot_size

    except Exception as e:
        # अगर कुछ गड़बड़ हो तो डिफ़ॉल्ट वैल्यू भेजें
        pass

    return symbol, 1

import os

def load_master_contract():
    global instrument_df
    if instrument_df is not None:
        return

    file_path = 'complete.csv'
    
    # अगर फाइल पुरानी है या नहीं है, तो डाउनलोड करें
    # (आप चाहें तो इसे रोज़ एक बार डाउनलोड करने का लॉजिक लगा सकते हैं)
    if not os.path.exists(file_path):
        print("📥 Downloading latest master contract...")
        url = "https://assets.upstox.com/feed/instruments/nse-eq.csv.gz" 
        # नोट: हम सीधे NSE Equity ले रहे हैं ताकि फाइल छोटी रहे और तेज़ चले
        # अगर आपको पूरा चाहिए तो: https://assets.upstox.com/feed/instruments/complete.csv.gz
        
        # यहाँ हम complete.csv ही यूज़ करेंगे जैसा आपका कोड है
        url = "https://assets.upstox.com/feed/instruments/complete.csv.gz"
        
        response = requests.get(url)
        with open(file_path, 'wb') as f:
            f.write(response.content)
        print("✅ Download Complete!")

    try:
        # फाइल लोड करें (Pandas gzip को खुद संभाल लेता है अगर एक्सटेंशन .gz हो, 
        # लेकिन अगर आपने unzip करके .csv सेव की है तो ये कोड है)
        instrument_df = pd.read_csv(file_path)
        
        # कॉलम के नाम साफ़ करें और स्ट्रिंग बनाएं
        instrument_df['tradingsymbol'] = instrument_df['tradingsymbol'].astype(str).str.strip()
        instrument_df['exchange'] = instrument_df['exchange'].astype(str).str.strip()
        
        print(f"✅ Master File Loaded! Total Instruments: {len(instrument_df)}")
    except Exception as e:
        print(f"❌ File Load Error: {e}")

# ---------------------------------------------------------
# NEW SMART EXPIRY LOGIC START
# ---------------------------------------------------------

def get_all_expiries_from_api(symbol):
    """API से सभी Expiry Dates निकालकर सॉर्टेड लिस्ट देता है"""
    try:
        key = get_instrument_key(symbol)
        if not key: return []

        url = "https://api.upstox.com/v2/option/contract"
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}
        
        # API Call
        res = requests.get(url, headers=headers, params={"instrument_key": key}, timeout=10).json()
        
        if "data" in res and res["data"]:
            # सारी डेट्स निकालें
            all_dates = [item["expiry"] for item in res["data"]]
            # डुप्लिकेट हटाकर सॉर्ट करें
            sorted_expiries = sorted(list(set(all_dates)))
            return sorted_expiries
            
    except Exception as e:
        logger.error(f"Expiry API fetch fail for {symbol}: {e}")
    
    return []

def get_storage_key(symbol):
    """तय करता है कि DB में किस नाम से सेव करना है"""
    indices = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]
    if symbol in indices:
        return symbol
    else:
        return "STOCK_MONTHLY" # सभी स्टॉक्स के लिए एक ही की (Key)

def get_smart_expiry(symbol):
    """
    1. DB चेक करता है (Smart Key के साथ)
    2. अगर नहीं मिलता तो API कॉल करता है
    3. लिस्ट रिटर्न करता है (e.g., ['2026-02-05', '2026-02-12'])
    """
    db_key = get_storage_key(symbol)
    today_str = str(timezone.now().date())

    # 1. DB Check
    try:
        cache_entry = ExpiryCache.objects.get(symbol=db_key)
        
        # अगर डेटा आज का है और लिस्ट खाली नहीं है
        if cache_entry.is_data_fresh() and cache_entry.expiries:
            # चेक करें कि पहली एक्सपायरी बीत तो नहीं गई
            if cache_entry.expiries[0] >= today_str:
                # logger.info(f"✅ Found in DB: {db_key} (for {symbol})")
                return cache_entry.expiries
    except ExpiryCache.DoesNotExist:
        pass

    # 2. API Fetch (अगर DB में नहीं मिला)
    logger.info(f"🔄 Fetching fresh Expiry from API for {symbol} ({db_key})...")
    
    # अगर हमें STOCK_MONTHLY चाहिए, तो हम API को किसी एक स्टॉक का नाम देंगे (जैसे RELIANCE)
    # ताकि हमें सही मंथली डेट्स मिलें।
    api_symbol = symbol
    if db_key == "STOCK_MONTHLY" and symbol == "STOCK_MONTHLY":
        api_symbol = "RELIANCE" 
    
    fresh_list = get_all_expiries_from_api(api_symbol)

    if fresh_list:
        # 3. Save to DB (update_or_create सबसे बेस्ट है)
        ExpiryCache.objects.update_or_create(
            symbol=db_key,
            defaults={'expiries': fresh_list} # last_updated ऑटो उपडेट हो जायेगा
        )
        return fresh_list
    
    return []


import json  # Ensure json is imported at the top✅ फाइल सफलतापूर्वक लोड हो गई! कुल स्टॉक्स: 205312

semaphore = asyncio.Semaphore(5)  # एक समय में 5 API कॉल्स की अनुमति (Rate Limit Control)

async def get_option_chain_async(session, symbol, expiry_Date, retries=2):
    """
    Smart Async Function with Error Code Handling
    Based on Upstox Error Codes:
    - 400-410: Don't Retry (Code/Token issue)
    - 429: Rate Limit (Wait & Retry)
    - 500-503: Server Issue (Retry)
    """

    # 1. Basic Checks
    key = get_instrument_key(symbol)
    if not key:
        logger.error(f"❌ Key Missing for {symbol}")
        return None
    
    # 2. Setup
    url = "https://api.upstox.com/v2/option/chain"
    params = {"instrument_key": key, "expiry_date": str(expiry_Date)}
    headers = {"Accept": "application/json", "Authorization": f"Bearer {access_token}"}
    timeout = aiohttp.ClientTimeout(total=15)

    async with semaphore:  # Rate Limit Control
        for attempt in range(retries + 1):
            try:
                async with session.get(url, params=params, headers=headers, timeout=timeout) as res:
                    
                    # --- STATUS CODE HANDLING ---
                    
                    # ✅ 200 OK: सब सही है
                    if res.status == 200:
                        try:
                            data = await res.json()
                            if data.get("data"):
                                return data
                            else:
                                logger.warning(f"⚠️ {symbol}: Data list is empty.")
                                return None # खाली डेटा पर Retry न करें
                        except Exception as e:
                            logger.error(f"❌ {symbol}: JSON Decode Error: {e}")
                            return None

                    # ⏳ 429: Too Many Requests (Slow Down!)
                    elif res.status == 429:
                        wait_time = 2 ** (attempt + 1) # 2s, 4s, 8s
                        logger.warning(f"⚠️ {symbol}: Rate Limit (429). Waiting {wait_time}s...")
                        await asyncio.sleep(wait_time)
                        continue # Retry loop

                    # ❌ 400, 401, 403, 404: Client Errors (Don't Retry)
                    elif 400 <= res.status < 500:
                        text = await res.text()
                        logger.error(f"❌ {symbol}: Critical Error {res.status} | {text}")
                        # 401 Unauthorized मतलब टोकन एक्सपायर, तुरंत रोक दें
                        if res.status == 401:
                            logger.critical("STOP: API Token is Invalid/Expired!")
                        return None # लूप तोड़ दें, Retry का फायदा नहीं

                    # 🔄 500, 503: Server Errors (Retry)
                    elif res.status >= 500:
                        logger.warning(f"🔥 {symbol}: Server Error {res.status}. Retrying...")
                        # Loop अपने आप Retry करेगा

            except asyncio.TimeoutError:
                logger.warning(f"⏳ {symbol}: Timeout (Attempt {attempt+1})")
            
            except aiohttp.ClientError as e:
                logger.error(f"🌐 {symbol}: Network Error: {e}")

            # अगर यहाँ पहुंचे हैं मतलब Retry करना है (429 या 500 या Timeout के केस में)
            if attempt < retries:
                await asyncio.sleep(1) # थोड़ा रुकें

    logger.error(f"❌ {symbol}: Failed after all attempts.")
    return None

async def calculate_data_async_optimized(session, symbol, expiry_Date):
    """पूरी कैलकुलेशन प्रोसेस"""
    # if symbol == "NIFTY":
    #     expiry_Date = '2026-02-17'
    
    response_data = await get_option_chain_async(session, symbol, expiry_Date)
    

    if not response_data or 'data' not in response_data:
        logger.warning(f"⚠️ डेटा नहीं मिला: {symbol} तारीख {expiry_Date}")
        return None

    try:
        data_list = response_data['data']
        spot_price = response_data.get('underlying_spot_price') or data_list[0].get('underlying_spot_price', 0)
        
        _, lot_size = get_Name_Lot_size_Fast(symbol)

        lot_size = lot_size  if lot_size and lot_size > 0 else 1
  
        rows = []
        for entry in data_list:
            ce_obj = entry.get("call_options") or {}
            pe_obj = entry.get("put_options") or {}
            ce_md = ce_obj.get("market_data") or {}
            pe_md = pe_obj.get("market_data") or {}
            ce_g = ce_obj.get("option_greeks") or {}
            pe_g = pe_obj.get("option_greeks") or {}
            
            rows.append({
                "Time": timezone.now(),
                "Symbol": symbol,
                "expiry": expiry_Date,  
                "Lot_size": lot_size,
                "Strike_Price": entry.get("strike_price"),
                "Spot_Price": spot_price,
                "CE_Delta": ce_g.get("delta", 0),
                "PE_Delta": pe_g.get("delta", 0),
                "CE_OI": ce_md.get("oi", 0) / lot_size,
                "PE_OI": pe_md.get("oi", 0) / lot_size,
                "CE_CLTP": ce_md.get("ltp", 0) - ce_md.get("close_price", 0),
                "PE_CLTP": pe_md.get("ltp", 0) - pe_md.get("close_price", 0),
                "CE_LTP": ce_md.get("ltp", 0),
                "PE_LTP": pe_md.get("ltp", 0),
                "CE_Volume": ce_md.get("volume", 0) / lot_size,
                "PE_Volume": pe_md.get("volume", 0) / lot_size,
                "CE_COI": (ce_md.get("oi", 0) - ce_md.get("prev_oi", 0)) / lot_size,
                "PE_COI": (pe_md.get("oi", 0) - pe_md.get("prev_oi", 0)) / lot_size,
                "CE_IV": ce_g.get("iv", 0),
                "PE_IV": pe_g.get("iv", 0),
            })

        df = pd.DataFrame(rows)
        if df.empty: return None

        # Vectorized Calculations
        # df["Reversl_Ce"] = ((df["PE_LTP"] - df["CE_LTP"].shift(-1)) + spot_price).round(2)
        # df["Reversl_Pe"] = ((df["PE_LTP"].shift(1) - df["CE_LTP"]) + spot_price).round(2)
      
        # पहले पूरी गणना करें (बिना राउंड किए CE और PE दोनों के लिए)
        calculation_ce = (
                    ((df["PE_LTP"] - df["CE_LTP"].shift(-1))) / 
                    ((df["CE_Delta"].shift(-1) - df["PE_Delta"]))
                    ) + spot_price

        # अब इसे 0.05 के निकटतम गुणज (Multiple) पर राउंड करें
        df["Reversl_Ce"] = ((calculation_ce / 0.05).round() * 0.05).round(2)
        # PE के लिए भी यही करें
        calculation_pe = (
                    ((df["PE_LTP"].shift(1) - df["CE_LTP"])) / 
                    ((df["CE_Delta"] - df["PE_Delta"].shift(1)))
                    ) + spot_price
        # अब इसे 0.05 के निकटतम गुणज (Multiple) पर राउंड करें
        df["Reversl_Pe"] = ((calculation_pe / 0.05).round() * 0.05).round(2)


        ce_oi = df["CE_OI"].replace(0, np.nan)
        pe_oi = df["PE_OI"].replace(0, np.nan)
        df["CE_RANGE"] = ((np.maximum(ce_oi - pe_oi, 0) / ce_oi) * 100).round(2).fillna(0)
        df["PE_RANGE"] = ((np.maximum(pe_oi - ce_oi, 0) / pe_oi) * 100).round(2).fillna(0)

        for col in ["CE_OI", "PE_OI", "CE_Volume", "PE_Volume", "CE_COI", "PE_COI"]:
            max_v = df[col].max()
            df[f"{col}_percent"] = ((df[col] / max_v) * 100).round(2) if max_v > 0 else 0

        return df.fillna(0)
    except Exception as e:
        logger.error(f"❌ Calc Error {symbol}: {e}")
        return None

@sync_to_async
def save_sr_async_wrapper(df, symbol):
    return save_top2_support_resistance(df, symbol)

def build_pe_ce_logic(df):
    """डेटा से रेजिस्टेंस और सपोर्ट लेवल्स निकालना (Updated for Shifted Reversal Values)"""
    result = {
        "Time": df["Time"].iloc[0],
        "Symbol": df["Symbol"].iloc[0],
        "Spot Price": float(df["Spot_Price"].iloc[0]),
        "expiry": df["expiry"].iloc[0]  # Expiry को भी रिजल्ट में शामिल करें
    }

    for side in ["PE", "CE"]:
        col = f"{side}_OI_percent"
        # सबसे ज्यादा OI वाले 2 स्ट्राइक प्राइस निकालना
        sorted_df = df.sort_values(col, ascending=False).reset_index(drop=True)
        
        if len(sorted_df) >= 2:
            s1, s2 = sorted_df.iloc[0], sorted_df.iloc[1]
            side_lower = side.lower() # 'pe' या 'ce'
            
            # WTB/WTT/Strong Logic
            result[f"s_t_b_{side_lower}"] = (
                "Strong" if s2[col] < 75 else
                "WTB" if s2["Strike_Price"] < s1["Strike_Price"] else
                "WTT"
            )
            
            # --- NEW LOGIC START: Reversal Value Shift ---
            reversl_col = f"Reversl_{side.capitalize()}" # Reversl_Ce or Reversl_Pe
            
            if side == "CE":
                # CE के लिए: इससे बड़ी (Next Higher) स्ट्राइक ढूंढें
                # s1 के लिए
                next_strike_s1 = df[df["Strike_Price"] > s1["Strike_Price"]].sort_values("Strike_Price")
                rev_val_s1 = next_strike_s1.iloc[0][reversl_col] if not next_strike_s1.empty else 0
                
                # s2 के लिए
                next_strike_s2 = df[df["Strike_Price"] > s2["Strike_Price"]].sort_values("Strike_Price")
                rev_val_s2 = next_strike_s2.iloc[0][reversl_col] if not next_strike_s2.empty else 0

            else: # PE Case
                # PE के लिए: इससे छोटी (Next Lower) स्ट्राइक ढूंढें
                # s1 के लिए
                prev_strike_s1 = df[df["Strike_Price"] < s1["Strike_Price"]].sort_values("Strike_Price", ascending=False)
                rev_val_s1 = prev_strike_s1.iloc[0][reversl_col] if not prev_strike_s1.empty else 0
                
                # s2 के लिए
                prev_strike_s2 = df[df["Strike_Price"] < s2["Strike_Price"]].sort_values("Strike_Price", ascending=False)
                rev_val_s2 = prev_strike_s2.iloc[0][reversl_col] if not prev_strike_s2.empty else 0
            
            # --- NEW LOGIC END ---

            # डेटा को रिजल्ट में सेव करना
            
            # 1. Strike 1 Data (Highest OI)
            result[f"Strike Price_{side}1"] = s1["Strike_Price"]
            result[f"Reversl {side}"] = rev_val_s1  # यहाँ अब अगली/पिछली स्ट्राइक की वैल्यू आएगी
            
            # 2. Strike 2 Data (2nd Highest OI)
            result[f"Strike Price_{side}2"] = s2["Strike_Price"]
            result[f"Reversl {side}2"] = rev_val_s2 # s2 की शिफ्टेड रिवर्सल वैल्यू
            
            result[f"week_{side} %"] = s2[col]
            
    return result


def save_top2_support_resistance(df, symbol):
    
    try:
        if df is None or df.empty: return False

        top_row = build_pe_ce_logic(df)
        spot = float(top_row["Spot Price"])
        
        # --- 1. Risk Logic & WTT/WTB ---
        bearish_val = int((df[(df["Strike_Price"] < spot)].tail(10)["CE_LTP"] == 0).sum())
        bullish_val = int((df[(df["Strike_Price"] > spot)].head(10)["PE_LTP"] == 0).sum())
        top_row["Bearish_Risk"] = bearish_val
        top_row["Bullish_Risk"] = bullish_val
        
        if top_row.get("s_t_b_ce") == "WTT": top_row["Bullish_Risk"] += 1
        if top_row.get("s_t_b_pe") == "WTB": top_row["Bearish_Risk"] += 1

        # --- 2. Stop Loss Calculation ---
        pe_top = df.nlargest(2, "PE_OI")
        ce_top = df.nlargest(2, "CE_OI")

        def calculate_stop_loss(full_df, strike, side):
            if side == "CE":
                filtered = full_df[full_df["Strike_Price"] > strike].sort_values("Strike_Price")
                col_name = "Reversl_Ce"
            else:
                filtered = full_df[full_df["Strike_Price"] < strike].sort_values("Strike_Price", ascending=False)
                col_name = "Reversl_Pe"
            return float(filtered.iloc[0][col_name]) if not filtered.empty else 0.0

        # Extract Strikes & Reversals
        pe1_strike = float(pe_top.iloc[0]["Strike_Price"])
        pe2_strike = float(pe_top.iloc[1]["Strike_Price"])
        rev_pe1 = float(pe_top.iloc[0]["Reversl_Pe"])
        rev_pe2 = float(pe_top.iloc[1]["Reversl_Pe"])

        ce1_strike = float(ce_top.iloc[0]["Strike_Price"])
        ce2_strike = float(ce_top.iloc[1]["Strike_Price"])
        rev_ce1 = float(ce_top.iloc[0]["Reversl_Ce"])
        rev_ce2 = float(ce_top.iloc[1]["Reversl_Ce"])

        # Calculate SL
        sl_pe1 = calculate_stop_loss(df, pe1_strike, "PE")
        sl_pe2 = calculate_stop_loss(df, pe2_strike, "PE")
        sl_ce1 = calculate_stop_loss(df, ce1_strike, "CE")
        sl_ce2 = calculate_stop_loss(df, ce2_strike, "CE")

        # --- 3. NEW: Calculate Distance for ALL 4 Levels ---
        def get_dist_percentage(spot_price, level_price):
            if spot_price > 0 and level_price > 0:
                return round((abs(level_price - spot_price) / spot_price) * 100, 2)
            return 0.0

        d_ce1 = get_dist_percentage(spot, rev_ce1)
        d_ce2 = get_dist_percentage(spot, rev_ce2)
        d_pe1 = get_dist_percentage(spot, rev_pe1)
        d_pe2 = get_dist_percentage(spot, rev_pe2)
        # ---------------------------------------------------
        expiry_val = top_row.get("expiry")
        
        # अगर expiry 0 है, None है या खाली स्ट्रिंग है, तो उसे None कर दें
        if not expiry_val or expiry_val == 0:
            expiry_val = None
            print(f"⚠️ Expiry value for {symbol} is invalid ({expiry_val}). Setting to None.")
        else:
            try:
                # पक्का करें कि यह स्ट्रिंग फॉर्मेट (YYYY-MM-DD) में हो
                expiry_val = str(expiry_val)
            except:
                expiry_val = None

        # --- 4. Database Save ---
        SupportResistance.objects.create(
            Time=timezone.localtime(),
            Symbol=symbol,
            Spot_Price=spot,
            Expiry_Date=expiry_val,
            
            # --- New 4 Distance Fields ---
            dist_ce_1=d_ce1,
            dist_ce_2=d_ce2,
            dist_pe_1=d_pe1,
            dist_pe_2=d_pe2,

            # PE Data इसे हटाना है
            Strike_Price_Pe1=pe1_strike,
            Reversl_Pe=rev_pe1,
            Stop_Loss_Pe1=sl_pe1,
            week_Pe_1=float(pe_top.iloc[0]["PE_OI_percent"]),
            
            
            Strike_Price_Pe2=pe2_strike,
            Reversl_Pe_2=rev_pe2,
            Stop_Loss_Pe2=sl_pe2,
            week_Pe_2=float(pe_top.iloc[1]["PE_OI_percent"]),
            
            s_t_b_pe=top_row.get("s_t_b_pe", ""),
            
            # CE Data इसे हटाना है
            Strike_Price_Ce1=ce1_strike,
            Reversl_Ce=rev_ce1,
            Stop_Loss_Ce1=sl_ce1,
            week_Ce_1=float(ce_top.iloc[0]["CE_OI_percent"]),
            
            Strike_Price_Ce2=ce2_strike,
            Reversl_Ce_2=rev_ce2,
            Stop_Loss_Ce2=sl_ce2,
            week_Ce_2=float(ce_top.iloc[1]["CE_OI_percent"]),
            
            s_t_b_ce=top_row.get("s_t_b_ce", ""),
            
            # Risks
            Bearish_Risk=top_row["Bearish_Risk"],
            Bullish_Risk=top_row["Bullish_Risk"]
        )
        return True
    except Exception as e:
        print(f"Error saving DB for {symbol}: {e}")
        return False

from mystock.models import SupportResistance, ExpiryCache, TempOptionChain  # TempOptionChain add kiya


def save_full_temp_chain(df, symbol):
    """
    पूरे DataFrame को TempOptionChain टेबल में सेव करता है।
    सेव करने से पहले उस सिंबल का पुराना डेटा डिलीट कर देता है।
    """
    try:
        if df is None or df.empty:
            return

        # 1. उस सिंबल का पुराना डेटा हटाएं (ताकि टेबल बहुत भारी न हो)
        TempOptionChain.objects.filter(Symbol=symbol).delete()

        # 2. DataFrame से Model Objects बनाएं
        entries = [
            TempOptionChain(
                Time=row.get('Time'),
                Symbol=row.get('Symbol'),
                Expiry_Date=row.get('expiry'), # Note: df column matches dictionary key
                Lot_size=row.get('Lot_size'),
                Strike_Price=row.get('Strike_Price'),
                Spot_Price=row.get('Spot_Price'),
                
                # CE Data
                CE_Delta=row.get('CE_Delta'),
                CE_RANGE=row.get('CE_RANGE'),
                CE_IV=row.get('CE_IV'),
                CE_COI_percent=row.get('CE_COI_percent'),
                CE_COI=row.get('CE_COI'),
                CE_OI_percent=row.get('CE_OI_percent'),
                CE_OI=row.get('CE_OI'),
                CE_Volume_percent=row.get('CE_Volume_percent'),
                CE_Volume=row.get('CE_Volume'),
                CE_CLTP=row.get('CE_CLTP'),
                CE_LTP=row.get('CE_LTP'),
                Reversl_Ce=row.get('Reversl_Ce'),

                # PE Data
                Reversl_Pe=row.get('Reversl_Pe'),
                PE_LTP=row.get('PE_LTP'),
                PE_CLTP=row.get('PE_CLTP'),
                PE_Volume=row.get('PE_Volume'),
                PE_Volume_percent=row.get('PE_Volume_percent'),
                PE_OI=row.get('PE_OI'),
                PE_OI_percent=row.get('PE_OI_percent'),
                PE_COI=row.get('PE_COI'),
                PE_COI_percent=row.get('PE_COI_percent'),
                PE_IV=row.get('PE_IV'),
                PE_RANGE=row.get('PE_RANGE'),
                PE_Delta=row.get('PE_Delta'),
            )
            for _, row in df.iterrows()
        ]

        # 3. Bulk Create (Fast Save)
        TempOptionChain.objects.bulk_create(entries)
        # print(f"✅ Full Chain Saved for {symbol}")

    except Exception as e:
        print(f"❌ Error saving TempChain for {symbol}: {e}")

@sync_to_async
def save_temp_async_wrapper(df, symbol):
    """Async Wrapper ताकी मेन लूप ब्लॉक न हो"""
    return save_full_temp_chain(df, symbol)