from django.shortcuts import render
import pandas as pd
import requests
import numpy as np
from . import credentials as cd
from datetime import datetime
from django.utils import timezone
from django.db import transaction
from .models import SupportResistance, OptionChain
import traceback
import time
from requests.exceptions import SSLError, ConnectionError, Timeout

access_token = cd.access_token
expiry_Date = "2026-02-03"  # YYYY-MM-DD

def safe_get(url, headers=None, params=None, retries=3, timeout=10):
    for attempt in range(retries):
        try:
            return requests.get(
                url,
                headers=headers,
                params=params,
                timeout=timeout
            ).json()

        except (SSLError, ConnectionError, Timeout) as e:
            if attempt == retries - 1:
                raise
            time.sleep(1)   # small backoff



def get_instrument_key(symbol):
    scripts = pd.read_csv('NSE.csv')
    filtered = scripts[scripts['tradingsymbol'] == symbol]

    if not filtered.empty:
        return filtered['instrument_key'].values[0]

    fallback = {
        'NIFTY': 'NSE_INDEX|Nifty 50',
        'BANKNIFTY': 'NSE_INDEX|Nifty Bank',
        'FINNIFTY': 'NSE_INDEX|Nifty Fin Service',
        'MIDCPNIFTY': 'NSE_INDEX|NIFTY MID SELECT',
        'CRUDEOIL': 'MCX_FO|436953'
    }
    return fallback.get(symbol, None)

def get_Name_Lot_size(symbol):
    key = get_instrument_key(symbol)
    url = "https://api.upstox.com/v2/option/contract"

    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {access_token}'
    }

    try:
        response = safe_get(
            url,
            headers=headers,
            params={'instrument_key': key}
        )

        if "data" not in response or not response["data"]:
            return None, None

        data = response["data"][0]
        return data.get("underlying_symbol"), data.get("lot_size")

    except Exception as e:
        print(f"⚠ Lot size fetch failed {symbol}: {e}")
        return None, None


def get_option_chain(symbol, expiry_Date):
    key = get_instrument_key(symbol)

    url = 'https://api.upstox.com/v2/option/chain'
    params = {'instrument_key': key, 'expiry_date': expiry_Date}
    headers = {
        'Accept': 'application/json',
        'Authorization': f'Bearer {access_token}'
    }

    res = requests.get(url, params=params, headers=headers, timeout=5)

    # print("\n\n🔍 RAW API RESPONSE:\n", res.text)   # <---- ADD THIS

    try:
        data = res.json()
    except:
        return None

    if "data" not in data or len(data["data"]) == 0:
        print("⚠ Empty option chain received!")
        return None

    return data


