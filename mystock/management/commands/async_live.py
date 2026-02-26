# async_live.py के टॉप पर
import logging
import aiohttp
import asyncio
import pandas as pd
from django.utils import timezone
from mystock.credentials1 import access_token  # सीधे क्रेडेंशियल्स से लें
from .symbol import symbols as SYMBOLS        # सिंबल लिस्ट के लिए
from asgiref.sync import sync_to_async
import numpy as np
from mystock.models import SupportResistance, ExpiryCache, InstrumentStore , TempOptionChain
import requests
from datetime import timedelta, datetime
import os
import gzip
from .symbol import symbols as ALL_SYMBOLS
from django.db import transaction
import re


logger = logging.getLogger(__name__)

@sync_to_async
def get_instrument_from_db(symbol):
    """डेटाबेस से इंस्ट्रूमेंट की जानकारी लेता है"""
    try:
        # from .models import InstrumentStore
        obj = InstrumentStore.objects.get(symbol=symbol)
        # (key, lot_size, expiry_list) रिटर्न करें
        return obj.instrument_key, obj.lot_size, obj.expiry_dates
    except Exception:
        return None, 1, []

def update_instrument_store_bulk1():
    """बिना API के सीधे से Key, Lot और Expiry निकालना"""
    print("🚀 Starting Bulk Update using API...")
    
    url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
    headers = {'User-Agent': 'Mozilla/5.0'}
    
    try:
        # फाइल लोड करें
        df_master = pd.read_csv(url, compression='gzip', storage_options=headers)
        df_master['tradingsymbol'] = df_master['tradingsymbol'].astype(str).str.strip()
        
        success_count = 0
        from mystock.models import InstrumentStore
        from .symbol import symbols as ALL_SYMBOLS

        for sym in ALL_SYMBOLS:
            try:
                # 1. लोट साइज और एक्सपायरी के लिए डेरिवेटिव्स ढूंढें
                # हम उन सभी रो को देखेंगे जो इस सिंबल से शुरू होती हैं और F&O में हैं
                deriv_rows = df_master[
                    (df_master['tradingsymbol'].str.startswith(sym)) & 
                    (df_master['instrument_type'].isin(['OPTSTK', 'FUTSTK', 'OPTIDX', 'FUTIDX']))
                ]

                if not deriv_rows.empty:
                    # सही लोट साइज लें
                    lot = int(deriv_rows.dropna(subset=['lot_size']).iloc[0]['lot_size'])
                    
                    # फाइल से ही सभी यूनिक एक्सपायरी डेट्स निकालें और सॉर्ट करें
                    all_expiries = sorted(deriv_rows['expiry'].dropna().unique().tolist())
                    
                    # 2. इन्स्ट्रुमेंट की (Key) के लिए मेन सिंबल (Underlying) ढूंढें
                    ikey_row = df_master[
                        (df_master['tradingsymbol'] == sym) & 
                        (df_master['exchange'].isin(['NSE_INDEX', 'NSE_EQ']))
                    ].iloc[0]
                    ikey = ikey_row['instrument_key']
                    
                    # DB में सेव करें
                    InstrumentStore.objects.update_or_create(
                        symbol=sym,
                        defaults={
                            'instrument_key': ikey,
                            'lot_size': lot,
                            'expiry_dates': all_expiries
                        }
                    )
                    success_count += 1
                    print(f"✅ {sym}: Key={ikey}, Lot={lot}, Expiries={len(all_expiries)}")

            except Exception as e:
                continue

        print(f"🏁 Bulk Update Finished! Total: {success_count} symbols.")

    except Exception as e:
        print(f"🔥 Error: {e}")

