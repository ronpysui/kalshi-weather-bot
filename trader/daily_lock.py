"""
Daily bet lock.

Ensures bets are placed exactly ONCE per day at BET_HOUR_ET (default 7 AM ET).
After the morning bet window, the bot monitors only — no new orders.

Lock file format (data/daily_lock.json):
{
    "date": "2026-03-26",
    "locked_at": "2026-03-26T07:03:12",
    "forecast": 72.0,
    "sigma": 3.0,
    "bets": [
        {
            "ticker":    "KXHIGHNY-26MAR26-T72",
            "side":      "no",
            "contracts": 12,
            "price":     21,
            "edge":      39.9,
            "our_prob":  59.9,
            "label":     "73° or above"
        },
        ...
    ]
}
"""

import json
import os
from datetime import datetime, date
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


def is_prediction_locked() -> bool:
    """Return True if today's prediction (forecast + sigma) has been fixed."""
    lock = _load()
    return lock.get("date") == today_str()


def is_locked() -> bool:
    """Alias for is_prediction_locked() — kept for backward compatibility."""
    return is_prediction_locked()


def bets_are_placed() -> bool:
    """Return True if orders have already been placed today."""
    lock = _load()
    return lock.get("date") == today_str() and lock.get("bets_placed", False)


def get_lock() -> dict | None:
    """Return today's lock data, or None if not yet locked."""
    lock = _load()
    if lock.get("date") == today_str():
        return lock
    return None


def in_bet_window() -> bool:
    """
    Return True if we're currently in the morning bet window.
    Window: BET_HOUR_ET ≤ hour < BET_HOUR_ET + 1  (one-hour window).
    """
    hour = datetime.now(ET).hour
    return hour == config.BET_HOUR_ET


def should_bet() -> bool:
    """Return True if we should place orders right now (in window AND bets not yet placed)."""
    return in_bet_window() and not bets_are_placed()


def lock_prediction(forecast: float, sigma: float):
    """
    Lock today's prediction immediately on first daily fetch.
    This fixes the forecast + sigma for the whole day regardless of bet window.
    Orders are placed separately at BET_HOUR_ET via update_bets().
    """
    if is_prediction_locked():
        return  # already locked — don't overwrite
    now_et = datetime.now(ET)
    data = {
        "date":        today_str(),
        "locked_at":   now_et.strftime("%Y-%m-%dT%H:%M:%S"),
        "forecast":    forecast,
        "sigma":       sigma,
        "bets_placed": False,
        "bets":        [],
    }
    _save(data)


def update_bets(bets: list[dict]):
    """
    Record placed orders into today's existing lock file.
    Call this after orders fire at BET_HOUR_ET.
    """
    data = _load()
    if data.get("date") != today_str():
        return  # no prediction lock for today — shouldn't happen
    data["bets_placed"] = True
    data["bets"]        = bets
    _save(data)


def lock(forecast: float, sigma: float, bets: list[dict]):
    """
    Legacy one-shot lock (prediction + bets in one call).
    Kept for backward compatibility with main.py standalone mode.
    """
    now_et = datetime.now(ET)
    data = {
        "date":        today_str(),
        "locked_at":   now_et.strftime("%Y-%m-%dT%H:%M:%S"),
        "forecast":    forecast,
        "sigma":       sigma,
        "bets_placed": True,
        "bets":        bets,
    }
    _save(data)


def unlock_for_testing():
    """Remove today's lock — use for development/testing only."""
    if os.path.exists(config.DAILY_LOCK_PATH):
        os.remove(config.DAILY_LOCK_PATH)