def data_to_df(symbol, expiry_Date):
    response_data = get_option_chain(symbol, expiry_Date)

    if response_data is None:
        return None

    symbol_name, lot_size = get_Name_Lot_size(symbol)

    if symbol_name is None or lot_size is None:
        return None

    data = response_data.get("data", [])
    now = datetime.now().strftime('%d/%m/%Y %H:%M:%S')
  

    rows = []

    for entry in data:

        strike = entry.get("strike_price")

        # ---------------- SAFE CE ----------------
        ce_options = entry.get("call_options") or {}
        ce_md = ce_options.get("market_data") or {}
        ce_g = ce_options.get("option_greeks") or {}

        ce_ltp = ce_md.get("ltp", 0)
        ce_cltp = ce_md.get("close_price", 0)
        ce_oi = ce_md.get("oi", 0)
        ce_prev_oi = ce_md.get("prev_oi", 0)
        ce_volume = ce_md.get("volume", 0)

        ce_delta = ce_g.get("delta", 0)
        ce_iv = ce_g.get("iv", 0)

        # ---------------- SAFE PE ----------------
        pe_options = entry.get("put_options") or {}
        pe_md = pe_options.get("market_data") or {}
        pe_g = pe_options.get("option_greeks") or {}

        pe_ltp = pe_md.get("ltp", 0)
        pe_cltp = pe_md.get("close_price", 0)
        pe_oi = pe_md.get("oi", 0)
        pe_prev_oi = pe_md.get("prev_oi", 0)
        pe_volume = pe_md.get("volume", 0)

        pe_delta = pe_g.get("delta", 0)
        pe_iv = pe_g.get("iv", 0)

        # ---------------- ROW APPEND ----------------
        rows.append([
            now, symbol_name,
            ce_delta, "",
            ce_iv, "",
            (ce_oi - ce_prev_oi) / lot_size,
            "",
            ce_oi / lot_size,
            "",
            ce_volume / lot_size,
            ce_ltp - ce_cltp,
            ce_ltp,
            "",
            strike,
            "",
            pe_ltp,
            (pe_ltp - pe_cltp),
            pe_volume / lot_size,
            "",
            pe_oi / lot_size,
            "",
            (pe_oi - pe_prev_oi) / lot_size,
            "",
            pe_iv,
            "",
            pe_delta,
            ""
        ])

    # DataFrame
    columns = [
        "Time", "Symbol", "CE Delta", "CE RANGE", "CE IV", "CE COI percent",
        "CE COI", "CE OI percent", "CE OI", "CE Volume percent",
        "CE Volume", "CE CLTP", "CE LTP", "Reversl Ce",
        "Strike Price", "Reversl Pe", "PE LTP", "PE CLTP",
        "PE Volume", "PE Volume percent", "PE OI", "PE OI percent",
        "PE COI", "PE COI percent", "PE IV", "PE RANGE",
        "PE Delta", "Spot Price"
    ]

    df = pd.DataFrame(rows, columns=columns)
    df = df.sort_values(by="Strike Price").reset_index(drop=True)
    return df

def calculate_data(symbol, expiry_Date):
    response_data = get_option_chain(symbol, expiry_Date)
    if response_data is None:
        return None

    spot_price = response_data['data'][0].get('underlying_spot_price', 0)
    df = data_to_df(symbol, expiry_Date)
    if df is None:
        return None

    df["Reversl Ce"] = ((df["PE LTP"] - df["CE LTP"].shift(-1)) + spot_price).round(2)
    df["Reversl Pe"] = ((df["PE LTP"].shift(1) - df["CE LTP"]) + spot_price).round(2)

    ce_oi = df["CE OI"].replace(0, np.nan)
    pe_oi = df["PE OI"].replace(0, np.nan)

    df["CE RANGE"] = ((np.maximum(ce_oi - pe_oi, 0) / ce_oi) * 100).round(2).fillna(0)
    df["PE RANGE"] = ((np.maximum(pe_oi - ce_oi, 0) / pe_oi) * 100).round(2).fillna(0)

    df["Spot Price"] = spot_price

    def pct(col):
        maxv = col.max()
        return ((col / maxv * 100).round(2)) if maxv > 0 else 0.00

    df["CE OI percent"] = pct(df["CE OI"])
    df["PE OI percent"] = pct(df["PE OI"])
    df["CE Volume percent"] = pct(df["CE Volume"])
    df["PE Volume percent"] = pct(df["PE Volume"])
    df["CE COI percent"] = pct(df["CE COI"])
    df["PE COI percent"] = pct(df["PE COI"])
    # ---------- UNDERSCORE CONSISTENCY ----------
    df.columns = df.columns.str.replace(" ", "_")

    return df

def strike_price_selector(df, count=15):
    spot_price = df["Spot_Price"].iloc[0]

    # नीचे की strikes (ATM से नीचे)
    below_spot = (
        df[df["Strike_Price"] < spot_price]
        .sort_values("Strike_Price", ascending=False)
        .head(count)
    )

    # ऊपर की strikes (ATM से ऊपर)
    above_spot = (
        df[df["Strike_Price"] > spot_price]
        .sort_values("Strike_Price", ascending=True)
        .head(count)
    )

    # Combine + sort
    filtered_df = (
        pd.concat([below_spot, above_spot])
        .sort_values("Strike_Price")
        .reset_index(drop=True)
       
    )

    return filtered_df

import math

def clean_float(val):
    try:
        if val is None:
            return None

        if isinstance(val, str) and val.strip() in ("", "-", "NA"):
            return None

        if isinstance(val, float) and math.isnan(val):
            return None

        return float(val)
    except:
        return None

