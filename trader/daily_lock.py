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


def is_locked() -> bool:
    """Return True if bets have already been placed today."""
    lock = _load()
    return lock.get("date") == today_str()


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
    """Return True if we should place bets right now (in window AND not locked)."""
    return in_bet_window() and not is_locked()


def lock(forecast: float, sigma: float, bets: list[dict]):
    """
    Write today's lock file with the bets that were placed.

    bets: list of dicts with keys: ticker, side, contracts, price, edge,
          our_prob, label
    """
    now_et = datetime.now(ET)
    data = {
        "date":      today_str(),
        "locked_at": now_et.strftime("%Y-%m-%dT%H:%M:%S"),
        "forecast":  forecast,
        "sigma":     sigma,
        "bets":      bets,
    }
    _save(data)


def unlock_for_testing():
    """Remove today's lock — use for development/testing only."""
    if os.path.exists(config.DAILY_LOCK_PATH):
        os.remove(config.DAILY_LOCK_PATH)
