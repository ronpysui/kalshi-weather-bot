"""
Fetch today's NWS forecast and live KNYC observations.

Key outputs
-----------
get_forecast_high()      → predicted daily high (°F) from NWS gridpoint forecast
get_running_high()       → highest temp observed at KNYC so far today (°F)
get_current_sigma()      → calibrated forecast uncertainty (°F) based on time of day
"""

from datetime import datetime, timezone, date
from zoneinfo import ZoneInfo

import requests

import config

ET = ZoneInfo("America/New_York")
HEADERS = {"User-Agent": "kalshi-nyc-temp-bot/1.0 (contact: user@example.com)"}


def _get(url: str) -> dict:
    resp = requests.get(url, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── Forecast high ─────────────────────────────────────────────────────────────

def get_forecast_high() -> float:
    """
    Return NWS predicted high for today (°F).
    Uses the daily forecast; period[0] = "Today" when called before midnight ET.
    Falls back to the maximum of today's hourly forecasts.
    """
    try:
        data = _get(config.NWS_FORECAST_URL)
        periods = data["properties"]["periods"]
        today_et = datetime.now(ET).date()

        # Find the "Today" or daytime period that matches today's date
        for p in periods:
            start = datetime.fromisoformat(p["startTime"]).astimezone(ET)
            if start.date() == today_et and p["isDaytime"]:
                return float(p["temperature"])

        # Fallback: max of hourly forecast for today
        return _hourly_max_today()

    except Exception:
        return _hourly_max_today()


def _hourly_max_today() -> float:
    data = _get(config.NWS_HOURLY_URL)
    periods = data["properties"]["periods"]
    today_et = datetime.now(ET).date()
    temps = [
        p["temperature"]
        for p in periods
        if datetime.fromisoformat(p["startTime"]).astimezone(ET).date() == today_et
    ]
    if not temps:
        raise ValueError("No hourly forecast periods found for today.")
    return float(max(temps))


# ── Running high (live observations) ─────────────────────────────────────────

def get_running_high() -> float | None:
    """
    Return the highest temperature observed at KNYC so far today (°F).
    Returns None if no observations yet.
    """
    now = datetime.now(timezone.utc)
    today_midnight = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    start = today_midnight.strftime("%Y-%m-%dT%H:%M:%SZ")
    end   = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (
        f"https://api.weather.gov/stations/{config.NWS_STATION}/observations"
        f"?start={start}&end={end}&limit=50"
    )
    try:
        data = _get(url)
        features = data.get("features", [])
        temps_f = []
        for f in features:
            val = f["properties"]["temperature"]["value"]
            if val is not None:
                temps_f.append(val * 9 / 5 + 32)
        return max(temps_f) if temps_f else None
    except Exception:
        return None


# ── Historical forecast (Open-Meteo) ─────────────────────────────────────────

def get_historical_forecasts(start: date, end: date) -> dict[str, float]:
    """
    Return {date_str: predicted_high_F} for each date in [start, end].
    Uses Open-Meteo's historical forecast API — returns what the model
    predicted for each day, not the observed actuals.
    Free, no API key needed.
    """
    url = (
        "https://historical-forecast-api.open-meteo.com/v1/forecast"
        f"?latitude=40.7589&longitude=-73.9851"
        f"&start_date={start}&end_date={end}"
        f"&daily=temperature_2m_max"
        f"&temperature_unit=fahrenheit"
        f"&timezone=America%2FNew_York"
    )
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        dates  = data["daily"]["time"]
        highs  = data["daily"]["temperature_2m_max"]
        return {d: round(float(h), 1) for d, h in zip(dates, highs) if h is not None}
    except Exception:
        return {}


# ── Current observed temperature ─────────────────────────────────────────────

def get_current_temp() -> float | None:
    """
    Return the most recent observed temperature at KNYC (°F).
    This is the live reading, not the forecast or running high.
    """
    url = f"https://api.weather.gov/stations/{config.NWS_STATION}/observations/latest"
    try:
        data = _get(url)
        val = data["properties"]["temperature"]["value"]
        if val is None:
            return None
        return round(val * 9 / 5 + 32, 1)
    except Exception:
        return None


# ── Calibrated sigma ──────────────────────────────────────────────────────────

def get_current_sigma(running_high: float | None, forecast_high: float) -> float:
    """
    Return forecast uncertainty (σ, °F) calibrated to time of day.

    If a running high is above the forecast, the effective uncertainty
    collapses because the temperature can only go up or stay.
    """
    hour_et = datetime.now(ET).hour

    if hour_et < 10:
        base_sigma = config.SIGMA_MORNING
    elif hour_et < 14:
        base_sigma = config.SIGMA_MIDDAY
    elif hour_et < 17:
        base_sigma = config.SIGMA_AFTERNOON
    else:
        base_sigma = config.SIGMA_EVENING

    # If we've already observed a high, uncertainty about the final high shrinks
    if running_high is not None and running_high >= forecast_high:
        # We know it's at least running_high; only upside uncertainty remains
        return base_sigma * 0.5

    return base_sigma


# ── Effective forecast ────────────────────────────────────────────────────────

def get_effective_forecast() -> tuple[float, float]:
    """
    Return (effective_high, sigma) for use in the probability model.

    effective_high = max(NWS forecast, running observed high)
    sigma          = calibrated uncertainty
    """
    forecast = get_forecast_high()
    running  = get_running_high()

    effective = max(forecast, running) if running is not None else forecast
    sigma     = get_current_sigma(running, forecast)

    return effective, sigma