def optionChain_save(symbol, expiry_Date):
    df = calculate_data(symbol, expiry_Date)
    df = strike_price_selector(df, count=15)

    if df is None or df.empty:
        print(f"❌ No data to save for {symbol}")
        return False
    now = timezone.now()
    try:
        with transaction.atomic():
            for _, row in df.iterrows():
                OptionChain.objects.create(
                    Time=now,
                    Symbol=row["Symbol"],

                    # Spot_Price=clean_float(row.get("Spot_Price")),
                    Spot_Price=clean_float(row["Spot_Price"]),
                    CE_Delta=clean_float(row["CE_Delta"]),
                    CE_RANGE=clean_float(row["CE_RANGE"]),
                    CE_IV=clean_float(row["CE_IV"]),

                    CE_COI_percent=clean_float(row["CE_COI_percent"]),
                    CE_COI=clean_float(row["CE_COI"]),
                    CE_OI_percent=clean_float(row["CE_OI_percent"]),
                    CE_OI=clean_float(row["CE_OI"]),
                    CE_Volume_percent=clean_float(row["CE_Volume_percent"]),
                    CE_Volume=clean_float(row["CE_Volume"]),
                    CE_CLTP=clean_float(row["CE_CLTP"]),
                    CE_LTP=clean_float(row["CE_LTP"]),

                    Reversl_Ce=clean_float(row["Reversl_Ce"]),

                    Strike_Price=clean_float(row["Strike_Price"]),

                    Reversl_Pe=clean_float(row["Reversl_Pe"]),

                    PE_LTP=clean_float(row["PE_LTP"]),
                    PE_CLTP=clean_float(row["PE_CLTP"]),
                    PE_Volume=clean_float(row["PE_Volume"]),
                    PE_Volume_percent=clean_float(row["PE_Volume_percent"]),
                    PE_OI=clean_float(row["PE_OI"]),
                    PE_OI_percent=clean_float(row["PE_OI_percent"]),
                    PE_COI=clean_float(row["PE_COI"]),
                    PE_COI_percent=clean_float(row["PE_COI_percent"]),
                    PE_IV=clean_float(row["PE_IV"]),
                    PE_RANGE=clean_float(row["PE_RANGE"]),
                    PE_Delta=clean_float(row["PE_Delta"]),
                )

        print(f"✅ Option chain data saved for {symbol}")
        return True

    except Exception as e:
        print(f"🔥 Error saving option chain for {symbol}: {e}")
        traceback.print_exc()
        return False

def optionChain_live_loop(symbol, expiry_Date, delay=20):
    print(f"🚀 Live option chain loop started for {symbol}")


    while True:
        try:
            # df = calculate_data(symbol, expiry_Date)
            optionChain_save(symbol, expiry_Date)
            time.sleep(delay)   # seconds (5, 10, 30 etc)

        except KeyboardInterrupt:
            print("⛔ Loop stopped manually")
            break

        except Exception as e:
            print("🔥 Loop error:", e)
            time.sleep(5)

from django.shortcuts import render
from django.http import JsonResponse
from .models import OptionChain


# 👉 Page render करने के लिए
def option_chain_page(request):
    # optionChain_live_loop("NIFTY", "2026-02-03", delay=10)
    return render(request, "stock_market/option_chain_live.html")


# 👉 AJAX API (LIVE DATA)
def option_chain_api1(request):
    symbol = request.GET.get("symbol", "NIFTY")

    qs = (
        OptionChain.objects
        .filter(Symbol=symbol)
        .order_by("-Time")[:30]
    )

    data = []
    for row in qs:
        data.append({
            "time": row.Time.strftime("%H:%M:%S"),
            "ce_iv": row.CE_IV,
            "ce_delta": row.CE_Delta,
            "ce_range": row.CE_RANGE,
            "ce_coi": row.CE_COI,
            "ce_oi": row.CE_OI,
            "ce_volume_percent": row.CE_Volume_percent,
            "ce_volume": row.CE_Volume,
            "ce_cltp": row.CE_CLTP,
            "ce_ltp": row.CE_LTP,
            "ce_reversl": row.Reversl_Ce,
            "strike_price": row.Strike_Price,
            "pe_reversl": row.Reversl_Pe,
            "pe_ltp": row.PE_LTP,
            "pe_cltp": row.PE_CLTP,
            "pe_volume_percent": row.PE_Volume_percent,
            "pe_volume": row.PE_Volume,
            "pe_oi": row.PE_OI,
            "pe_coi": row.PE_COI,
            "pe_range": row.PE_RANGE,
            "pe_delta": row.PE_Delta,
            "pe_iv": row.PE_IV,
            
        })

    return JsonResponse(data, safe=False)

