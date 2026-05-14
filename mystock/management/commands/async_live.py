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
from mystock.models import SupportResistance, ExpiryCache, InstrumentStore , TempOptionChain, LiveSRData, PaperTrade, OptionChain, BotSettings
import requests
from datetime import timedelta, datetime
import os
import gzip
from .symbol import symbols as ALL_SYMBOLS
from django.db import transaction
import re
from mystock.trade_logic import get_master_levels
import time
from django.utils.timezone import localtime

cleanup_done_today = None


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
        
        # print(f"📊 {symbol} - Spot: {spot_price}, Lot: {lot_size}, Expiry: {expiry_Date}, Data Points: {len(data_list)}")

        lot_size = lot_size  if lot_size and lot_size > 0 else 1
  
        rows = []
        for entry in data_list:
            ce_obj = entry.get("call_options") or {}
            pe_obj = entry.get("put_options") or {}
            ce_md = ce_obj.get("market_data") or {}
            pe_md = pe_obj.get("market_data") or {}
            ce_g = ce_obj.get("option_greeks") or {}
            pe_g = pe_obj.get("option_greeks") or {}
            
            # 1. लूप शुरू होने से पहले एक ही बार टाइम कैलकुलेट कर लें
            current_time = timezone.now()

            rows.append({
                "Time": current_time,
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

        # OI, Volume, COI के लिए परसेंटेज कॉलम बनाएं (Vectorized)
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






# ────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────
WTT    = "WTT"
WTB    = "WTB"
STRONG = "STRONG"

from datetime import datetime, timedelta, timezone as dt_timezone
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)

IST = dt_timezone(timedelta(hours=5, minutes=30))

def _today_ist():
    return datetime.now(IST).date()

cleanup_done_today = None  # Global variable for cleanup tracking

# ────────────────────────────────────────────────────────────────
# Helper: Float formatter
# ────────────────────────────────────────────────────────────────
def _fmt(v):
    if v is None:
        return "—"
    return str(int(v)) if v == int(v) else str(v)

# ────────────────────────────────────────────────────────────────
# Helper: Label से Strike Price निकालना
# ────────────────────────────────────────────────────────────────
def _extract_strike(label: str):
    """String में से पहला number निकालता है (regex से)"""
    import re
    if not label:
        return None
    m = re.search(r'[\d]+(?:[.][\d]+)?', label)
    if m:
        try:
            return float(m.group())
        except ValueError:
            return None
    return None

# ────────────────────────────────────────────────────────────────
# Helper: WTT / WTB / Strong  (75% Rule — per OI/Volume column)
# ────────────────────────────────────────────────────────────────
def _get_status(s1_percent: float, s2_percent: float,
                s1_strike:  float, s2_strike:  float) -> str:
    if s2_percent < 75:
        return "Strong"
    elif s2_strike < s1_strike:
        return "WTB"
    else:
        return "WTT"


# ════════════════════════════════════════════════════════════════
# ResistanceCalculator  (CE — छोटी strike primary)
# ════════════════════════════════════════════════════════════════
class ResistanceCalculator:
    def __init__(self): self.reset()

    def reset(self):
        self._prev_label   = None
        self._shifting     = False
        self._shift_strike = None
        self._shift_wt     = None
        self._in_shifted   = False
        self._shifted_wt   = None
        self._prev_p2nd    = None

    def calculate(self, row_dict):
        label, source = self._compute(row_dict)
        self._prev_label = label
        return label, source

    def _src(self, ptype, pS):
        return f"Resistance ({ptype}){_fmt(pS)}"

    def _do_shift(self, shift_to, wt, src):
        self._shifting     = True
        self._in_shifted   = False
        self._shift_strike = shift_to
        self._shift_wt     = wt
        self._prev_p2nd    = None
        return f"Resistance {wt} {_fmt(shift_to)}", src

    def _reset(self):
        self._shifting     = False
        self._shift_strike = None
        self._shift_wt     = None
        self._in_shifted   = False
        self._shifted_wt   = None
        self._prev_p2nd    = None

    def _compute(self, r):
        vs    = r.get("ce_high_vol_strike")
        os_   = r.get("ce_high_oi_strike")
        vStat = (r.get("ce_vol_status") or "").upper()
        oStat = (r.get("ce_oi_status")  or "").upper()

        # ── CASE 1: Same Strike ──────────────────────────────
        if vs is not None and os_ is not None and vs == os_:
            pS     = vs
            src    = self._src("Both", pS)

            if vStat == WTT and oStat == WTT:
                target = r.get("ce_2nd_high_vol_strike") or r.get("ce_2nd_high_oi_strike")
                # FIX: IN_SHIFTED उसी primary strike (pS) पर → Shifted state preserve करो
                if target and self._in_shifted and self._shift_strike == pS:
                    self._shifted_wt = WTT
                    self._prev_p2nd  = target
                    return f"Resistance Shifted WTT {_fmt(target)}", src
                self._reset()
                if target:
                    return self._do_shift(target, WTT, src)
                return f"Resistance WTT {_fmt(pS)}", src

            if vStat == WTB or oStat == WTB:
                if vStat == WTB and oStat == WTB:
                    target = r.get("ce_2nd_high_vol_strike") or r.get("ce_2nd_high_oi_strike")
                elif vStat == WTB:
                    target = r.get("ce_2nd_high_vol_strike")
                else:
                    target = r.get("ce_2nd_high_oi_strike")
                # FIX: IN_SHIFTED उसी primary strike (pS) पर → Shifted state preserve करो
                if target and self._in_shifted and self._shift_strike == pS:
                    self._shifted_wt = WTB
                    self._prev_p2nd  = target
                    return f"Resistance Shifted WTB {_fmt(target)}", src
                self._reset()
                if target:
                    return self._do_shift(target, WTB, src)
                return f"Resistance WTB {_fmt(pS)}", src

            self._reset()
            return "Resistance Both strong", src

        # ── CASE 2: Different Strikes — CE में छोटी strike primary
        if vs is not None and os_ is not None:
            if vs < os_:
                pS, pStat, p2nd, pType = vs,  vStat, r.get("ce_2nd_high_vol_strike"), "Vol"
            else:
                pS, pStat, p2nd, pType = os_, oStat, r.get("ce_2nd_high_oi_strike"),  "OI"
        elif vs is not None:
            pS, pStat, p2nd, pType = vs,  vStat, r.get("ce_2nd_high_vol_strike"), "Vol"
        else:
            pS, pStat, p2nd, pType = os_, oStat, r.get("ce_2nd_high_oi_strike"),  "OI"

        src = self._src(pType, pS)

        # ── IN_SHIFTED ───────────────────────────────────────
        if self._in_shifted:
            if pS == self._shift_strike:
                if pStat in (WTT, WTB):
                    if p2nd is not None and p2nd != self._prev_p2nd:
                        self._in_shifted = False
                        return self._do_shift(p2nd, pStat, src)
                    self._shifted_wt = pStat
                    self._prev_p2nd  = p2nd
                    # FIX: p2nd if p2nd else pS
                    return f"Resistance Shifted {pStat} {_fmt(p2nd if p2nd else pS)}", src
                else:
                    self._reset()
                    return f"Resistance strong {_fmt(pS)}", src
            else:
                self._in_shifted = False
                if pStat in (WTT, WTB):
                    if p2nd:
                        return self._do_shift(p2nd, pStat, src)
                    self._reset()
                    return f"Resistance {pStat} {_fmt(pS)}", src # FIX: return normal WTT/WTB
                self._reset()
                return f"Resistance strong {_fmt(pS)}", src

        # ── SHIFTING ─────────────────────────────────────────
        if self._shifting:
            if pS == self._shift_strike:
                if pStat in (WTT, WTB):
                    self._shifting   = False
                    self._in_shifted = True
                    self._shifted_wt = pStat
                    self._prev_p2nd  = p2nd
                    # FIX: p2nd if p2nd else pS
                    return f"Resistance Shifted {pStat} {_fmt(p2nd if p2nd else pS)}", src
                else:
                    self._reset()
                    return f"Resistance strong {_fmt(pS)}", src # FIX: removed 'Shifted strong'
            else:
                if pStat in (WTT, WTB):
                    if p2nd:
                        return self._do_shift(p2nd, pStat, src)
                    self._reset()
                    return f"Resistance {pStat} {_fmt(pS)}", src # FIX: return normal WTT/WTB
                self._reset()
                return f"Resistance strong {_fmt(pS)}", src # FIX: removed 'Shifted strong'

        # ── NORMAL ───────────────────────────────────────────
        if pStat == WTT:
            if p2nd:
                return self._do_shift(p2nd, WTT, src)
            return f"Resistance WTT {_fmt(pS)}", src

        if pStat == WTB:
            if p2nd:
                return self._do_shift(p2nd, WTB, src)
            return f"Resistance WTB {_fmt(pS)}", src

        self._reset()
        return f"Resistance strong {_fmt(pS)}", src


# ════════════════════════════════════════════════════════════════
# SupportCalculator  (PE — बड़ी strike primary)
# ════════════════════════════════════════════════════════════════
class SupportCalculator:
    def __init__(self): self.reset()

    def reset(self):
        self._prev_label   = None
        self._shifting     = False
        self._shift_strike = None
        self._shift_wt     = None
        self._in_shifted   = False
        self._shifted_wt   = None
        self._prev_p2nd    = None

    def calculate(self, row_dict):
        label, source = self._compute(row_dict)
        self._prev_label = label
        return label, source

    def _src(self, ptype, pS):
        return f"Support ({ptype}){_fmt(pS)}"

    def _do_shift(self, shift_to, wt, src):
        self._shifting     = True
        self._in_shifted   = False
        self._shift_strike = shift_to
        self._shift_wt     = wt
        self._prev_p2nd    = None
        return f"Support {wt} {_fmt(shift_to)}", src

    def _reset(self):
        self._shifting     = False
        self._shift_strike = None
        self._shift_wt     = None
        self._in_shifted   = False
        self._shifted_wt   = None
        self._prev_p2nd    = None

    def _compute(self, r):
        vs    = r.get("pe_high_vol_strike")
        os_   = r.get("pe_high_oi_strike")
        vStat = (r.get("pe_vol_status") or "").upper()
        oStat = (r.get("pe_oi_status")  or "").upper()

        # ── CASE 1: Same Strike ──────────────────────────────
        if vs is not None and os_ is not None and vs == os_:
            pS     = vs
            src    = self._src("Both", pS)

            if vStat == WTB and oStat == WTB:
                target = r.get("pe_2nd_high_vol_strike") or r.get("pe_2nd_high_oi_strike")
                # FIX: IN_SHIFTED उसी primary strike (pS) पर → Shifted state preserve करो
                if target and self._in_shifted and self._shift_strike == pS:
                    self._shifted_wt = WTB
                    self._prev_p2nd  = target
                    return f"Support Shifted WTB {_fmt(target)}", src
                self._reset()
                if target:
                    return self._do_shift(target, WTB, src)
                return f"Support WTB {_fmt(pS)}", src

            if vStat == WTT or oStat == WTT:
                if vStat == WTT and oStat == WTT:
                    target = r.get("pe_2nd_high_vol_strike") or r.get("pe_2nd_high_oi_strike")
                elif vStat == WTT:
                    target = r.get("pe_2nd_high_vol_strike")
                else:
                    target = r.get("pe_2nd_high_oi_strike")
                # FIX: IN_SHIFTED उसी primary strike (pS) पर → Shifted state preserve करो
                if target and self._in_shifted and self._shift_strike == pS:
                    self._shifted_wt = WTT
                    self._prev_p2nd  = target
                    return f"Support Shifted WTT {_fmt(target)}", src
                self._reset()
                if target:
                    return self._do_shift(target, WTT, src)
                return f"Support WTT {_fmt(pS)}", src

            self._reset()
            return "Support Both strong", src

        # ── CASE 2: Different Strikes — PE में बड़ी strike primary
        if vs is not None and os_ is not None:
            if vs > os_:
                pS, pStat, p2nd, pType = vs,  vStat, r.get("pe_2nd_high_vol_strike"), "Vol"
            else:
                pS, pStat, p2nd, pType = os_, oStat, r.get("pe_2nd_high_oi_strike"),  "OI"
        elif vs is not None:
            pS, pStat, p2nd, pType = vs,  vStat, r.get("pe_2nd_high_vol_strike"), "Vol"
        else:
            pS, pStat, p2nd, pType = os_, oStat, r.get("pe_2nd_high_oi_strike"),  "OI"

        src = self._src(pType, pS)

        # ── IN_SHIFTED ───────────────────────────────────────
        if self._in_shifted:
            if pS == self._shift_strike:
                if pStat in (WTT, WTB):
                    if p2nd is not None and p2nd != self._prev_p2nd:
                        self._in_shifted = False
                        return self._do_shift(p2nd, pStat, src)
                    self._shifted_wt = pStat
                    self._prev_p2nd  = p2nd
                    # FIX: Support string with proper target strike
                    return f"Support Shifted {pStat} {_fmt(p2nd if p2nd else pS)}", src
                else:
                    self._reset()
                    return f"Support strong {_fmt(pS)}", src
            else:
                self._in_shifted = False
                if pStat in (WTT, WTB):
                    if p2nd:
                        return self._do_shift(p2nd, pStat, src)
                    self._reset()
                    return f"Support {pStat} {_fmt(pS)}", src
                self._reset()
                return f"Support strong {_fmt(pS)}", src

        # ── SHIFTING ─────────────────────────────────────────
        if self._shifting:
            if pS == self._shift_strike:
                if pStat in (WTT, WTB):
                    self._shifting   = False
                    self._in_shifted = True
                    self._shifted_wt = pStat
                    self._prev_p2nd  = p2nd
                    # FIX: Support string with proper target strike
                    return f"Support Shifted {pStat} {_fmt(p2nd if p2nd else pS)}", src
                else:
                    self._reset()
                    return f"Support strong {_fmt(pS)}", src # FIX: removed 'Shifted strong'
            else:
                if pStat in (WTT, WTB):
                    if p2nd:
                        return self._do_shift(p2nd, pStat, src)
                    self._reset()
                    return f"Support {pStat} {_fmt(pS)}", src
                self._reset()
                return f"Support strong {_fmt(pS)}", src # FIX: removed 'Shifted strong'

        # ── NORMAL ───────────────────────────────────────────
        if pStat == WTT:
            if p2nd:
                return self._do_shift(p2nd, WTT, src)
            return f"Support WTT {_fmt(pS)}", src
            
        if pStat == WTB:
            if p2nd:
                return self._do_shift(p2nd, WTB, src)
            return f"Support WTB {_fmt(pS)}", src

        self._reset()
        return f"Support strong {_fmt(pS)}", src


# ════════════════════════════════════════════════════════════════
# Per-Symbol Calculator Cache
# (दिन बदलने पर auto reset — नया दिन = fresh calculators)
# ════════════════════════════════════════════════════════════════
_CALC_CACHE: dict = {}   # { symbol: (date, ResistanceCalc, SupportCalc) }

def _get_calculators(symbol: str):
    today = _today_ist()
    if symbol in _CALC_CACHE:
        cached_date, r_calc, s_calc = _CALC_CACHE[symbol]
        if cached_date == today:
            return r_calc, s_calc
    # नया दिन या पहली बार — fresh calculators
    r_calc = ResistanceCalculator()
    s_calc = SupportCalculator()
    _CALC_CACHE[symbol] = (today, r_calc, s_calc)
    return r_calc, s_calc


# ════════════════════════════════════════════════════════════════
# Main Sync Save Function
# ════════════════════════════════════════════════════════════════
def save_live_sr_data(df, symbol: str) -> bool:
    """
    calculate_data_async_optimized() का df लेता है,
    CE + PE Top-2 OI/Volume निकालता है,
    ResistanceCalculator/SupportCalculator से
    resistance_strike/status और supprt_strike/status भरता है,
    LiveSRData में save करता है।
    """
    global cleanup_done_today  
    try:
        if df is None or df.empty:
            print(f"⚠️ [{symbol}] DataFrame खाली — LiveSRData skip")
            return False

        # ── Basic Info ──────────────────────────────────────
        spot   = float(df["Spot_Price"].iloc[0])
        expiry = str(df["expiry"].iloc[0]) if df["expiry"].iloc[0] else None
        now    = timezone.localtime()

        # ════════════════════════════════════════════════════
        # CE  (CALL — Resistance)
        # ════════════════════════════════════════════════════

        ce_oi_top2 = df.nlargest(2, "CE_OI")
        if len(ce_oi_top2) < 2:
            print(f"⚠️ [{symbol}] CE OI rows < 2, skip")
            return False
        ce_oi_s1, ce_oi_s2 = ce_oi_top2.iloc[0], ce_oi_top2.iloc[1]

        ce_oi_status = _get_status(
            float(ce_oi_s1["CE_OI_percent"]), float(ce_oi_s2["CE_OI_percent"]),
            float(ce_oi_s1["Strike_Price"]),  float(ce_oi_s2["Strike_Price"]),
        )

        ce_vol_top2 = df.nlargest(2, "CE_Volume")
        if len(ce_vol_top2) < 2:
            print(f"⚠️ [{symbol}] CE Volume rows < 2, skip")
            return False
        ce_vol_s1, ce_vol_s2 = ce_vol_top2.iloc[0], ce_vol_top2.iloc[1]

        ce_vol_status = _get_status(
            float(ce_vol_s1["CE_Volume_percent"]), float(ce_vol_s2["CE_Volume_percent"]),
            float(ce_vol_s1["Strike_Price"]),      float(ce_vol_s2["Strike_Price"]),
        )

        # ════════════════════════════════════════════════════
        # PE  (PUT — Support)
        # ════════════════════════════════════════════════════

        pe_oi_top2 = df.nlargest(2, "PE_OI")
        if len(pe_oi_top2) < 2:
            print(f"⚠️ [{symbol}] PE OI rows < 2, skip")
            return False
        pe_oi_s1, pe_oi_s2 = pe_oi_top2.iloc[0], pe_oi_top2.iloc[1]

        pe_oi_status = _get_status(
            float(pe_oi_s1["PE_OI_percent"]), float(pe_oi_s2["PE_OI_percent"]),
            float(pe_oi_s1["Strike_Price"]),  float(pe_oi_s2["Strike_Price"]),
        )

        pe_vol_top2 = df.nlargest(2, "PE_Volume")
        if len(pe_vol_top2) < 2:
            print(f"⚠️ [{symbol}] PE Volume rows < 2, skip")
            return False
        pe_vol_s1, pe_vol_s2 = pe_vol_top2.iloc[0], pe_vol_top2.iloc[1]

        pe_vol_status = _get_status(
            float(pe_vol_s1["PE_Volume_percent"]), float(pe_vol_s2["PE_Volume_percent"]),
            float(pe_vol_s1["Strike_Price"]),      float(pe_vol_s2["Strike_Price"]),
        )

        # ════════════════════════════════════════════════════
        # Calculator के लिए row_dict
        # ════════════════════════════════════════════════════
        row_dict = {
            "ce_high_oi_strike":      float(ce_oi_s1["Strike_Price"]),
            "ce_oi_status":           ce_oi_status,
            "ce_2nd_high_oi_strike":  float(ce_oi_s2["Strike_Price"]),
            "ce_high_vol_strike":     float(ce_vol_s1["Strike_Price"]),
            "ce_vol_status":          ce_vol_status,
            "ce_2nd_high_vol_strike": float(ce_vol_s2["Strike_Price"]),
            "pe_high_oi_strike":      float(pe_oi_s1["Strike_Price"]),
            "pe_oi_status":           pe_oi_status,
            "pe_2nd_high_oi_strike":  float(pe_oi_s2["Strike_Price"]),
            "pe_high_vol_strike":     float(pe_vol_s1["Strike_Price"]),
            "pe_vol_status":          pe_vol_status,
            "pe_2nd_high_vol_strike": float(pe_vol_s2["Strike_Price"]),
        }

        # ════════════════════════════════════════════════════
        # State Machine चलाओ
        # ════════════════════════════════════════════════════
        res_calc, sup_calc = _get_calculators(symbol)

        res_label, _res_src = res_calc.calculate(row_dict)
        sup_label, _sup_src = sup_calc.calculate(row_dict)

        # ── resistance_strike / supprt_strike ───────────────
        resistance_strike = _extract_strike(_res_src)
        supprt_strike     = _extract_strike(_sup_src)

        # ── resistance_status / supprt_status ───────────────
        import re as _re

        def _build_status(src: str, label: str, prefix: str) -> str:
            m = _re.search(r'([(][^)]+[)])', src)
            ptype  = m.group(1) if m else ""
            action = label[len(prefix):].strip()
            return f"{prefix} {ptype} {action}".strip()

        resistance_status = _build_status(_res_src, res_label, "Resistance")
        supprt_status     = _build_status(_sup_src, sup_label, "Support")

        # ════════════════════════════════════════════════════
        # DUPLICATE CHECK
        # ════════════════════════════════════════════════════
        last = (LiveSRData.objects
                .filter(Symbol=symbol)
                .order_by("-Time")
                .only("resistance_status", "supprt_status")
                .first())

        if (last is not None
                and last.resistance_status == resistance_status
                and last.supprt_status     == supprt_status):
            # print(f"⏭️  [{symbol}] Duplicate skip | R: {resistance_status} | S: {supprt_status}")
            return True


        LiveSRData.objects.create(
            Time        = now,
            Symbol      = symbol,
            Expiry_Date = expiry,
            Spot_Price  = spot,

            # CE OI
            ce_high_oi_strike     = float(ce_oi_s1["Strike_Price"]),
            ce_oi_status          = ce_oi_status,
            ce_2nd_high_oi_strike = float(ce_oi_s2["Strike_Price"]),

            # CE Volume
            ce_high_vol_strike     = float(ce_vol_s1["Strike_Price"]),
            ce_vol_status          = ce_vol_status,
            ce_2nd_high_vol_strike = float(ce_vol_s2["Strike_Price"]),

            # CE Combined
            resistance_strike = resistance_strike,
            resistance_status = resistance_status,

            # PE OI
            pe_high_oi_strike     = float(pe_oi_s1["Strike_Price"]),
            pe_oi_status          = pe_oi_status,
            pe_2nd_high_oi_strike = float(pe_oi_s2["Strike_Price"]),

            # PE Volume
            pe_high_vol_strike     = float(pe_vol_s1["Strike_Price"]),
            pe_vol_status          = pe_vol_status,
            pe_2nd_high_vol_strike = float(pe_vol_s2["Strike_Price"]),

            # PE Combined
            supprt_strike = supprt_strike,
            supprt_status = supprt_status,
        )

        print(f"✅ [{symbol}] R: {res_label} | S: {sup_label}")
        return True

    except Exception as e:
        print(f"❌ LiveSRData Save Error [{symbol}]: {e}")
        return False


# ────────────────────────────────────────────────────────────────
# Async Wrapper
# ────────────────────────────────────────────────────────────────
from asgiref.sync import sync_to_async

@sync_to_async
def save_live_sr_async(df, symbol: str) -> bool:
    """Async Wrapper — Main async loop में call करें"""
    return save_live_sr_data(df, symbol)


# ================================================================
# INTEGRATION (async_live.py में):
#
#   df = await calculate_data_async_optimized(session, symbol, expiry_Date)
#   if df is not None:
#       await save_live_sr_async(df, symbol)       # ← LiveSRData
#       await save_sr_async_wrapper(df, symbol)    # SupportResistance
#       await save_temp_async_wrapper(df, symbol)  # TempOptionChain
# ================================================================



def run_live_paper_trading1(df, symbol="NIFTY"):
    today = timezone.now().date()
    
    # 1. स्पॉट प्राइस सीधे API के ताज़ा डेटा (df) से निकालें
    spot = float(df["Spot_Price"].iloc[0])
    current_time = df["Time"].iloc[0]

    # 2. पुरानी ओपन ट्रेड डेटाबेस से लाएं
    open_trade = PaperTrade.objects.filter(symbol=symbol, trade_date=today, result="OPEN").first()

    # ==========================================
    # ── 1. LEVEL CALCULATION (R & S) ──
    # ==========================================
    sr = LiveSRData.objects.filter(Symbol__iexact=symbol, Time__date=today).order_by('-Time').first()
    if not sr or not sr.resistance_strike or not sr.supprt_strike:
        return "No SR Data"

    # Step साइज़ निकालना (सिंबल के हिसाब से डायनामिक)
    step = 100 if "BANKNIFTY" in symbol or "SENSEX" in symbol else 50 

    # --- Effective Resistance (R) ---
    res_status = str(sr.resistance_status).upper() if sr.resistance_status else ""
    res_base = float(sr.resistance_strike)
    m_res = re.search(r'(?:WTB|WTT)\s+(\d+)', res_status)
    res_target = float(m_res.group(1)) if m_res else res_base

    if "SHIFTED WTT" in res_status: eff_res = res_base
    elif "SHIFTED WTB" in res_status: eff_res = res_base + step
    elif "WTT" in res_status: eff_res = res_target + step
    elif "WTB" in res_status: eff_res = res_target + step
    elif "STRONG" in res_status: eff_res = res_base + step
    else: eff_res = res_base + step

    # --- Effective Support (S) ---
    sup_status = str(sr.supprt_status).upper() if sr.supprt_status else ""
    sup_base = float(sr.supprt_strike)
    m_sup = re.search(r'(?:WTB|WTT)\s+(\d+)', sup_status)
    sup_target = float(m_sup.group(1)) if m_sup else sup_base

    if "SHIFTED WTT" in sup_status: eff_sup = sup_base
    elif "SHIFTED WTB" in sup_status: eff_sup = sup_base - step
    elif "WTT" in sup_status: eff_sup = sup_target - step
    elif "WTB" in sup_status: eff_sup = sup_target - step
    elif "STRONG" in sup_status: eff_sup = sup_base - step
    else: eff_sup = sup_base - step

    # --- Next SL Strikes (Target और Stoploss के लिए) ---
    eff_res_sl = eff_res + step  
    eff_sup_sl = eff_sup - step  

    # अब इन चारों Effective Strikes की Reversal Value लाएं
    r_row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=today, Strike_Price=eff_res).order_by('-Time').first()
    s_row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=today, Strike_Price=eff_sup).order_by('-Time').first()
    r_sl_row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=today, Strike_Price=eff_res_sl).order_by('-Time').first()
    s_sl_row = OptionChain.objects.filter(Symbol__iexact=symbol, Time__date=today, Strike_Price=eff_sup_sl).order_by('-Time').first()

    r_level = float(r_row.Reversl_Ce) if r_row and r_row.Reversl_Ce else None
    s_level = float(s_row.Reversl_Pe) if s_row and s_row.Reversl_Pe else None
    r_sl_level = float(r_sl_row.Reversl_Ce) if r_sl_row and r_sl_row.Reversl_Ce else None
    s_sl_level = float(s_sl_row.Reversl_Pe) if s_sl_row and s_sl_row.Reversl_Pe else None

    # ==========================================
    # ── 2. EXIT LOGIC (Reversal Target + Buffer) ──
    # ==========================================
    if open_trade:
        entry = open_trade.entry_spot
        ttype = open_trade.trade_type

        hit_target = False
        hit_sl = False

        # 🎯 5 पॉइंट का रिवर्सल बफर 
        # (ताकि मार्केट रिवर्सल लाइन को टच करने से 5 पॉइंट पहले ही प्रॉफिट बुक कर ले)
        BUFFER = 5.0 

        if ttype == 'PUT':
            # PUT के लिए टारगेट है सपोर्ट (s_level)
            target = s_level if s_level else (entry - 50.0) 
            sl = r_sl_level if r_sl_level else (entry + 50.0)

            # अगर मार्केट सपोर्ट से 5 पॉइंट दूर (ऊपर) भी है, तो प्रॉफिट बुक कर लें
            if spot <= (target + BUFFER): 
                hit_target = True
            elif spot >= sl: 
                hit_sl = True

        elif ttype == 'CALL':
            # CALL के लिए टारगेट है रेजिस्टेंस (r_level)
            target = r_level if r_level else (entry + 50.0)
            sl = s_sl_level if s_sl_level else (entry - 50.0)

            # अगर मार्केट रेजिस्टेंस से 5 पॉइंट दूर (नीचे) भी है, तो प्रॉफिट बुक कर लें
            if spot >= (target - BUFFER): 
                hit_target = True
            elif spot <= sl: 
                hit_sl = True

        if hit_target or hit_sl:
            # 🎯 असली P&L पॉइंट कैलकुलेशन
            if ttype == 'CALL':
                actual_pnl = spot - entry  # CALL में: Exit - Entry
            else:
                actual_pnl = entry - spot  # PUT में: Entry - Exit

            open_trade.exit_spot = spot
            open_trade.exit_time = current_time
            open_trade.result = "TARGET" if hit_target else "SL"
            open_trade.pnl = round(actual_pnl, 2)
            open_trade.save()
            print(f"[{current_time}] 🟢 TRADE CLOSED | {ttype} | Result: {open_trade.result} | PNL: {open_trade.pnl}")
            return "Trade Closed"

        return "Trade is Running"


    # ==========================================
    # ── 3. AVOID REPEAT & SHIFT TO NEXT LEVEL (NEW) ──
    # ==========================================
    # 20 पॉइंट का टॉलरेंस: अगर इस लेवल के 20 पॉइंट ऊपर-नीचे कोई ट्रेड लिया है, तो उसे गिना जाएगा
    tolerance = 20.0 
    
    # --- R (Resistance) Check ---
    if r_level:
        r_already_traded = PaperTrade.objects.filter(
            symbol=symbol, trade_date=today, trade_type='PUT',
            trigger_price__gte=r_level - tolerance, trigger_price__lte=r_level + tolerance
        ).exists()

        # अगर R पर पहले ट्रेड हो चुका है, तो नया एंट्री पॉइंट R+1 (यानी r_sl_level) बना दो
        if r_already_traded:
            if r_sl_level:
                r_level = r_sl_level  # अब बॉट 1 लेवल ऊपर इंतज़ार करेगा
                # print(f"⚠️ R Level already traded! Moving PUT Entry UP to -> {r_level}")
            else:
                r_level = None  # अगर अगला लेवल नहीं मिला तो ट्रेड रद्द करें

    # --- S (Support) Check ---
    if s_level:
        s_already_traded = PaperTrade.objects.filter(
            symbol=symbol, trade_date=today, trade_type='CALL',
            trigger_price__gte=s_level - tolerance, trigger_price__lte=s_level + tolerance
        ).exists()

        # अगर S पर पहले ट्रेड हो चुका है, तो नया एंट्री पॉइंट S-1 (यानी s_sl_level) बना दो
        if s_already_traded:
            if s_sl_level:
                s_level = s_sl_level  # अब बॉट 1 लेवल नीचे इंतज़ार करेगा
                # print(f"⚠️ S Level already traded! Moving CALL Entry DOWN to -> {s_level}")
            else:
                s_level = None  # अगर अगला लेवल नहीं मिला तो ट्रेड रद्द करें


    # ==========================================
    # ── 4. ENTRY LOGIC ──
    # ==========================================
    # (यहाँ r_level और s_level अब ऑटोमैटिकली शिफ्टेड लेवल बन चुके हैं अगर पहले ट्रेड हुआ था)
    
    if r_level and spot >= r_level:
        # BUY PUT
        PaperTrade.objects.create(
            symbol=symbol, trade_type='PUT', entry_time=current_time, entry_spot=spot,
            trigger_level='R', trigger_price=r_level
        )
        print(f"[{current_time}] 🔴 NEW ENTRY: BUY PUT @ {spot} (R={r_level})")
        return "Put Trade Opened"

    elif s_level and spot <= s_level:
        # BUY CALL
        PaperTrade.objects.create(
            symbol=symbol, trade_type='CALL', entry_time=current_time, entry_spot=spot,
            trigger_level='S', trigger_price=s_level
        )
        print(f"[{current_time}] 🟢 NEW ENTRY: BUY CALL @ {spot} (S={s_level})")
        return "Call Trade Opened"

    return "No Entry Triggered"



