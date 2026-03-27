"""
lock_morning.py — Run at market open (6 AM ET) via Windows Task Scheduler.

Fetches the NWS morning forecast and locks today's prediction immediately.
The Flask dashboard will use this locked forecast all day instead of
re-fetching NWS on every load.

Usage:
    python lock_morning.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime
from zoneinfo import ZoneInfo

import config
from weather.nws_forecast import get_effective_forecast
from trader.daily_lock import is_prediction_locked, lock_prediction

ET = ZoneInfo("America/New_York")


def main():
    now = datetime.now(ET)
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S ET')}] Morning lock starting...")

    if is_prediction_locked():
        print("Already locked for today — nothing to do.")
        return

    forecast, source = get_effective_forecast()
    sigma = config.SIGMA_MORNING

    lock_prediction(forecast, sigma)

    print(f"Locked: forecast={forecast}°F  sigma={sigma}°F  source={source}")
    print(f"Prediction fixed for the day. Bets will fire at {config.BET_HOUR_ET}:00 AM ET.")


if __name__ == "__main__":
    main()
