"""
Daily prediction lock + opportunistic bet tracker.

Philosophy
----------
- Prediction (forecast + sigma) is locked ONCE at market open (6 AM ET).
- Bets are placed OPPORTUNISTICALLY throughout the day — the moment a
  bracket's edge clears the threshold, the order fires immediately.
- Each bracket ticker can only be bet ONCE per day (no doubling up).
- The bot keeps scanning after placing bets — new opportunities can arise
  in other brackets as Kalshi prices shift.

Lock file format (data/daily_lock.json):
{
    "date":        "2026-03-26",
    "locked_at":   "2026-03-26T06:00:00",
    "forecast":    75.0,
    "sigma":       3.0,
    "bets_placed": true,          // true if at least one bet placed today
    "bets": [
        {
            "ticker":    "KXHIGHNY-26MAR26-T73",
            "side":      "yes",
            "contracts": 24,
            "price":     1,
            "edge":      95.7,
            "our_prob":  96.7,
            "label":     "70° or above"
        },
        ...
    ]
}
"""

import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import config

ET = ZoneInfo("America/New_York")


def _load() -> dict:
    if not os.path.exists(config.DAILY_LOCK_PATH):
        return {}
    try:
        with open(config.DAILY_LOCK_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    os.makedirs("data", exist_ok=True)
    with open(config.DAILY_LOCK_PATH, "w") as f:
        json.dump(data, f, indent=2)


def today_str() -> str:
    return datetime.now(ET).date().isoformat()


# ── Prediction lock ────────────────────────────────────────────────────────────

def is_prediction_locked() -> bool:
    """Return True if today's forecast + sigma have been locked."""
    return _load().get("date") == today_str()


def is_locked() -> bool:
    """Alias for is_prediction_locked() — backward compat."""
    return is_prediction_locked()


def get_lock() -> dict | None:
    """Return today's lock dict, or None if not yet locked."""
    lock = _load()
    return lock if lock.get("date") == today_str() else None


def lock_prediction(forecast: float, sigma: float):
    """
    Lock forecast + sigma at market open.
    Stamps locked_at as MARKET_OPEN_HOUR_ET to reflect opening prediction.
    No-op if already locked today.
    """
    if is_prediction_locked():
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
    })


# ── Opportunistic bet tracker ──────────────────────────────────────────────────

def already_bet(ticker: str) -> bool:
    """Return True if this bracket ticker has already been bet today."""
    lock = get_lock()
    if not lock:
        return False
    return any(b["ticker"] == ticker for b in lock.get("bets", []))


def record_bet(bet: dict):
    """
    Add a single placed bet to today's lock file.
    Call this immediately after each order fires.

    bet = {ticker, side, contracts, price, edge, our_prob, label}
    """
    data = _load()
    if data.get("date") != today_str():
        return  # no lock yet — shouldn't happen
    data.setdefault("bets", []).append(bet)
    data["bets_placed"] = True
    _save(data)


def bets_are_placed() -> bool:
    """Return True if at least one bet has been placed today."""
    lock = get_lock()
    return bool(lock and lock.get("bets_placed", False))


# ── Legacy helpers (kept for backward compat) ──────────────────────────────────

def in_bet_window() -> bool:
    """Market is open and we're past market open hour."""
    hour = datetime.now(ET).hour
    return config.MARKET_OPEN_HOUR_ET <= hour < config.MARKET_CLOSE_HOUR_ET


def should_bet() -> bool:
    """
    Legacy: returns True if in market hours.
    Main.py now uses already_bet(ticker) per-signal instead.
    """
    return in_bet_window()


def update_bets(bets: list[dict]):
    """Legacy batch update — kept for backward compat."""
    data = _load()
    if data.get("date") != today_str():
        return
    data["bets_placed"] = True
    data["bets"]        = bets
    _save(data)


def lock(forecast: float, sigma: float, bets: list[dict]):
    """Legacy one-shot lock — kept for backward compat."""
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
    """Remove today's lock — dev/testing only."""
    if os.path.exists(config.DAILY_LOCK_PATH):
        os.remove(config.DAILY_LOCK_PATH)