def run_live_paper_trading2(df, symbol="NIFTY", master_levels=None):
    today = timezone.now().date()
    spot = float(df["Spot_Price"].iloc[0])
    current_time = df["Time"].iloc[0]
    

    # ==========================================
    # ── 1. LIVE ADMIN SETTINGS ──
    # ==========================================
    settings, _ = BotSettings.objects.get_or_create(id=1)
    if not settings.trading_enabled:
        return "Trading Disabled via Admin"
    
    TARGET_PTS = settings.default_target
    SL_PTS = settings.default_sl
    BUFFER = settings.reversal_buffer


    # ==========================================
    # ── 2. NEW MASTER LEVEL CALCULATION ──
    # ==========================================
    # master_levels = get_master_levels(symbol, today)
    if master_levels is None:
        master_levels = get_master_levels(symbol, today)
    
    eff_res = master_levels["R"]["strike"]
    r_level = master_levels["R"]["entry"]
    r_target_level = master_levels["R"]["target"]
    r_sl_level = master_levels["R"]["sl"]

    eff_sup = master_levels["S"]["strike"]
    s_level = master_levels["S"]["entry"]
    s_target_level = master_levels["S"]["target"]
    s_sl_level = master_levels["S"]["sl"]
    
    if not eff_res or not eff_sup:
        return "No SR Data"
    
    
    # ==========================================
    # ── 3. EXIT LOGIC (For ALL Open Trades) ──
    # ==========================================
    open_trades = PaperTrade.objects.filter(symbol=symbol, trade_date=today, result="OPEN")
    
    for open_trade in open_trades:
        entry = open_trade.entry_spot
        ttype = open_trade.trade_type
        hit_target = hit_sl = False

        

        if ttype == 'PUT':
            target = r_target_level if r_target_level else (entry - TARGET_PTS) 
            sl = r_sl_level if r_sl_level else (entry + SL_PTS)
            
            # 🚨 सेफ्टी लॉक: PUT का SL हमेशा Entry से बड़ा होना चाहिए
            if sl <= entry: 
                sl = entry + SL_PTS

            if spot <= (target + BUFFER): hit_target = True
            elif spot >= (sl + BUFFER): hit_sl = True

        elif ttype == 'CALL':
            target = s_target_level if s_target_level else (entry + TARGET_PTS)
            sl = s_sl_level if s_sl_level else (entry - SL_PTS)
            
            # 🚨 सेफ्टी लॉक: CALL का SL हमेशा Entry से छोटा होना चाहिए
            if sl >= entry: 
                sl = entry - SL_PTS
            
            if spot >= (target - BUFFER): hit_target = True
            elif spot <= (sl - BUFFER): hit_sl = True

        if hit_target or hit_sl:
            actual_pnl = (spot - entry) if ttype == 'CALL' else (entry - spot)
            open_trade.exit_spot = spot
            open_trade.exit_time = current_time
            open_trade.result = "TARGET" if hit_target else "SL"
            open_trade.pnl = round(actual_pnl, 2)
            open_trade.save()
            print(f"[{current_time}] 🟢 TRADE CLOSED | {ttype} | Result: {open_trade.result} | PNL: {open_trade.pnl}")

    # 🛑 FIREWALL: अगर डेटाबेस में अभी भी कोई OPEN ट्रेड है, तो यहीं से वापस लौट जाओ! (NO NEW ENTRY)
    if PaperTrade.objects.filter(symbol=symbol, trade_date=today, result="OPEN").exists():
        return "Trade is Running"

    # ==========================================
    # ── 4. MANUAL PENDING TRADES LOGIC ──
    # ==========================================
    pending_trades = PaperTrade.objects.filter(symbol=symbol, trade_date=today, result="PENDING")
    
    for pt in pending_trades:
        if pt.trade_type == 'CALL' and spot <= pt.trigger_price:
            pt.result = 'OPEN'
            pt.entry_spot = spot
            pt.entry_time = current_time
            pt.save()
            print(f"[{current_time}] 🎯 MANUAL ENTRY: BUY CALL @ {spot}")
            return "Manual Trade Opened" # तुरंत वापस लौटें

        elif pt.trade_type == 'PUT' and spot >= pt.trigger_price:
            pt.result = 'OPEN'
            pt.entry_spot = spot
            pt.entry_time = current_time
            pt.save()
            print(f"[{current_time}] 🎯 MANUAL ENTRY: BUY PUT @ {spot}")
            return "Manual Trade Opened" # तुरंत वापस लौटें



    # ==========================================
    # ── 6. AUTOMATIC ENTRY LOGIC ──
    # ==========================================
    if r_level and spot >= r_level:
        PaperTrade.objects.create(
            symbol=symbol, trade_type='PUT', entry_time=current_time, entry_spot=spot,
            trigger_level='R', trigger_price=r_level,
            entry_strike=eff_res  # <--- यह लाइन जोड़ें
        )
        print(f"[{current_time}] 🔴 AUTO ENTRY: BUY PUT @ {spot} (R={r_level})")
        return "Put Trade Opened"

    elif s_level and spot <= s_level:
        PaperTrade.objects.create(
            symbol=symbol, trade_type='CALL', entry_time=current_time, entry_spot=spot,
            trigger_level='S', trigger_price=s_level,
            entry_strike=eff_sup  # <--- यह लाइन जोड़ें
        )
        print(f"[{current_time}] 🟢 AUTO ENTRY: BUY CALL @ {spot} (S={s_level})")
        return "Call Trade Opened"