def option_chain_api(request):
    symbol = request.GET.get("symbol", "NIFTY")
    valid_expiry = get_valid_expiry(symbol)
    expiry = request.GET.get("expiry") or valid_expiry
    optionChain_save(symbol, expiry)

    qs = (
        OptionChain.objects
        .filter(Symbol=symbol)
        .order_by("-Time")[:25]
    )

    data = []
    for row in qs:
        data.append({
            "time": timezone.localtime(row.Time).strftime("%H:%M:%S"),

            "ce_iv": row.CE_IV,
            "ce_delta": row.CE_Delta,
            "ce_range": row.CE_RANGE,
            "ce_coi_percent": row.CE_COI_percent,
            "ce_coi": row.CE_COI,
            "ce_oi_percent": row.CE_OI_percent,
            "ce_oi": row.CE_OI,
            "ce_volume_percent": row.CE_Volume_percent,
            "ce_volume": row.CE_Volume,
            "ce_cltp": row.CE_CLTP,
            "ce_ltp": row.CE_LTP,
            "ce_reversl": row.Reversl_Ce,
            "strike_price": row.Strike_Price,
            "pe_reversl": row.Reversl_Pe,
            "pe_ltp": row.PE_LTP,
            "pe_cltp": row.PE_CLTP,
            "pe_volume_percent": row.PE_Volume_percent,
            "pe_volume": row.PE_Volume,
            "pe_oi": row.PE_OI,
            "pe_oi_percent": row.PE_OI_percent,
            "pe_coi": row.PE_COI,
            "pe_coi_percent": row.PE_COI_percent,
            "pe_range": row.PE_RANGE,
            "pe_delta": row.PE_Delta,
            "pe_iv": row.PE_IV,
            "spot":row.Spot_Price,
        })

    return JsonResponse(data, safe=False)

def strike_price_selector(df, count=10):
    spot_price = df["Spot_Price"].iloc[0]

    # नीचे की strikes (ATM से नीचे)
    below_spot = (
        df[df["Strike_Price"] < spot_price]
        .sort_values("Strike_Price", ascending=False)
        .head(count)
    )

    # ऊपर की strikes (ATM से ऊपर)
    above_spot = (
        df[df["Strike_Price"] > spot_price]
        .sort_values("Strike_Price", ascending=True)
        .head(count)
    )

    # Combine + sort
    filtered_df = (
        pd.concat([below_spot, above_spot])
        .sort_values("Strike_Price")
        .reset_index(drop=True)
       
    )

    return filtered_df