def update_instrument_store_bulk():
    """
    Ultra-Fast Vectorized Instrument Updater
    - Single-pass filtering
    - GroupBy aggregation
    - Dynamic Regex for accurate symbol matching
    - Bulk DB update
    """

    print("🚀 Starting Ultra-Fast Bulk Update...")

    url = "https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz"
    headers = {'User-Agent': 'Mozilla/5.0'}

    try:
        # ========= LOAD MASTER =========
        df = pd.read_csv(url, compression='gzip', storage_options=headers)

        df['tradingsymbol'] = df['tradingsymbol'].astype(str).str.strip()
        df['name'] = df['name'].astype(str).str.strip()

        symbols_set = set(ALL_SYMBOLS)

        # ========= DERIVATIVE FILTER =========
        deriv_df = df[
            (df['instrument_type'].isin(['OPTSTK', 'FUTSTK', 'OPTIDX', 'FUTIDX']))
        ].copy()

        # 🔥 NEW LOGIC: Dynamic Regex Based on ALL_SYMBOLS
        # 1. सिंबल्स को लंबाई के हिसाब से घटते क्रम में सॉर्ट करें (ताकि 'BAJAJFINSV' पहले मैच हो, 'BAJAJ' बाद में)
        # 2. re.escape का इस्तेमाल करें ताकि 'M&M' का '&' सही से हैंडल हो सके
        sorted_symbols = sorted([re.escape(sym) for sym in symbols_set], key=len, reverse=True)
        pattern = r'^(' + '|'.join(sorted_symbols) + r')'
        
        # 0th index का ग्रुप निकालें
        deriv_df['base_symbol'] = deriv_df['tradingsymbol'].str.extract(pattern)[0]

        # जो बेस सिंबल लिस्ट में हैं, सिर्फ उन्हें रखें
        deriv_df = deriv_df[deriv_df['base_symbol'].notna()]

        # ========= GROUPBY (VECTOR AGGREGATION) =========
        grouped = deriv_df.groupby('base_symbol').agg({
            'lot_size': 'first',
            'expiry': lambda x: sorted(x.dropna().unique().tolist())
        }).reset_index()

        grouped.rename(columns={'base_symbol': 'symbol'}, inplace=True)

        # ========= UNDERLYING KEYS (ONE TIME FILTER) =========
        underlying_df = df[
            (df['tradingsymbol'].isin(symbols_set)) &
            (df['exchange'].isin(['NSE_INDEX', 'NSE_EQ']))
        ][['tradingsymbol', 'instrument_key']]

        underlying_df.rename(columns={'tradingsymbol': 'symbol'}, inplace=True)

        # ========= MERGE =========
        final_df = grouped.merge(underlying_df, on='symbol', how='inner')

        if final_df.empty:
            print("❌ No matching underlying instruments found.")
            return

        # ========= BULK DATABASE UPDATE =========
        existing_objs = {
            obj.symbol: obj
            for obj in InstrumentStore.objects.filter(symbol__in=final_df['symbol'])
        }

        to_create = []
        to_update = []
        now = timezone.now()

        for _, row in final_df.iterrows():
            sym = row['symbol']

            if sym in existing_objs:
                obj = existing_objs[sym]
                obj.instrument_key = row['instrument_key']
                obj.lot_size = int(row['lot_size'])
                obj.expiry_dates = row['expiry']
                obj.last_updated = now
                
                # अगर आपके मॉडल में 'updated_at' फील्ड है, तभी इसे लिखें
                # obj.last_updated  = now 
                
                to_update.append(obj)
            else:
                to_create.append(
                    InstrumentStore(
                        symbol=sym,
                        instrument_key=row['instrument_key'],
                        lot_size=int(row['lot_size']),
                        expiry_dates=row['expiry']
                    )
                )

        with transaction.atomic():
            if to_create:
                InstrumentStore.objects.bulk_create(to_create, batch_size=100)

            if to_update:
                # 🔥 FIX: अगर मॉडल में updated_at है, तो उसे इस लिस्ट में जोड़ें
                # update_fields = ['instrument_key', 'lot_size', 'expiry_dates']
                update_fields = ['instrument_key', 'lot_size', 'expiry_dates', 'last_updated']
                
                InstrumentStore.objects.bulk_update(
                    to_update,
                    update_fields,
                    batch_size=100
                )

        print(f"🏁 Finished! Created: {len(to_create)}, Updated: {len(to_update)}")
        print("Total symbols matched:", len(final_df))

    except Exception as e:
        print(f"🔥 Error: {e}")

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

    # 1. Basic Checksawait get_instrument_from_db(other_symbols[0])
    s_key, lot_size, s_expiries = await get_instrument_from_db(symbol)
    key = s_key
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
        # s_key, lot_size, s_expiries = get_instrument_from_db(symbol)
        s_key, lot_size, s_expiries = await get_instrument_from_db(symbol)
        # _, lot_size = get_Name_Lot_size_Fast(symbol)

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

  # TempOptionChain add kiya

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

from mystock.models import LiveSRData  # फाइल के टॉप पर होना चाहिए