def run_live_paper_trading(df, symbol="NIFTY", master_levels=None):
    today        = timezone.now().date()
    spot         = float(df["Spot_Price"].iloc[0])
    current_time = df["Time"].iloc[0]
    step         = 100 if "BANKNIFTY" in symbol or "SENSEX" in symbol else 50

    # ==========================================
    # ── 1. LIVE ADMIN SETTINGS ──
    # ==========================================
    settings, _ = BotSettings.objects.get_or_create(id=1)
    if not settings.trading_enabled:
        return "Trading Disabled via Admin"

    TARGET_PTS = settings.default_target
    SL_PTS     = settings.default_sl
    BUFFER     = settings.reversal_buffer

    # ==========================================
    # ── 2. MASTER LEVEL CALCULATION ──
    #    ✅ FIX Bug 4: बाहर से आए master_levels reuse करो
    #    (run_sync_async पहले ही call कर चुका है — double query नहीं)
    # ==========================================
    if master_levels is None:
        master_levels = get_master_levels(symbol, today)

    eff_res        = master_levels["R"]["strike"]
    r_level        = master_levels["R"]["entry"]
    r_target_level = master_levels["R"]["target"]
    r_sl_level     = master_levels["R"]["sl"]
    r_tag          = master_levels["R"].get("tag", "R")

    eff_sup        = master_levels["S"]["strike"]
    s_level        = master_levels["S"]["entry"]
    s_target_level = master_levels["S"]["target"]
    s_sl_level     = master_levels["S"]["sl"]
    s_tag          = master_levels["S"].get("tag", "S")

    

    # 👇👇👇 यह FIX जोड़ें: स्पेस और कॉमा हटाकर Float में बदलें 👇👇👇
    r_level = float("".join(str(r_level).split()).replace(",", "")) if r_level else None
    s_level = float("".join(str(s_level).split()).replace(",", "")) if s_level else None
    # 👆👆👆 ======================================================== 👆👆👆

    if not eff_res or not eff_sup:
        return "No SR Data"
    
    # print trend and gap for logging
    log_time = localtime().strftime("%H:%M:%S")

    
    put_gap = (r_level - spot) if r_level is not None else None
    call_gap = (spot - s_level) if s_level is not None else None

    # दोनों गैप मौजूद होने पर ही तुलना करें
    if put_gap is not None and call_gap is not None:
        if put_gap > call_gap:
            print(f"[{log_time}] 📊 TICK: Spot={spot} 🟢 Buy CALL @ S Strike {eff_sup} To Level {s_level} (Gap: {call_gap:.2f})")
        else:
            print(f"[{log_time}] 📊 TICK: Spot={spot} 🔴 Buy PUT @ R Strike {eff_res} To Level {r_level} (Gap: {put_gap:.2f})")
    elif call_gap is not None:
        print(f"[{log_time}] 📊 TICK: Spot={spot} 🟢 Buy CALL @ S Strike {eff_sup} To Level {s_level} (Gap: {call_gap:.2f})")
    elif put_gap is not None:
        print(f"[{log_time}] 📊 TICK: Spot={spot} 🔴 Buy PUT @ R Strike {eff_res} To Level {r_level} (Gap: {put_gap:.2f})")
    else:
        print(f"[{log_time}] 📊 TICK: Spot={spot} ⚪ Waiting for valid Resistance/Support Levels...")
    # ==========================================
    # ── 3. EXIT LOGIC (सभी Open Trades के लिए) ──
    # ==========================================
    open_trades = PaperTrade.objects.filter(symbol=symbol, trade_date=today, result="OPEN")
    # print(f"[{current_time}] 🔍 Checking {open_trades.count()} open trades for exit conditions...")
    # 👇 1. लूप के बाहर यह नया फ्लैग बनाएँ 👇
    trade_closed_in_this_tick = False

    # 15:15 के बाद मार्केट क्लोज हो चुका होगा, तो सभी ट्रेड्स को क्लोज कर देना है
    local_current_time = localtime(current_time)
    market_close_time = local_current_time.replace(hour=15, minute=15, second=0, microsecond=0)
    force_close = local_current_time >= market_close_time

    for open_trade in open_trades:
        entry  = float(open_trade.entry_spot)
        ttype  = open_trade.trade_type
        entry_strike = float(open_trade.entry_strike) if open_trade.entry_strike else None
        # print(f"    [Debug] Evaluating Trade ID {open_trade.id} | Type: {ttype} | Entry: {entry} | Entry Strike: {entry_strike}")

        # ✅ FIX Bug 1: trade की actual entry_strike से target/SL निकालो
        # master_levels में shifted strike हो सकती है — वो इस trade की नहीं
        entry_strike = float(open_trade.entry_strike) if open_trade.entry_strike else None

        hit_target = hit_sl = False

        # अगर 15:15 हो चुके हैं तो सीधे फोर्स क्लोज करें
        if force_close:
            hit_target = True
            print(f"    [Debug] Market Close Time Reached | Force Closing Trade")
     
        elif ttype == 'PUT':
            if entry_strike:
                # इस trade की actual strike के neighbor से target/SL
                target = get_rev_val_direct(symbol, today, entry_strike - step, 'CE') or r_target_level or (entry - TARGET_PTS)
                sl     = get_rev_val_direct(symbol, today, entry_strike + step, 'CE') or r_sl_level     or (entry + SL_PTS)
            else:
                target = r_target_level or (entry - TARGET_PTS)
                sl     = r_sl_level     or (entry + SL_PTS)

            # 🚨 Safety Lock
            if sl <= entry:
                sl = entry + SL_PTS

            if spot <= (target + BUFFER): hit_target = True
            elif spot >= (sl - BUFFER):   hit_sl     = True
            print(f"    [Debug] PUT Trade | Spot {spot} Entry Strike: {entry_strike} | Target: {target} | SL: {sl} | Hit Target Gep: {spot - target:.2f} | Hit SL: {sl - spot:.2f}")
        elif ttype == 'CALL':
            if entry_strike:
                target = get_rev_val_direct(symbol, today, entry_strike + step, 'PE') or s_target_level or (entry + TARGET_PTS)
                sl     = get_rev_val_direct(symbol, today, entry_strike - step, 'PE') or s_sl_level     or (entry - SL_PTS)
            else:
                target = s_target_level or (entry + TARGET_PTS)
                sl     = s_sl_level     or (entry - SL_PTS)

            # 🚨 Safety Lock
            if sl >= entry:
                sl = entry - SL_PTS

            if spot >= (target - BUFFER): hit_target = True
            elif spot <= (sl + BUFFER):   hit_sl     = True
            print(f"    [Debug] CALL Trade | Entry Strike: {entry_strike} | Target: {target} | SL: {sl} | Hit Target Gap: {target - spot:.2f} | Hit SL Gap: {spot - sl:.2f}")
        
            
        if hit_target or hit_sl:
            actual_pnl          = (spot - entry) if ttype == 'CALL' else (entry - spot)
            open_trade.exit_spot = spot
            open_trade.exit_time = current_time
            open_trade.result    = "TARGET" if hit_target else "SL"
            open_trade.pnl       = round(actual_pnl, 2)
            open_trade.save()
            print(f"[{current_time}] 🟢 TRADE CLOSED | {ttype} | Result: {open_trade.result} | PNL: {open_trade.pnl} | Strike: {entry_strike}")

            # 👇 2. जैसे ही ट्रेड क्लोज़ हो, इस फ्लैग को True कर दें 👇
            trade_closed_in_this_tick = True
    
    # 🛑 FIREWALL: OPEN trade है तो नई entry मत लो
    if PaperTrade.objects.filter(symbol=symbol, trade_date=today, result="OPEN").exists():
        return "Trade is Running"

    # 👇 3. NEW FIREWALL: अगर इसी टिक में ट्रेड क्लोज़ हुई है, तो तुरंत नई एंट्री मत लो! 👇
    if trade_closed_in_this_tick:
        print(f"[{current_time}] ⏳ Trade just closed. Waiting for next tick to refresh levels.")
        return "Trade Just Closed"
    
    # ==========================================
    # ── 4. TIME STOP LOGIC (लूप और फायरवॉल के बाहर) ──
    # ==========================================
    # पुराने ट्रेड्स चेक होने के बाद, अब नए ट्रेड्स के लिए 14:30 का टाइम रिस्ट्रिक्शन लगाएं
    market_stop_time = local_current_time.replace(hour=14, minute=30, second=0, microsecond=0)
    if local_current_time >= market_stop_time:
        print(f"[{local_current_time.strftime('%H:%M:%S')}] ⏰ Market Stop Time Reached | No new entries allowed")
        return "Market Stop Time Reached"
    
    # ==========================================
    # ── 5. MANUAL PENDING TRADES LOGIC ──
    # ==========================================
    pending_trades = PaperTrade.objects.filter(symbol=symbol, trade_date=today, result="PENDING")

    for pt in pending_trades:
        if pt.trade_type == 'CALL' and spot <= pt.trigger_price:
            pt.result     = 'OPEN'
            pt.entry_spot = spot
            pt.entry_time = current_time
            pt.save()
            print(f"[{current_time}] 🎯 MANUAL ENTRY: BUY CALL @ {spot}")
            return "Manual Trade Opened"

        elif pt.trade_type == 'PUT' and spot >= pt.trigger_price:
            pt.result     = 'OPEN'
            pt.entry_spot = spot
            pt.entry_time = current_time
            pt.save()
            print(f"[{current_time}] 🎯 MANUAL ENTRY: BUY PUT @ {spot}")
            return "Manual Trade Opened"

    # ==========================================
    # ── 5. AUTOMATIC ENTRY LOGIC ──
    # ==========================================
    
        
    # if r_level and spot >= (r_level - BUFFER):        # ← BUFFER add
    #     PaperTrade.objects.create(
    #         symbol=symbol, trade_type='PUT',
    #         entry_time=current_time, entry_spot=spot,
    #         trigger_level='R', trigger_price=r_level,
    #         entry_strike=eff_res,
    #     )
    #     print(f"[{current_time}] 🔴 PUT ENTRY @ {spot} (R={r_level}, diff={spot-r_level:.2f})")
    #     return "Put Trade Opened"

    # elif s_level and spot <= (s_level + BUFFER):      # ← BUFFER add
    #     PaperTrade.objects.create(
    #         symbol=symbol, trade_type='CALL',
    #         entry_time=current_time, entry_spot=spot,
    #         trigger_level='S', trigger_price=s_level,
    #         entry_strike=eff_sup,
    #     )
    #     print(f"[{current_time}] 🟢 CALL ENTRY @ {spot} (S={s_level}, diff={s_level-spot:.2f})")
    #     return "Call Trade Opened"

    # ==========================================
    # ── 5. AUTOMATIC ENTRY LOGIC ──
    # ==========================================
    
    if r_level and spot >= (r_level - BUFFER):
        PaperTrade.objects.create(
            symbol=symbol, trade_type='PUT',
            entry_time=current_time, entry_spot=spot,
            trigger_level=r_tag,  # 👈 यहाँ r_tag लगाएँ
            trigger_price=r_level,
            entry_strike=eff_res,
        )
        print(f"[{current_time}] 🔴 PUT ENTRY @ {spot} (Level={r_tag}, Strike={eff_res}, diff={spot-r_level:.2f})")
        return "Put Trade Opened"

    elif s_level and spot <= (s_level + BUFFER):
        PaperTrade.objects.create(
            symbol=symbol, trade_type='CALL',
            entry_time=current_time, entry_spot=spot,
            trigger_level=s_tag,  # 👈 यहाँ s_tag लगाएँ
            trigger_price=s_level,
            entry_strike=eff_sup,
        )
        print(f"[{current_time}] 🟢 CALL ENTRY @ {spot} (Level={s_tag}, Strike={eff_sup}, diff={s_level-spot:.2f})")
        return "Call Trade Opened"