def format_ce_pe_columns1(df):
    # -------- DROP UNWANTED COLUMNS --------
    df = df.drop(columns=["Time", "Symbol", "spot_price"], errors="ignore")

    # -------- ROUND NUMERIC VALUES --------
    round_cols = [
        "CE Delta", "CE IV", "CE COI", "CE COI %",
        "CE OI", "CE OI %", "CE Volume", "CE Volume %",
        "PE Delta", "PE IV", "PE COI", "PE COI %",
        "PE OI", "PE OI %", "PE Volume", "PE Volume %"
    ]

    for col in round_cols:
        if col in df.columns:
            df[col] = df[col].astype(float).round(2)

    # -------- CE FORMAT --------
    df["CE Delta (CE IV)"] = df["CE Delta"].astype(str) + " (" + df["CE IV"].astype(str) + ")"
    df["CE COI (CE COI %)"] = df["CE COI"].astype(str) + " (" + df["CE COI %"].astype(str) + "%)"
    df["CE OI (CE OI %)"] = df["CE OI"].astype(str) + " (" + df["CE OI %"].astype(str) + "%)"
    df["CE Volume (CE Volume %)"] = df["CE Volume"].astype(str) + " (" + df["CE Volume %"].astype(str) + "%)"

    # -------- PE FORMAT --------
    df["PE Delta (PE IV)"] = df["PE Delta"].astype(str) + " (" + df["PE IV"].astype(str) + ")"
    df["PE COI (PE COI %)"] = df["PE COI"].astype(str) + " (" + df["PE COI %"].astype(str) + "%)"
    df["PE OI (PE OI %)"] = df["PE OI"].astype(str) + " (" + df["PE OI %"].astype(str) + "%)"
    df["PE Volume (PE Volume %)"] = df["PE Volume"].astype(str) + " (" + df["PE Volume %"].astype(str) + "%)"

    # -------- DROP ORIGINAL CE / PE COLUMNS --------
    df = df.drop(columns=[
        "CE Delta", "CE IV", "CE COI", "CE COI %",
        "CE OI", "CE OI %", "CE Volume", "CE Volume %",
        "PE Delta", "PE IV", "PE COI", "PE COI %",
        "PE OI", "PE OI %", "PE Volume", "PE Volume %"
    ], errors="ignore")

    # -------- FINAL COLUMN ORDER --------
    final_cols = [
        "Spot Price",
        "CE Delta (CE IV)",  "CE RANGE", "CE COI (CE COI %)", "CE OI (CE OI %)", "CE Volume (CE Volume %)", "CE CLTP", "CE LTP", "Reversl Ce",
        "Strike Price",
        "Reversl Pe", " PE LTP", "PE CLTP", "PE Volume (PE Volume %)", "PE OI (PE OI %)", "PE COI (PE COI %)", "PE RANGE", "PE Delta (PE IV)"
    ]
    # keep only existing columns
    final_cols = [c for c in final_cols if c in df.columns]
    df = df[final_cols]
    return df