def calculate_final_sr(oi_strike, oi_status, vol_strike, vol_status, option_type):
    """
    यह फंक्शन आपके नियमों के अनुसार फाइनल Support या Resistance और उसका Status निकाल कर देगा।
    """
    
    if not oi_strike or not vol_strike:
        return None, None

    # ==========================================
    # नियम 1: अगर OI और Volume दोनों एक ही स्ट्राइक पर हैं
    # ==========================================
    if oi_strike == vol_strike:
        final_strike = oi_strike
        
        if option_type == "CE":
            # --- RESISTANCE (CE) का नियम ---
            # अगर एक भी WTB है, तो ओवरऑल WTB
            if oi_status == "WTB" or vol_status == "WTB":
                final_status = "BOTH WTB"
            # अगर दोनों WTT हैं, तभी ओवरऑल WTT
            elif oi_status == "WTT" and vol_status == "WTT":
                final_status = "BOTH WTT"
            else:
                final_status = "BOTH STRONG"
                
        elif option_type == "PE":
            # --- SUPPORT (PE) का नियम ---
            # अगर एक भी WTT है, तो ओवरऑल WTT
            if oi_status == "WTT" or vol_status == "WTT":
                final_status = "BOTH WTT"
            # अगर दोनों WTB हैं, तभी ओवरऑल WTB
            elif oi_status == "WTB" and vol_status == "WTB":
                final_status = "BOTH WTB"
            else:
                final_status = "BOTH STRONG"

    # ==========================================
    # नियम 2: अगर OI और Volume अलग-अलग स्ट्राइक पर हैं
    # ==========================================
    else:
        if option_type == "CE":
            # Resistance (CE): दोनों में जो छोटी स्ट्राइक है, उसका माना जाएगा
            if oi_strike < vol_strike:
                final_strike = oi_strike
                final_status = "OI" + oi_status
            else:
                final_strike = vol_strike
                final_status = "Vol" + vol_status
                
        elif option_type == "PE":
            # Support (PE): दोनों में जो बड़ी स्ट्राइक (स्पॉट के ज्यादा पास) है, उसका माना जाएगा
            if oi_strike > vol_strike:
                final_strike = oi_strike
                final_status = "OI" + oi_status
            else:
                final_strike = vol_strike
                final_status = "Vol" + vol_status

    return final_strike, final_status

def determine_status(strike1, strike2, pct2):
    if pct2 < 75:
        return "STRONG"
    elif strike2 > strike1:
        return "WTT"
    else:
        return "WTB"