def get_rev_val_direct(symbol, selected_date, strike, side, period=5):
    """
    ✅ FIX Bug 1 के लिए helper:
    किसी specific strike की reversal value निकालना।
    Trade की actual entry_strike के लिए target/SL calculate करने में use होता है।
    """
    from django.core.cache import caches
    from mystock.models import OptionChain

    today = timezone.now().date()

    # आज है → Redis से
    if selected_date == today:
        try:
            history_key  = f"moving_history_all_{symbol.upper()}"
            history_data = caches['default'].get(history_key) or caches['db_cache'].get(history_key)
            if history_data:
                strike_float = float(strike)
                if strike_float in history_data:
                    hist_key  = 'ce_hist' if side == 'CE' else 'pe_hist'
                    full_hist = history_data[strike_float].get(hist_key, [])
                    last_n    = full_hist[-period:]
                    vals      = [float(t['value']) for t in last_n if float(t.get('value', 0)) > 0]
                    if vals:
                        return round(sum(vals) / len(vals), 2)
        except Exception:
            pass

    # पुरानी date या cache miss → DB से
    try:
        col  = 'Reversl_Ce' if side == 'CE' else 'Reversl_Pe'
        rows = (
            OptionChain.objects
            .filter(Symbol__iexact=symbol, Time__date=selected_date, Strike_Price=strike)
            .order_by('-Time')
            .values(col)[:period]
        )
        vals = [float(r[col]) for r in rows if r[col] and float(r[col]) > 0]
        if vals:
            return round(sum(vals) / len(vals), 2)
    except Exception:
        pass

    return None