def format_ce_pe_columns1(df):
    # -------- SAFE SPOT PRICE --------
   
    if "Spot Price" in df.columns:
        spot_price = df["Spot Price"].iloc[0]
    else:
        spot_price = None
    # -------- DROP UNWANTED COLUMNS --------
    df = df.drop(columns=["Time", "Symbol", "spot_price"], errors="ignore")

    # -------- ROUND NUMERIC VALUES --------
    round_cols = [
        "CE Delta", "CE IV", "CE COI", "CE COI %",
        "CE OI", "CE OI %", "CE Volume", "CE Volume %",
        "PE Delta", "PE IV", "PE COI", "PE COI %",
        "PE OI", "PE OI %", "PE Volume", "PE Volume %"
    ]

    def fmt(x):
        try:
            x = round(float(x), 2)
            return str(int(x)) if x.is_integer() else str(x)
        except:
            return ""

    # -------- ROUND NUMERIC VALUES --------
    for col in round_cols:
        if col in df.columns:
            df[col] = df[col].astype(float).round(2)

    # -------- CE FORMAT --------
    df["CE Delta (CE IV)"] = df["CE Delta"].apply(fmt) + " (" + df["CE IV"].apply(fmt) + ")"
    df["CE COI (CE COI %)"] = df["CE COI"].apply(fmt) + " (" + df["CE COI %"].apply(fmt) + "%)"
    df["CE OI (CE OI %)"] = df["CE OI"].apply(fmt) + " (" + df["CE OI %"].apply(fmt) + "%)"
    df["CE Volume (CE Volume %)"] = df["CE Volume"].apply(fmt) + " (" + df["CE Volume %"].apply(fmt) + "%)"

    # -------- PE FORMAT --------
    df["PE Delta (PE IV)"] = df["PE Delta"].apply(fmt) + " (" + df["PE IV"].apply(fmt) + ")"
    df["PE COI (PE COI %)"] = df["PE COI"].apply(fmt) + " (" + df["PE COI %"].apply(fmt) + "%)"
    df["PE OI (PE OI %)"] = df["PE OI"].apply(fmt) + " (" + df["PE OI %"].apply(fmt) + "%)"
    df["PE Volume (PE Volume %)"] = df["PE Volume"].apply(fmt) + " (" + df["PE Volume %"].apply(fmt) + "%)"


    # -------- DROP ORIGINAL CE / PE COLUMNS --------
    df = df.drop(columns=[
        "CE Delta", "CE IV", "CE COI", "CE COI %",
        "CE OI", "CE OI %", "CE Volume", "CE Volume %",
        "PE Delta", "PE IV", "PE COI", "PE COI %",
        "PE OI", "PE OI %", "PE Volume", "PE Volume %"
    ], errors="ignore")

    # -------- RENAME PE COLUMN HEADERS --------
    df = df.rename(columns={
        "CE Volume (CE Volume %)": "CE Volume",
        "CE OI (CE OI %)": "CE OI",
        "CE COI (CE COI %)": "CE COI",
        "CE Delta (CE IV)": "Delta IV CE",

        "PE Volume (PE Volume %)": "PE Volume",
        "PE OI (PE OI %)": "PE OI",
        "PE COI (PE COI %)": "PE COI",
        "PE Delta (PE IV)": "Delta IV PE",
    })
    

    # -------- FINAL COLUMN ORDER (CE ← Strike → PE) --------
    final_cols = [
        # ---- CE LEFT ----
        "Delta IV CE", "CE RANGE",
        "CE COI", "CE OI",
        "CE Volume", "CE CLTP", "CE LTP", "Reversl Ce",

        # ---- CENTER ----
        "Strike Price",

        # ---- PE RIGHT ----
        "Reversl Pe", "PE LTP", "PE CLTP",
        "PE Volume", "PE OI", "PE COI", 
        "PE RANGE", "Delta IV PE",
    ]

    final_cols = [c for c in final_cols if c in df.columns]
    df = df[final_cols].reset_index(drop=True)
    df.columns = (
    df.columns
      .str.strip()
      .str.replace(" ", "_")
)


    

    return df, spot_price

def format_ce_pe_columns(df):

    # ---------- SPOT PRICE ----------
    spot_price = df["Spot Price"].iloc[0] if "Spot Price" in df.columns else None

    # ---------- DROP UNWANTED ----------
    df = df.drop(columns=["Time", "Symbol"], errors="ignore")

    # ---------- SAFE FORMAT ----------
    def fmt(x):
        try:
            x = round(float(x), 2)
            return str(int(x)) if x.is_integer() else str(x)
        except:
            return ""

    # ---------- ENSURE REQUIRED COLUMNS ----------
    for col in ["CE IV", "PE IV"]:
        if col not in df.columns:
            df[col] = 0

    # ---------- CE FORMAT ----------
    df["Delta_IV_CE"] = df["CE Delta"].apply(fmt) + " (" + df["CE IV"].apply(fmt) + ")"
    df["CE_COI"] = df["CE COI"].apply(fmt) + " (" + df["CE COI %"].apply(fmt) + "%)"
    df["CE_OI"] = df["CE OI"].apply(fmt) + " (" + df["CE OI %"].apply(fmt) + "%)"
    df["CE_Volume"] = df["CE Volume"].apply(fmt) + " (" + df["CE Volume %"].apply(fmt) + "%)"

    # ---------- PE FORMAT ----------
    df["Delta_IV_PE"] = df["PE Delta"].apply(fmt) + " (" + df["PE IV"].apply(fmt) + ")"
    df["PE_COI"] = df["PE COI"].apply(fmt) + " (" + df["PE COI %"].apply(fmt) + "%)"
    df["PE_OI"] = df["PE OI"].apply(fmt) + " (" + df["PE OI %"].apply(fmt) + "%)"
    df["PE_Volume"] = df["PE Volume"].apply(fmt) + " (" + df["PE Volume %"].apply(fmt) + "%)"

    # ---------- DROP RAW ----------
    df = df.drop(columns=[
        "CE Delta", "CE IV", "CE COI", "CE COI %",
        "CE OI", "CE OI %", "CE Volume", "CE Volume %",
        "PE Delta", "PE IV", "PE COI", "PE COI %",
        "PE OI", "PE OI %", "PE Volume", "PE Volume %"
    ], errors="ignore")

    # ---------- FINAL ORDER ----------
    final_cols = [
        "Delta_IV_CE", "CE RANGE",
        "CE_COI", "CE_OI", "CE_Volume",
        "CE CLTP", "CE LTP", "Reversl Ce",

        "Strike Price",

        "Reversl Pe", "PE LTP", "PE CLTP",
        "PE_Volume", "PE_OI", "PE_COI",
        "PE RANGE", "Delta_IV_PE",
    ]

    df = df[[c for c in final_cols if c in df.columns]].reset_index(drop=True)

    # ---------- UNDERSCORE CONSISTENCY ----------
    df.columns = df.columns.str.replace(" ", "_")

    return df, spot_price


