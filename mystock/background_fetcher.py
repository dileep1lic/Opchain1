"""
background_fetcher.py
━━━━━━━━━━━━━━━━━━━━━
हर 1 मिनट में Upstox v3 API से intraday candle data fetch करके Redis में save।
apps.py → ready() में start_background_fetcher() call करें।
"""

import logging
import threading
import time
from datetime import datetime

import requests as http_requests
from django.conf import settings

from .redis_cache_manager import save_intraday_candles

logger = logging.getLogger(__name__)

FETCH_INTERVAL_SECONDS = 60


# ─────────────────────────────────────────────
# Market Hours Check
# ─────────────────────────────────────────────

def is_market_open() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60 + 14) <= minutes <= (15 * 60 + 31)


# ─────────────────────────────────────────────
# Upstox v3 Intraday Fetch
# ─────────────────────────────────────────────

def _fetch_intraday(instrument_key: str, unit: str, interval: str) -> list:
    access_token = getattr(settings, "UPSTOX_ACCESS_TOKEN", "")
    if not access_token:
        print("[Fetcher] ❌ UPSTOX_ACCESS_TOKEN settings.py में नहीं है!")
        return []

    encoded_key = http_requests.utils.quote(instrument_key, safe="")
    url = (
        f"https://api.upstox.com/v3/historical-candle/intraday/"
        f"{encoded_key}/{unit}/{interval}"
    )
    headers = {
        "Content-Type":  "application/json",
        "Accept":        "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    try:
        resp = http_requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            print(f"[Fetcher] ⚠️  API {resp.status_code} for {instrument_key}: {resp.text[:200]}")
            return []

        raw = resp.json().get("data", {}).get("candles", [])
        candles = []
        for c in raw:
            candles.append({
                "time":   c[0],
                "open":   c[1],
                "high":   c[2],
                "low":    c[3],
                "close":  c[4],
                "volume": c[5],
                "oi":     c[6] if len(c) > 6 else 0,
            })
        candles.reverse()
        return candles

    except Exception as e:
        print(f"[Fetcher] ❌ Exception for {instrument_key}: {e}")
        return []


# ─────────────────────────────────────────────
# Main Loop
# ─────────────────────────────────────────────

def fetch_loop():
    print("[Fetcher] 🚀 Background data fetcher thread शुरू!")

    watched = getattr(settings, "WATCHED_INSTRUMENTS", [])

    if not watched:
        print("[Fetcher] ⚠️  WATCHED_INSTRUMENTS settings.py में नहीं है — fetcher बंद।")
        return

    print(f"[Fetcher] 📋 {len(watched)} instruments watch हो रहे हैं।")

    while True:
        try:
            now_str = datetime.now().strftime("%H:%M:%S")
            if is_market_open():
                print(f"[Fetcher] 🔄 Fetch cycle शुरू ({now_str})")
                for entry in watched:
                    ikey     = entry.get("instrument_key", "")
                    unit     = entry.get("unit", "minutes")
                    interval = entry.get("interval", "5")
                    if not ikey:
                        continue
                    candles = _fetch_intraday(ikey, unit, interval)
                    if candles:
                        save_intraday_candles(ikey, unit, interval, candles)
                        print(f"[Fetcher] ✅ {ikey} {unit}/{interval} → {len(candles)} candles saved")
                    else:
                        print(f"[Fetcher] ⚠️  {ikey} {unit}/{interval} → empty response")
            else:
                print(f"[Fetcher] 💤 Market बंद है ({now_str}) — अगला check 60s बाद")

        except Exception as e:
            print(f"[Fetcher] ❌ Loop error: {e}")

        time.sleep(FETCH_INTERVAL_SECONDS)


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────
_started = False


def start_background_fetcher():
    global _started
    if _started:
        print("[Fetcher] Thread already running — skip ✅")
        return
    t = threading.Thread(target=fetch_loop, daemon=True, name="UpstoxFetcher")
    t.start()
    _started = True
    print("[Fetcher] ✅ Thread launched successfully!")
