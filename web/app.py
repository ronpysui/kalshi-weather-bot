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
from kalshi.api import get_todays_markets
from weather.nws_forecast import get_effective_forecast
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


def _fetch_live():
    markets = get_todays_markets()
    if not markets:
        return {"error": "No open markets found for today.", "brackets": [], "signals": []}

    forecast, _ = get_effective_forecast()
    # Always use SIGMA_MORNING — prediction is fixed at bet time, not dynamic
    sigma = config.SIGMA_MORNING

    brackets = parse_brackets(markets)
    brackets = assign_probabilities(brackets, mu=forecast, sigma=sigma)
    signals  = compute_signals(brackets)

    # Lock prediction on first daily fetch — fixes forecast+sigma for the whole day.
    # Orders fire separately at BET_HOUR_ET via main.py.
    lock_prediction(forecast, sigma)

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
        "forecast":     round(forecast, 1),
        "sigma":        round(sigma, 1),
        "bankroll":     config.BANKROLL,
        "dry_run":      config.DRY_RUN,
        "min_edge":     config.MIN_EDGE,
        "bet_hour_et":  config.BET_HOUR_ET,
        "secs_to_bet":  secs_to_bet,
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
                "label":     s.label,
                "ticker":    s.ticker,
                "side":      s.side,
                "our_prob":  round(s.our_prob * 100, 1),
                "mkt_price": round(s.mkt_price * 100, 1),
                "edge":      round(s.edge * 100, 1),
                "ev":        round(expected_value(s) * 100, 2),
                "contracts": kelly_contracts(s, config.BANKROLL),
                "risk":      round(kelly_contracts(s, config.BANKROLL) * s.mkt_price, 2),
            }
            for s in signals
        ],
    }


def _fetch_backtest():
    from backtest.backtest import run_backtest
    result = run_backtest(days=config.BACKTEST_DAYS)

    days_data = []
    cumulative = 0.0
    for d in reversed(result.days):
        cumulative += d.pnl
        days_data.append({
            "date":           str(d.date),
            "actual":         d.actual_high,
            "forecast":       round(d.avg_forecast, 1),
            "seasonal":       d.seasonal_center,
            "correct_bracket":d.correct_bracket or "--",
            "model_prob":     round(d.model_prob_correct * 100, 1),
            "bets":           d.bets_placed,
            "won":            d.won,
            "lost":           d.lost,
            "no_bet":         d.no_bet,
            "pnl":            round(d.pnl, 2),
            "cumulative":     round(cumulative, 2),
        })

    return {
        "summary": {
            "total_pnl":    round(result.total_pnl, 2),
            "avg_daily":    round(result.avg_daily_pnl, 2),
            "win_rate":     round(result.win_rate * 100, 1),
            "wins":         sum(1 for d in result.days if d.pnl > 0),
            "losses":       sum(1 for d in result.days if d.pnl < 0),
            "ties":         sum(1 for d in result.days if d.pnl == 0),
            "roi":          round(result.roi * 100, 1),
            "total_risked": round(result.total_risked, 2),
            "sharpe":       round(result.sharpe, 2),
            "accuracy":     round(result.accuracy * 100, 1),
            "brier":        round(result.avg_brier, 3),
            "betting_days": result.betting_days,
            "no_bet_days":  result.no_bet_days,
            "total_days":   len(result.days),
            "best_day":     str(result.best_day.date),
            "best_pnl":     round(result.best_day.pnl, 2),
            "worst_day":    str(result.worst_day.date),
            "worst_pnl":    round(result.worst_day.pnl, 2),
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