def build_pe_ce_logic(Op_Data):
    result = {}

    # ---------------- COMMON ----------------
    result["Time"] = Op_Data["Time"].iloc[0]
    result["Symbol"] = Op_Data["Symbol"].iloc[0]
    result["Spot Price"] = float(Op_Data["Spot Price"].iloc[0])

    # ================= PE LOGIC =================
    sorted_pe = Op_Data.sort_values("PE OI %", ascending=False).reset_index(drop=True)

    if sorted_pe.shape[0] >= 2:
        pe_1 = sorted_pe.loc[0]
        pe_2 = sorted_pe.loc[1]

        result["Strike Price_Pe1"] = pe_1["Strike Price"]
        result["Reversl Pe"] = pe_1["Reversl Pe"]

        result["week_Pe %"] = pe_2["PE OI %"]
        result["Strike Price_Pe2"] = pe_2["Strike Price"]
        result["Reversl Pe_2"] = pe_2["Reversl Pe"]

        # ---- PE Strength / Trend ----
        result["s_t_b_Pe"] = (
            "Strong" if pe_2["PE OI %"] < 75 else
            "WTB" if pe_2["Strike Price"] < pe_1["Strike Price"] else
            "WTT"
        )

    # ================= CE LOGIC =================
    sorted_ce = Op_Data.sort_values("CE OI %", ascending=False).reset_index(drop=True)

    if sorted_ce.shape[0] >= 2:
        ce_1 = sorted_ce.loc[0]
        ce_2 = sorted_ce.loc[1]

        result["Strike Price_Ce1"] = ce_1["Strike Price"]
        result["Reversl Ce"] = ce_1["Reversl Ce"]

        result["week_Ce %"] = ce_2["CE OI %"]
        result["Strike Price_Ce2"] = ce_2["Strike Price"]
        result["Reversl Ce_2"] = ce_2["Reversl Ce"]

        # ---- CE Strength / Trend ----
        result["s_t_b_Ce"] = (
            "Strong" if ce_2["CE OI %"] < 75 else
            "WTB" if ce_2["Strike Price"] < ce_1["Strike Price"] else
            "WTT"
        )

    return result


def add_bullish_bearish_risk(top_row, Op_Data):
    spot_price = float(top_row["Spot Price"])

    # ---------- Bearish Risk ----------
    ce_below_spot = (
        Op_Data[Op_Data["Strike Price"] < spot_price]
        .sort_values("Strike Price", ascending=False)
        .head(10)
    )

    bearish_risk = int((ce_below_spot["CE LTP"] == 0).sum())

    # ---------- Bullish Risk ----------
    pe_above_spot = (
        Op_Data[Op_Data["Strike Price"] > spot_price]
        .sort_values("Strike Price", ascending=True)
        .head(10)
    )

    bullish_risk = int((pe_above_spot["PE LTP"] == 0).sum())

    top_row["Bearish Risk"] = bearish_risk
    top_row["Bullish Risk"] = bullish_risk

    return top_row