def save_live_sr_data_new_table(df, symbol):
    """नया प्रोग्राम जो डुप्लीकेट डेटा को रोककर नई टेबल में सेव करेगा"""
    
    # 🛑 सेफ्टी लॉक: सिर्फ NIFTY
    if symbol != "NIFTY":
        return False

    try:
        if df is None or df.empty:
            return False

        time_val = df["Time"].iloc[0]
        spot_val = float(df["Spot_Price"].iloc[0])
        expiry_val = df["expiry"].iloc[0]

        data_dict = {}

        # CE और PE दोनों के लिए लूप
        for side in ["CE", "PE"]:
            side_lower = side.lower()

            # --- 1. OI LOGIC ---
            oi_col = f"{side}_OI_percent"
            sorted_oi = df.sort_values(oi_col, ascending=False).reset_index(drop=True)
            
            if len(sorted_oi) >= 2:
                s1_strike = float(sorted_oi.iloc[0]["Strike_Price"])
                s2_strike = float(sorted_oi.iloc[1]["Strike_Price"])
                s2_pct = sorted_oi.iloc[1][oi_col]

                data_dict[f"{side_lower}_high_oi_strike"] = s1_strike
                data_dict[f"{side_lower}_2nd_high_oi_strike"] = s2_strike
                data_dict[f"{side_lower}_oi_status"] = determine_status(s1_strike, s2_strike, s2_pct)

            # --- 2. VOLUME LOGIC ---
            vol_col = f"{side}_Volume_percent"
            sorted_vol = df.sort_values(vol_col, ascending=False).reset_index(drop=True)
            
            if len(sorted_vol) >= 2:
                v1_strike = float(sorted_vol.iloc[0]["Strike_Price"])
                v2_strike = float(sorted_vol.iloc[1]["Strike_Price"])
                v2_pct = sorted_vol.iloc[1][vol_col]

                data_dict[f"{side_lower}_high_vol_strike"] = v1_strike
                data_dict[f"{side_lower}_2nd_high_vol_strike"] = v2_strike
                data_dict[f"{side_lower}_vol_status"] = determine_status(v1_strike, v2_strike, v2_pct)

        # ==========================================
        # 🛡️ डुप्लीकेट चेक लॉजिक (पिछला रिकॉर्ड चेक करें)
        # ==========================================
        last_record = LiveSRData.objects.filter(Symbol=symbol).order_by('-Time').first()

        if last_record:
            # चेक करें कि क्या सभी लेवल्स और स्टेटस पुराने वाले जैसे ही हैं?
            is_same = (
                last_record.ce_high_oi_strike == data_dict.get("ce_high_oi_strike") and
                last_record.ce_oi_status == data_dict.get("ce_oi_status") and
                last_record.ce_2nd_high_oi_strike == data_dict.get("ce_2nd_high_oi_strike") and
                
                last_record.ce_high_vol_strike == data_dict.get("ce_high_vol_strike") and
                last_record.ce_vol_status == data_dict.get("ce_vol_status") and
                last_record.ce_2nd_high_vol_strike == data_dict.get("ce_2nd_high_vol_strike") and

                last_record.pe_high_oi_strike == data_dict.get("pe_high_oi_strike") and
                last_record.pe_oi_status == data_dict.get("pe_oi_status") and
                last_record.pe_2nd_high_oi_strike == data_dict.get("pe_2nd_high_oi_strike") and
                
                last_record.pe_high_vol_strike == data_dict.get("pe_high_vol_strike") and
                last_record.pe_vol_status == data_dict.get("pe_vol_status") and
                last_record.pe_2nd_high_vol_strike == data_dict.get("pe_2nd_high_vol_strike")
            )

            # अगर सब कुछ सेम है तो सेव ना करें और वापस लौट जाएँ
            if is_same:
                return False
            # अगर डेटा अलग है, तो फाइनल स्ट्राइक और स्टेटस निकालें
        ce_final_strike, ce_final_status = calculate_final_sr(
            data_dict.get("ce_high_oi_strike"),
            data_dict.get("ce_oi_status"),
            data_dict.get("ce_high_vol_strike"),
            data_dict.get("ce_vol_status"),
            "CE",)
        
        pe_final_strike, pe_final_status = calculate_final_sr(
            data_dict.get("pe_high_oi_strike"),
            data_dict.get("pe_oi_status"),
            data_dict.get("pe_high_vol_strike"),
            data_dict.get("pe_vol_status"),
            "PE",)
        # ==========================================
        # 💾 अगर डेटा अलग है (या पहला रिकॉर्ड है), तो सेव करें
        # ==========================================
        LiveSRData.objects.create(
            Time=time_val,
            Symbol=symbol,
            Spot_Price=spot_val,
            Expiry_Date=str(expiry_val) if expiry_val else None,
            
            # CE Data
            ce_high_oi_strike=data_dict.get("ce_high_oi_strike"),
            ce_oi_status=data_dict.get("ce_oi_status"),
            ce_2nd_high_oi_strike=data_dict.get("ce_2nd_high_oi_strike"),
            
            ce_high_vol_strike=data_dict.get("ce_high_vol_strike"),
            ce_vol_status=data_dict.get("ce_vol_status"),
            ce_2nd_high_vol_strike=data_dict.get("ce_2nd_high_vol_strike"),
            resistance_strike=ce_final_strike,
            resistance_status=ce_final_status,

            # PE Data
            pe_high_oi_strike=data_dict.get("pe_high_oi_strike"),
            pe_oi_status=data_dict.get("pe_oi_status"),
            pe_2nd_high_oi_strike=data_dict.get("pe_2nd_high_oi_strike"),
            
            pe_high_vol_strike=data_dict.get("pe_high_vol_strike"),
            pe_vol_status=data_dict.get("pe_vol_status"),
            pe_2nd_high_vol_strike=data_dict.get("pe_2nd_high_vol_strike"),
            supprt_strike=pe_final_strike,
            supprt_status=pe_final_status,
               
        )
        return True

    except Exception as e:
        print(f"Error saving to new LiveSRData table for {symbol}: {e}")
        return False

# Wrapper
@sync_to_async
def save_live_sr_data_async_wrapper(df, symbol):
    return save_live_sr_data_new_table(df, symbol)



    