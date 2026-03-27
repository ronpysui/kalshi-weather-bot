"""
Flask web server for the Kalshi NYC Temp Bot dashboard.
Run with: python web/app.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import math
import threading
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, jsonify, render_template

import config
from kalshi.api import get_todays_markets, get_account_balance
from weather.nws_forecast import get_effective_forecast, get_current_temp, get_running_high, get_7day_forecast
from predictor.probability import parse_brackets, assign_probabilities
from trader.edge import compute_signals
from trader.sizer import kelly_contracts, expected_value
from trader.daily_lock import get_lock, is_locked, bets_are_placed, lock_prediction, should_bet

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
ET = ZoneInfo("America/New_York")

# ── In-memory cache (refreshed every 5 min via background thread) ─────────────
_cache = {
    "live":      None,
    "backtest":  None,
    "last_live": None,
    "last_bt":   None,
    "errors":    [],
}
_cache_lock = threading.Lock()


def _inject_today(days: list, forecast: float) -> list:
    """
    If NWS no longer has a daytime period for today (evening), prepend it
    using the locked forecast so TODAY always appears in the 7-day strip.
    """
    from datetime import date as date_type
    today = datetime.now(ET).date().isoformat()
    if not any(d["date"] == today for d in days):
        days = [{
            "date":           today,
            "label":          "Today",
            "high":           forecast,
            "short_forecast": "locked forecast",
            "is_today":       True,
            "is_tomorrow":    False,
        }] + days
    return days[:7]


def _temp_in_bracket(label: str, temp: float) -> bool:
    """Return True if temp falls within the bracket described by label."""
    s = label.lower().replace('°', '').strip()
    if 'or above' in s:
        lower = float(s.split('or')[0].strip())
        return temp >= lower
    if 'or below' in s:
        upper = float(s.split('or')[0].strip())
        return temp <= upper
    if ' to ' in s:
        parts = s.split(' to ')
        return float(parts[0].strip()) <= temp <= float(parts[1].strip()) + 0.9
    return False


def _fetch_live():
    markets = get_todays_markets()
    if not markets:
        return {"error": "No open markets found for today.", "brackets": [], "signals": []}

    # ── Use locked forecast if we already have one for today ─────────────────
    # This ensures bracket probabilities are frozen at market-open values and
    # don't drift throughout the day as NWS updates its forecast.
    existing_lock = get_lock()
    if existing_lock:
        forecast = existing_lock["forecast"]
        sigma    = existing_lock["sigma"]
    else:
        # First call of the day — fetch NWS forecast and lock it immediately.
        # Subsequent calls will use this locked value regardless of NWS updates.
        forecast, _ = get_effective_forecast()
        sigma = config.SIGMA_MORNING
        lock_prediction(forecast, sigma)

    brackets     = parse_brackets(markets)
    brackets     = assign_probabilities(brackets, mu=forecast, sigma=sigma)
    running_high  = get_running_high()
    signals       = compute_signals(brackets, running_high=running_high)
    current_temp  = get_current_temp()
    forecast_7day = get_7day_forecast()

    now_et   = datetime.now(ET)
    lock     = get_lock()

    # Seconds until next bet window (BET_HOUR_ET AM tomorrow if already past today's)
    from datetime import timedelta
    now_naive     = now_et.replace(tzinfo=None)
    bet_today     = now_naive.replace(hour=config.BET_HOUR_ET, minute=0, second=0, microsecond=0)
    if now_naive >= bet_today:
        bet_next  = bet_today + timedelta(days=1)
    else:
        bet_next  = bet_today
    secs_to_bet   = int((bet_next - now_naive).total_seconds())

    return {
        "timestamp":    now_et.strftime("%Y-%m-%d %H:%M:%S ET"),
        "current_temp": current_temp,
        "forecast":     round(forecast, 1),
        "sigma":        round(sigma, 1),
        "bankroll":     get_account_balance() or config.BANKROLL,
        "dry_run":      config.DRY_RUN,
        "min_edge":     config.MIN_EDGE,
        "bet_hour_et":  config.BET_HOUR_ET,
        "secs_to_bet":  secs_to_bet,
        "forecast_7day":     _inject_today(forecast_7day, forecast),
        "prediction_locked": lock is not None,
        "bets_placed":       lock is not None and lock.get("bets_placed", False),
        "locked":            lock is not None,   # kept for backward compat
        "lock":              lock,
        "brackets": [
            {
                "label":      b.label,
                "ticker":     b.ticker,
                "our_prob":   round(b.our_prob * 100, 1),
                "mkt_yes":    round(b.market_yes_price * 100, 1),
                "mkt_no":     round(b.market_no_price  * 100, 1),
                "yes_edge":   round((b.our_prob - b.market_yes_price) * 100, 1),
                "no_edge":    round(((1 - b.our_prob) - b.market_no_price) * 100, 1),
            }
            for b in brackets
        ],
        "signals": [
            {
                "label":      s.label,
                "ticker":     s.ticker,
                "side":       s.side,
                "our_prob":   round(s.our_prob * 100, 1),
                "mkt_price":  round(s.mkt_price * 100, 1),
                "edge":       round(s.edge * 100, 1),
                "ev":         round(expected_value(s) * 100, 2),
                "contracts":  kelly_contracts(s, config.BANKROLL),
                "risk":       round(kelly_contracts(s, config.BANKROLL) * s.mkt_price, 2),
                "is_winning": (
                    _temp_in_bracket(s.label, current_temp) if s.side == "yes"
                    else not _temp_in_bracket(s.label, current_temp)
                ) if current_temp is not None else None,
            }
            for s in signals
        ],
    }


def _fetch_backtest():
    from backtest.backtest import run_backtest
    result = run_backtest(days=config.BACKTEST_DAYS)

    days_data = []
    for d in result.days:
        days_data.append({
            "date":            str(d.date),
            "actual":          d.actual_high,
            "forecast":        d.forecast,
            "forecast_src":    d.forecast_src,
            "sigma":           d.sigma,
            "correct_bracket": d.correct_bracket or "--",
            "top_bracket":     d.top_bracket or "--",
            "correct":         d.correct,
            "brackets": [
                {
                    "label":  s.label,
                    "prob":   s.prob,
                    "result": s.result,
                    "is_top": s.is_top,
                    "is_win": s.is_win,
                }
                for s in d.brackets
            ],
        })

    return {
        "summary": {
            "wins":       result.wins,
            "losses":     result.losses,
            "win_rate":   round(result.win_rate * 100, 1),
            "total_days": result.total,
        },
        "days": days_data,
    }


def _refresh_live():
    try:
        data = _fetch_live()
        with _cache_lock:
            _cache["live"]      = data
            _cache["last_live"] = datetime.now(ET).isoformat()
    except Exception as e:
        with _cache_lock:
            _cache["errors"].append(str(e))


def _refresh_backtest():
    try:
        data = _fetch_backtest()
        with _cache_lock:
            _cache["backtest"] = data
            _cache["last_bt"]  = datetime.now(ET).isoformat()
    except Exception as e:
        with _cache_lock:
            _cache["errors"].append(str(e))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/live")
def api_live():
    with _cache_lock:
        data = _cache["live"]
    if data is None:
        try:
            data = _fetch_live()
            with _cache_lock:
                _cache["live"] = data
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify(data)


@app.route("/api/backtest")
def api_backtest():
    with _cache_lock:
        data = _cache["backtest"]
    if data is None:
        try:
            data = _fetch_backtest()
            with _cache_lock:
                _cache["backtest"] = data
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify(data)


@app.route("/api/refresh/live")
def api_refresh_live():
    t = threading.Thread(target=_refresh_live, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})


@app.route("/api/refresh/backtest")
def api_refresh_backtest():
    t = threading.Thread(target=_refresh_backtest, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})


@app.route("/api/place_order", methods=["POST"])
def api_place_order():
    from flask import request as flask_request
    import csv, os
    body = flask_request.get_json()
    ticker      = body.get("ticker", "")
    side        = body.get("side", "yes")
    contracts   = int(body.get("contracts", 1))
    price_cents = int(body.get("price_cents", 50))

    result = place_order(
        ticker=ticker,
        side=side,
        count=contracts,
        price_cents=price_cents,
    )

    # Log to CSV regardless of dry run
    os.makedirs("data", exist_ok=True)
    log_path = config.TRADE_LOG_PATH
    write_header = not os.path.exists(log_path)
    with open(log_path, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "timestamp", "ticker", "side", "contracts",
                "price_cents", "edge_cents", "our_prob", "dry_run",
            ])
        writer.writerow([
            datetime.now(ET).isoformat(),
            ticker, side, contracts,
            price_cents, "--", "--", config.DRY_RUN,
        ])

    return jsonify({"status": result.get("status", "ok")})


@app.route("/api/tradelog")
def api_tradelog():
    import csv
    rows = []
    path = config.TRADE_LOG_PATH
    if os.path.exists(path):
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
    return jsonify(rows)


if __name__ == "__main__":
    print("Starting Kalshi NYC Temp Bot dashboard at http://localhost:5000")
    app.run(debug=False, port=5000, use_reloader=False)