def save_top2_support_resistance(df, symbol):
    try:
        print("➡ Saving SR data for:", symbol)

        if df is None or df.empty:
            return False

        # ---------- Build PE / CE logic ----------
        top_row = build_pe_ce_logic(df)
        top_row = add_bullish_bearish_risk(top_row, df)

        pe_top = df.nlargest(2, "PE OI")
        ce_top = df.nlargest(2, "CE OI")

        if len(pe_top) < 2 or len(ce_top) < 2:
            print("❌ Not enough PE/CE rows")
            return False
        def safe_float(val):
            try:
                if val in ("", None):
                    return 0.0
                return float(val)
            except:
                return 0.0

        # 
        obj = SupportResistance.objects.create(
            # Time=top_row["Time"],
            Time = timezone.localtime(),
            Symbol=symbol,
            Spot_Price=safe_float(top_row["Spot Price"]),

            Strike_Price_Pe1=safe_float(pe_top.iloc[0]["Strike Price"]),
            Reversl_Pe=safe_float(pe_top.iloc[0]["Reversl Pe"]),
            week_Pe_1=safe_float(pe_top.iloc[0]["PE RANGE"]),

            Strike_Price_Pe2=safe_float(pe_top.iloc[1]["Strike Price"]),
            Reversl_Pe_2=safe_float(pe_top.iloc[1]["Reversl Pe"]),
            week_Pe_2=safe_float(pe_top.iloc[1]["PE RANGE"]),

            s_t_b_pe=top_row.get("s_t_b_Pe", ""),

            Strike_Price_Ce1=safe_float(ce_top.iloc[0]["Strike Price"]),
            Reversl_Ce=safe_float(ce_top.iloc[0]["Reversl Ce"]),
            week_Ce_1=safe_float(ce_top.iloc[0]["CE RANGE"]),

            Strike_Price_Ce2=safe_float(ce_top.iloc[1]["Strike Price"]),
            Reversl_Ce_2=safe_float(ce_top.iloc[1]["Reversl Ce"]),
            week_Ce_2=safe_float(ce_top.iloc[1]["CE RANGE"]),

            s_t_b_ce=top_row.get("s_t_b_Ce", ""),

            Bearish_Risk=int(top_row.get("Bearish Risk", 0)),
            Bullish_Risk=int(top_row.get("Bullish Risk", 0)),
        )

        print("✅ Saved SR row ID:", obj.id)
        return True

    except Exception:
        print("🔥 ERROR while saving SR")
        traceback.print_exc()
        return False
    
def get_valid_expiry(symbol):
    try:
        key = get_instrument_key(symbol)
        url = "https://api.upstox.com/v2/option/contract"

        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}"
        }

        params = {"instrument_key": key}
        res = requests.get(url, headers=headers, params=params, timeout=10).json()

        if "data" not in res or not res["data"]:
            return None

        # 👉 nearest expiry
        return res["data"][0]["expiry"]

    except Exception as e:
        print(f"Expiry fetch error {symbol}: {e}")
        return None



def option_chain_live(request):
    symbol = request.GET.get("symbol", "NIFTY")

    valid_expiry = get_valid_expiry(symbol)
    expiry = request.GET.get("expiry") or valid_expiry

    if not expiry:
        return render(request, "stock_market/option_chain.html", {
            # "spot price": spot_price,
            "symbol": symbol,
            "expiry": None,
            "table": None,
            "error": "⚠ Expiry not available. Please try again."
        })

    
    df = calculate_data(symbol, expiry)
    # df = strike_price_selector(df, count=10)
    # df, spot_price = format_ce_pe_columns(df)
    # optionChain_save(symbol, expiry)
    optionChain_live_loop(symbol, expiry, delay=10)


    if df is None or df.empty:
        return render(request, "stock_market/option_chain.html", {
            # "spot price": spot_price,
            "symbol": symbol,
            "expiry": expiry,
            "table": None,
            "error": "⚠ No data received."
        })

    # table_html = df.to_html(classes="table table-bordered table-striped")
    table_html = df.to_html(
    classes="table table-bordered table-striped",
    index=False,
    escape=False
)


    return render(request, "stock_market/option_chain.html", {
        "symbol": symbol,
        "expiry": expiry,
        "table": table_html
    })

