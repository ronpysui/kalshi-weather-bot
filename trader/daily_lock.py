"""
Daily prediction lock + opportunistic bet tracker.

Storage
-------
- If REDIS_URL env var is set (Railway): stores lock in Redis under
  key "kalshi:daily_lock" — survives restarts and redeploys forever.
- Otherwise: falls back to data/daily_lock.json (local dev).

Philosophy
----------
- Prediction (forecast + sigma) is locked ONCE at market open (6 AM ET).
- Bets are placed OPPORTUNISTICALLY throughout the day — the moment a
  bracket's edge clears the threshold, the order fires immediately.
- Each bracket ticker can only be bet ONCE per day (no doubling up).
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")
def _redis_key(city: str) -> str:
    return f"kalshi:daily_lock:{city}"


def _file_path(city: str) -> str:
    return f"data/daily_lock_{city}.json"


# ── Storage backend (Redis or file) ───────────────────────────────────────────

def _get_redis():
    """Return a Redis client if REDIS_URL is configured, else None."""
    url = os.getenv("REDIS_URL") or os.getenv("REDIS_PRIVATE_URL")
    if not url:
        return None
    try:
        import redis
        return redis.from_url(url, decode_responses=True)
    except Exception:
        return None


def _load(city: str = None) -> dict:
    city = city or config.DEFAULT_CITY
    r = _get_redis()
    if r:
        try:
            raw = r.get(_redis_key(city))
            return json.loads(raw) if raw else {}
        except Exception:
            pass
    # File fallback
    path = _file_path(city)
    if not os.path.exists(path):
        # backward compat: try old single-city path
        if os.path.exists(config.DAILY_LOCK_PATH):
            try:
                with open(config.DAILY_LOCK_PATH) as f:
                    return json.load(f)
            except Exception:
                pass
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict, city: str = None):
    city = city or config.DEFAULT_CITY
    r = _get_redis()
    if r:
        try:
            r.setex(_redis_key(city), 48 * 3600, json.dumps(data))
            return
        except Exception:
            pass
    # File fallback
    os.makedirs("data", exist_ok=True)
    with open(_file_path(city), "w") as f:
        json.dump(data, f, indent=2)


def today_str() -> str:
    return datetime.now(ET).date().isoformat()


# ── Prediction lock ────────────────────────────────────────────────────────────

def is_prediction_locked(city: str = None) -> bool:
    return _load(city).get("date") == today_str()


def is_locked(city: str = None) -> bool:
    return is_prediction_locked(city)


def get_lock(city: str = None) -> dict | None:
    lock = _load(city)
    return lock if lock.get("date") == today_str() else None


def lock_prediction(forecast: float, sigma: float, city: str = None):
    """Lock forecast + sigma at market open. No-op if already locked today."""
    city = city or config.DEFAULT_CITY
    if is_prediction_locked(city):
        return
    now_et = datetime.now(ET)
    open_time = now_et.replace(
        hour=config.MARKET_OPEN_HOUR_ET, minute=0, second=0, microsecond=0
    )
    _save({
        "date":        today_str(),
        "locked_at":   open_time.strftime("%Y-%m-%dT%H:%M:%S"),
        "forecast":    forecast,
        "sigma":       sigma,
        "bets_placed": False,
        "bets":        [],
    }, city)


# ── Opportunistic bet tracker ──────────────────────────────────────────────────

def already_bet(ticker: str, city: str = None) -> bool:
    """Return True if this bracket ticker has already been bet today."""
    lock = get_lock(city)
    if not lock:
        return False
    return any(b["ticker"] == ticker for b in lock.get("bets", []))


def record_bet(bet: dict, city: str = None):
    """Add a single placed bet to today's lock. Call immediately after order fires."""
    city = city or config.DEFAULT_CITY
    data = _load(city)
    if data.get("date") != today_str():
        return
    data.setdefault("bets", []).append(bet)
    data["bets_placed"] = True
    _save(data, city)


def bets_are_placed(city: str = None) -> bool:
    lock = get_lock(city)
    return bool(lock and lock.get("bets_placed", False))


# ── Market window helpers ──────────────────────────────────────────────────────

def in_bet_window() -> bool:
    hour = datetime.now(ET).hour
    return config.MARKET_OPEN_HOUR_ET <= hour < config.MARKET_CLOSE_HOUR_ET


def should_bet() -> bool:
    return in_bet_window()


# ── Legacy helpers (backward compat) ──────────────────────────────────────────

def update_bets(bets: list[dict]):
    data = _load()
    if data.get("date") != today_str():
        return
    data["bets_placed"] = True
    data["bets"]        = bets
    _save(data)


def lock(forecast: float, sigma: float, bets: list[dict]):
    now_et = datetime.now(ET)
    _save({
        "date":        today_str(),
        "locked_at":   now_et.strftime("%Y-%m-%dT%H:%M:%S"),
        "forecast":    forecast,
        "sigma":       sigma,
        "bets_placed": bool(bets),
        "bets":        bets,
    })


def unlock_for_testing():
    r = _get_redis()
    if r:
        try:
            r.delete(REDIS_KEY)
        except Exception:
            pass
    if os.path.exists(config.DAILY_LOCK_PATH):
        os.remove(config.DAILY_LOCK_PATH)
