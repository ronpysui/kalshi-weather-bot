"""
Fetch NWS CLI (Climatological) reports and KNYC historical observations.

Used by the backtest to retrieve actual observed highs for past dates.
"""

import re
from datetime import datetime, date, timedelta, timezone
from typing import Optional

import requests

import config

HEADERS = {"User-Agent": "kalshi-nyc-temp-bot/1.0 (contact: user@example.com)"}


def _get(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── CLI reports ───────────────────────────────────────────────────────────────

def _get_cli_product_list() -> list[dict]:
    """Return recent CLI product metadata for NYC."""
    url = f"https://api.weather.gov/products/types/CLI/locations/{config.NWS_CLI_LOCATION}"
    data = _get(url)
    return data.get("@graph", [])


def _parse_cli_max(text: str) -> Optional[float]:
    """Extract the observed high from CLI product text."""
    match = re.search(r"MAXIMUM\s+(\d+)", text)
    if match:
        return float(match.group(1))
    return None


def get_cli_high_for_date(target_date: date) -> Optional[float]:
    """
    Return the official observed high (°F) for `target_date` from NWS CLI.
    Only works for dates within the past ~7 days (NWS retention limit).
    """
    products = _get_cli_product_list()
    target_str = target_date.strftime("%B %d %Y").upper()   # e.g. "MARCH 25 2026"
    # Strip leading zero from day
    target_str = re.sub(r"\s0(\d)\s", r" \1 ", target_str)

    for product in products:
        prod_data = _get(f"https://api.weather.gov/products/{product['id']}")
        text = prod_data.get("productText", "")
        if target_str in text.upper():
            val = _parse_cli_max(text)
            if val is not None:
                return val
    return None


# ── Historical observations (KNYC) ────────────────────────────────────────────

def get_observed_high_for_date(target_date: date) -> Optional[float]:
    """
    Return the observed daily high (°F) for `target_date` from KNYC hourly obs.
    Fetches all hourly observations and takes the max.
    """
    start = datetime(target_date.year, target_date.month, target_date.day,
                     tzinfo=timezone.utc)
    end   = start + timedelta(days=1)

    url = (
        f"https://api.weather.gov/stations/{config.NWS_STATION}/observations"
        f"?start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        f"&limit=100"
    )
    try:
        data = _get(url)
        features = data.get("features", [])
        temps_f = []
        for f in features:
            val = f["properties"]["temperature"]["value"]
            if val is not None:
                temps_f.append(val * 9 / 5 + 32)
        return round(max(temps_f)) if temps_f else None
    except Exception:
        return None


def get_historical_highs(days: int = 30) -> dict[date, float]:
    """
    Return a dict mapping date → observed high (°F) for the past `days` days.
    Prefers Kalshi's expiration_value (set by caller); this is a fallback.
    """
    result = {}
    today = date.today()
    for i in range(1, days + 1):
        target = today - timedelta(days=i)
        high = get_observed_high_for_date(target)
        if high is not None:
            result[target] = high
    return result
