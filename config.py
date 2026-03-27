import os
from dotenv import load_dotenv

load_dotenv()

# ── Kalshi API ────────────────────────────────────────────────────────────────
KALSHI_BASE_URL       = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_API_KEY_ID     = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")

# ── Trading ───────────────────────────────────────────────────────────────────
DRY_RUN          = os.getenv("DRY_RUN", "true").lower() == "true"
BANKROLL         = float(os.getenv("BANKROLL", "100.0"))   # USD
MIN_EDGE         = float(os.getenv("MIN_EDGE", "0.07"))    # 7 cents minimum edge
KELLY_FRACTION   = 0.25                                    # fractional Kelly

# ── Market ────────────────────────────────────────────────────────────────────
SERIES_TICKER    = "KXHIGHNY"
CITY_LABEL       = "NYC"

# ── NWS ──────────────────────────────────────────────────────────────────────
NWS_FORECAST_URL = "https://api.weather.gov/gridpoints/OKX/34,38/forecast"
NWS_HOURLY_URL   = "https://api.weather.gov/gridpoints/OKX/34,38/forecast/hourly"
NWS_STATION      = "KNYC"
NWS_CLI_LOCATION = "NYC"

# ── Cities ────────────────────────────────────────────────────────────────────
CITIES = {
    "NYC": {
        "name":           "New York City",
        "label":          "NYC",
        "kalshi_series":  "KXHIGHNY",
        "nws_station":    "KNYC",
        "nws_office":     "OKX",
        "nws_grid_x":     34,
        "nws_grid_y":     38,
        "lat":            40.7589,
        "lon":           -73.9851,
    },
    "HOU": {
        "name":           "Houston",
        "label":          "HOU",
        "kalshi_series":  "KXHIGHTHOU",
        "nws_station":    "KHOU",
        "nws_office":     "HGX",
        "nws_grid_x":     66,
        "nws_grid_y":     97,
        "lat":            29.7604,
        "lon":           -95.3698,
    },
}
DEFAULT_CITY = os.getenv("DEFAULT_CITY", "NYC")

# ── Prediction model ──────────────────────────────────────────────────────────
# Sigma (°F) used when no live observations are available yet (early morning).
# Shrinks as the day progresses and actual readings come in.
SIGMA_MORNING    = 3.0   # before 10 AM
SIGMA_MIDDAY     = 2.0   # 10 AM – 2 PM
SIGMA_AFTERNOON  = 1.5   # 2 PM – 5 PM
SIGMA_EVENING    = 1.0   # after 5 PM

# ── Backtest ──────────────────────────────────────────────────────────────────
BACKTEST_DAYS           = 366
BACKTEST_SIMULATIONS    = 500   # Monte Carlo runs per day
BACKTEST_FORECAST_SIGMA = 2.5   # simulated NWS forecast error (°F)

# ── Scheduler ────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 1800   # 30 minutes
MARKET_OPEN_HOUR_ET   = 6
MARKET_CLOSE_HOUR_ET  = 20

# ── Daily bet lock ────────────────────────────────────────────────────────────
# Bets are placed ONCE per day at BET_HOUR_ET and then locked.
# After lock, the bot monitors only — no new orders until next day.
BET_HOUR_ET     = int(os.getenv("BET_HOUR_ET", "7"))  # 7 AM ET default
DAILY_LOCK_PATH = "data/daily_lock.json"

# ── Logging ───────────────────────────────────────────────────────────────────
TRADE_LOG_PATH = "data/trade_log.csv"
