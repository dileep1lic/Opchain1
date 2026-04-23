"""
redis_cache_manager.py
━━━━━━━━━━━━━━━━━━━━━━
Upstox Candle data को Redis में store/retrieve करना।
Redis बंद हो तो gracefully fallback — error spam नहीं।
"""

import json
import logging
from datetime import date, datetime
from typing import Optional

import redis
from django.conf import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Redis Connection (Singleton + Health Check)
# ─────────────────────────────────────────────
_redis_client: Optional[redis.Redis] = None
_redis_available: Optional[bool]     = None   # None = अभी check नहीं हुआ


def get_redis() -> Optional[redis.Redis]:
    """
    Redis client return करता है।
    Redis बंद हो तो None return करता है (exception नहीं फेंकता)।
    """
    global _redis_client, _redis_available

    # पहले से down पता है → directly None
    if _redis_available is False:
        return None

    try:
        if _redis_client is None:
            url = getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
            _redis_client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )

        _redis_client.ping()
        _redis_available = True
        return _redis_client

    except (redis.ConnectionError, redis.TimeoutError) as e:
        if _redis_available is not False:   # पहली बार error — सिर्फ एक बार log
            logger.warning(
                f"[Redis] ⚠️  Redis उपलब्ध नहीं है — Upstox fallback चलेगा।\n"
                f"        Windows: Memurai install करें → https://www.memurai.com\n"
                f"        WSL/Linux: sudo service redis-server start"
            )
        _redis_available = False
        _redis_client    = None
        return None


def reset_redis_connection():
    """Redis restart के बाद reconnect। Admin panel से call करें।"""
    global _redis_client, _redis_available
    _redis_client    = None
    _redis_available = None
    logger.info("[Redis] 🔄 Connection reset — अगली request पर reconnect होगा।")


# ─────────────────────────────────────────────
# Keys
# ─────────────────────────────────────────────

INTRADAY_TTL = 24 * 60 * 60


def _make_key(instrument_key: str, unit: str, interval: str) -> str:
    safe = instrument_key.replace("|", "__").replace(" ", "_")
    return f"candle:intraday:{safe}:{unit}:{interval}:{date.today().isoformat()}"


def _meta_key(instrument_key: str, unit: str, interval: str) -> str:
    safe = instrument_key.replace("|", "__").replace(" ", "_")
    return f"meta:updated:{safe}:{unit}:{interval}"


# ─────────────────────────────────────────────
# Save
# ─────────────────────────────────────────────

def save_intraday_candles(instrument_key, unit, interval, candles) -> bool:
    r = get_redis()
    if r is None:
        return False
    try:
        r.set(_make_key(instrument_key, unit, interval),
              json.dumps(candles), ex=INTRADAY_TTL)
        r.set(_meta_key(instrument_key, unit, interval),
              datetime.now().strftime("%H:%M:%S"), ex=INTRADAY_TTL)
        logger.info(f"[Redis] ✅ {len(candles)} candles saved | {instrument_key} {unit}/{interval}")
        return True
    except Exception as e:
        logger.error(f"[Redis] ❌ save error: {e}")
        return False


# ─────────────────────────────────────────────
# Load
# ─────────────────────────────────────────────

def load_intraday_candles(instrument_key, unit, interval) -> Optional[list]:
    r = get_redis()
    if r is None:
        return None   # Upstox fallback
    try:
        raw = r.get(_make_key(instrument_key, unit, interval))
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.error(f"[Redis] ❌ load error: {e}")
        return None


def get_last_updated(instrument_key, unit, interval) -> str:
    r = get_redis()
    if r is None:
        return ""
    try:
        return r.get(_meta_key(instrument_key, unit, interval)) or ""
    except Exception:
        return ""


# ─────────────────────────────────────────────
# Status (Admin/Debug)
# ─────────────────────────────────────────────

def redis_status() -> dict:
    r = get_redis()
    if r is None:
        return {
            "redis_running": False,
            "message": "Redis बंद है। Memurai (Windows) या redis-server (Linux) start करें।",
        }
    try:
        info = r.info("memory")
        return {
            "redis_running":     True,
            "candle_keys":       len(r.keys("candle:*")),
            "used_memory_human": info.get("used_memory_human", "?"),
        }
    except Exception as e:
        return {"redis_running": False, "error": str(e)}
