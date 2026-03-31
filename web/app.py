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
from kalshi.api import get_todays_markets, get_account_balance, get_open_positions, place_baseball_order, get_portfolio_value
from weather.nws_forecast import (
    get_effective_forecast, get_current_temp, get_running_high, get_7day_forecast,
    get_forecast_high_for_city, get_7day_forecast_for_city,
    get_current_temp_for_city, get_running_high_for_city,
)
from predictor.probability import parse_brackets, assign_probabilities
from trader.edge import compute_signals
from trader.sizer import kelly_contracts, expected_value
from trader.daily_lock import get_lock, is_locked, bets_are_placed, lock_prediction, should_bet

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True
ET = ZoneInfo("America/New_York")

# ── Per-city in-memory cache ──────────────────────────────────────────────────
_cache: dict[str, dict] = {}   # keyed by city code e.g. "NYC", "HOU"
_cache_lock = threading.Lock()

def _city_cache(city: str) -> dict:
    if city not in _cache:
        _cache[city] = {"live": None, "backtest": None, "last_live": None, "last_bt": None}
    return _cache[city]


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


def _fetch_live(city_key: str = None):
    city_key = city_key or config.DEFAULT_CITY
    city     = config.CITIES[city_key]

    markets = get_todays_markets(city_key)
    if not markets:
        return {"error": f"No open markets found for {city_key} today.", "brackets": [], "signals": [], "city": city_key}

    existing_lock = get_lock(city_key)
    if existing_lock:
        forecast = existing_lock["forecast"]
        sigma    = existing_lock["sigma"]
    else:
        forecast = get_forecast_high_for_city(city)
        sigma    = config.SIGMA_MORNING
        lock_prediction(forecast, sigma, city_key)

    brackets      = parse_brackets(markets)
    brackets      = assign_probabilities(brackets, mu=forecast, sigma=sigma)
    running_high  = get_running_high_for_city(city)
    signals       = compute_signals(brackets, running_high=running_high)
    current_temp  = get_current_temp_for_city(city)
    forecast_7day = get_7day_forecast_for_city(city)
    bankroll      = get_account_balance() or config.BANKROLL

    now_et = datetime.now(ET)
    lock   = get_lock(city_key)

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
        "city":         city_key,
        "city_name":    city["name"],
        "timestamp":    now_et.strftime("%Y-%m-%d %H:%M:%S ET"),
        "current_temp": current_temp,
        "forecast":     round(forecast, 1),
        "sigma":        round(sigma, 1),
        "bankroll":     bankroll,
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
                "contracts":  kelly_contracts(s, bankroll),
                "risk":       round(kelly_contracts(s, bankroll) * s.mkt_price, 2),
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


def _refresh_live(city: str = None):
    city = city or config.DEFAULT_CITY
    try:
        data = _fetch_live(city)
        with _cache_lock:
            _city_cache(city)["live"]      = data
            _city_cache(city)["last_live"] = datetime.now(ET).isoformat()
    except Exception as e:
        with _cache_lock:
            _city_cache(city).setdefault("errors", []).append(str(e))


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
    from flask import request as req
    city = req.args.get("city", config.DEFAULT_CITY).upper()
    if city not in config.CITIES:
        city = config.DEFAULT_CITY
    with _cache_lock:
        data = _city_cache(city).get("live")
    if data is None:
        try:
            data = _fetch_live(city)
            with _cache_lock:
                _city_cache(city)["live"] = data
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
    from flask import request as req
    city = req.args.get("city", config.DEFAULT_CITY).upper()
    if city not in config.CITIES:
        city = config.DEFAULT_CITY
    # Clear cached data so next /api/live call fetches fresh
    with _cache_lock:
        _city_cache(city)["live"] = None
    t = threading.Thread(target=lambda: _refresh_live(city), daemon=True)
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


@app.route("/api/baseball/reset-starting-balance", methods=["POST"])
def api_reset_starting_balance():
    """Reset the stored starting_bankroll to the real Kalshi balance."""
    from baseball.bet_log import _load_meta, _save_meta
    balance = get_account_balance()
    if balance is None:
        return jsonify({"error": "Kalshi auth failed"}), 400
    meta = _load_meta()
    meta["starting_bankroll"] = balance
    _save_meta(meta)
    return jsonify({"ok": True, "starting_bankroll": balance})


@app.route("/api/auth/status")
def api_auth_status():
    """Quick auth diagnostic — tells you exactly what's missing."""
    import os, config
    from kalshi.api import _load_private_key, get_account_balance
    key = _load_private_key()
    balance = get_account_balance()
    return jsonify({
        "has_key_id":          bool(config.KALSHI_API_KEY_ID),
        "key_id_preview":      (config.KALSHI_API_KEY_ID or "")[:8] + "...",
        "has_private_key_env": bool(os.getenv("KALSHI_PRIVATE_KEY_CONTENTS")),
        "has_private_key_file":bool(config.KALSHI_PRIVATE_KEY_PATH and
                                    os.path.exists(config.KALSHI_PRIVATE_KEY_PATH or "")),
        "key_loaded":          key is not None,
        "balance":             balance,
        "auth_works":          balance is not None,
    })


@app.route("/api/baseball/debug")
def api_baseball_debug():
    try:
        from baseball.odds_api import get_mlb_games
        from baseball.kalshi_mlb import get_mlb_events, get_open_mlb_markets, discover_mlb_series
        from kalshi.api import _get

        odds_games    = get_mlb_games()
        series        = discover_mlb_series()
        raw_markets   = get_open_mlb_markets(series)
        kalshi_events = get_mlb_events()
        positions     = get_open_positions()
        balance       = get_account_balance()

        # Raw positions response for diagnostics
        try:
            raw_pos = _get("/portfolio/positions", params={"limit": 10}, auth=True)
        except Exception as pe:
            raw_pos = {"error": str(pe)}

        return jsonify({
            "odds_games_count":    len(odds_games),
            "odds_games":          [{"home": g["home"], "away": g["away"]} for g in odds_games[:5]],
            "kalshi_series_found": series,
            "kalshi_raw_markets":  len(raw_markets),
            "kalshi_sample":       raw_markets[:3] if raw_markets else [],
            "kalshi_events":       kalshi_events[:3],
            "open_positions":      positions,
            "raw_positions_api":   raw_pos,
            "account_balance":     balance,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/baseball")
def api_baseball():
    try:
        from baseball.odds_api import get_mlb_games
        from baseball.kalshi_mlb import get_mlb_events, match_to_odds
        from baseball.analyzer import analyze_all, analyze_game, minutes_to_first_pitch, LOCK_OUT_MIN
        from baseball.odds_api import get_odds_quota

        odds_games    = get_mlb_games()
        kalshi_events = get_mlb_events()
        matched       = match_to_odds(kalshi_events, odds_games)
        portfolio     = get_portfolio_value() or {}
        bankroll      = portfolio.get("cash") or get_account_balance() or config.BANKROLL
        portfolio_val = portfolio.get("portfolio") or bankroll
        positions     = get_open_positions()  # keyed by ticker

        # Build signal index keyed by game_id for quick lookup
        edge_signals = analyze_all(matched)  # only edge > threshold
        sig_by_game  = {}
        for s in edge_signals:
            key = (s.game_id, s.side)
            # Base Kelly contracts
            base_contracts = max(1, int(bankroll * s.kelly_frac / max(s.kalshi_prob, 0.01)))
            # Scale by edge magnitude: 4c=1x, 8c=2x, 16c+=3x (capped at 5x)
            edge_cents  = s.edge * 100
            edge_scale  = max(1, min(5, int(edge_cents / 4)))
            contracts   = base_contracts * edge_scale
            # Payout multiplier: how many dollars back per dollar risked if win
            roi_mult    = round(1.0 / max(s.kalshi_prob, 0.01), 2)
            sig_by_game[key] = {
                "team":        s.team,
                "side":        s.side,
                "ticker":      s.ticker,
                "vegas_prob":  round(s.vegas_prob * 100, 1),
                "kalshi_prob": round(s.kalshi_prob * 100, 1),
                "edge":        round(s.edge * 100, 1),
                "ev":          round(s.ev * 100, 2),
                "kelly_frac":  round(s.kelly_frac * 100, 1),
                "contracts":   contracts,
                "risk":        round(contracts * s.kalshi_prob, 2),
                "roi_mult":    roi_mult,
                "status":      s.status,
                "mins_to_game": s.minutes_to_game,
            }

        # All games sorted by first pitch (soonest first), includes pre-game + live
        games_out = []
        for g in sorted(matched, key=lambda x: x["commence"]):
            mins = minutes_to_first_pitch(g["commence"])
            kalshi = g["kalshi"]

            home_prob   = round(g["home_prob"] * 100, 1)
            away_prob   = round(g["away_prob"] * 100, 1)
            # Display: use home_yes/away_yes (complement-derived, matches Kalshi UI)
            home_kalshi = round(kalshi.get("home_yes", 0) * 100, 1)
            away_kalshi = round(kalshi.get("away_yes", 0) * 100, 1)
            # Edge: use actual ask prices (home_ask/away_ask) so edge is accurate
            home_edge   = round(home_prob - round(kalshi.get("home_ask", kalshi.get("home_yes", 0)) * 100, 1), 1)
            away_edge   = round(away_prob - round(kalshi.get("away_ask", kalshi.get("away_yes", 0)) * 100, 1), 1)

            home_ticker = kalshi.get("home_ticker", "")
            away_ticker = kalshi.get("away_ticker", "")

            # Attach open positions if any
            home_pos = positions.get(home_ticker) if home_ticker else None
            away_pos = positions.get(away_ticker) if away_ticker else None

            games_out.append({
                "id":           g["id"],
                "home":         g["home"],
                "away":         g["away"],
                "commence":     g["commence"].isoformat(),
                "mins_to_game": mins,
                "home_prob":    home_prob,
                "away_prob":    away_prob,
                "home_kalshi":  home_kalshi,
                "away_kalshi":  away_kalshi,
                "home_edge":    home_edge,
                "away_edge":    away_edge,
                "num_books":    g["num_books"],
                "home_ticker":  home_ticker,
                "away_ticker":  away_ticker,
                # Best signal for this game (if any)
                "home_signal":  sig_by_game.get((g["id"], "home")),
                "away_signal":  sig_by_game.get((g["id"], "away")),
                # Open Kalshi positions for this game (if any)
                "home_position": home_pos,
                "away_position": away_pos,
            })

        # MLB team abbreviation → full name mapping (used by position matching + sync)
        _MLB_ABBR = {
            "ARI": "Arizona Diamondbacks", "ATL": "Atlanta Braves",
            "BAL": "Baltimore Orioles", "BOS": "Boston Red Sox",
            "CHC": "Chicago Cubs", "CWS": "Chicago White Sox", "CHW": "Chicago White Sox",
            "CIN": "Cincinnati Reds", "CLE": "Cleveland Guardians",
            "COL": "Colorado Rockies", "DET": "Detroit Tigers",
            "HOU": "Houston Astros", "KC": "Kansas City Royals", "KCR": "Kansas City Royals",
            "LAA": "Los Angeles Angels", "LAD": "Los Angeles Dodgers",
            "MIA": "Miami Marlins", "MIL": "Milwaukee Brewers",
            "MIN": "Minnesota Twins", "NYM": "New York Mets", "NYY": "New York Yankees",
            "OAK": "Oakland Athletics", "PHI": "Philadelphia Phillies",
            "PIT": "Pittsburgh Pirates", "SD": "San Diego Padres", "SDP": "San Diego Padres",
            "SEA": "Seattle Mariners", "SF": "San Francisco Giants", "SFG": "San Francisco Giants",
            "STL": "St. Louis Cardinals", "TB": "Tampa Bay Rays", "TBR": "Tampa Bay Rays",
            "TEX": "Texas Rangers", "TOR": "Toronto Blue Jays",
            "WAS": "Washington Nationals", "WSH": "Washington Nationals", "WSN": "Washington Nationals",
        }

        # Build a lookup: for each position ticker, parse home/away team names
        # so we can attach positions to unmatched game cards
        def _match_pos_to_game(game_home, game_away, positions_dict):
            """Find positions matching a game by team name (last word match)."""
            home_pos = away_pos = None
            home_tk = away_tk = ""
            home_last = game_home.split()[-1].lower() if game_home else ""
            away_last = game_away.split()[-1].lower() if game_away else ""
            for tk, pos in positions_dict.items():
                if pos.get("quantity", 0) <= 0:
                    continue
                parts = tk.split("-")
                if len(parts) < 3:
                    continue
                team_abbr = parts[-1]
                team_full = _MLB_ABBR.get(team_abbr, "").lower()
                team_last = team_full.split()[-1] if team_full else ""
                if team_last == home_last:
                    home_pos = pos
                    home_tk = tk
                elif team_last == away_last:
                    away_pos = pos
                    away_tk = tk
            return home_pos, away_pos, home_tk, away_tk

        # Add unmatched Odds API games (no Kalshi market yet) so dashboard isn't blank
        matched_ids = {g["id"] for g in matched}
        for g in sorted(odds_games, key=lambda x: x["commence"]):
            if g["id"] in matched_ids:
                continue  # already in games_out from matched
            mins = minutes_to_first_pitch(g["commence"])
            # Try to find positions for this game even without Kalshi market match
            h_pos, a_pos, h_tk, a_tk = _match_pos_to_game(g["home"], g["away"], positions)
            games_out.append({
                "id":           g["id"],
                "home":         g["home"],
                "away":         g["away"],
                "commence":     g["commence"].isoformat(),
                "mins_to_game": mins,
                "home_prob":    round(g["home_prob"] * 100, 1),
                "away_prob":    round(g["away_prob"] * 100, 1),
                "home_kalshi":  0,
                "away_kalshi":  0,
                "home_edge":    0,
                "away_edge":    0,
                "num_books":    g.get("num_books", 0),
                "home_ticker":  h_tk,
                "away_ticker":  a_tk,
                "home_signal":  None,
                "away_signal":  None,
                "home_position": h_pos,
                "away_position": a_pos,
                "no_kalshi":    True,  # flag for frontend
            })

        def _parse_ticker_team(ticker):
            """Extract team name from Kalshi ticker like KXMLBGAME-26MAR312140NYYSEA-SEA"""
            parts = ticker.split("-")
            if len(parts) >= 3:
                abbr = parts[-1]
                return _MLB_ABBR.get(abbr, abbr)
            return ticker

        def _parse_ticker_matchup(ticker):
            """Extract home/away from ticker like KXMLBGAME-26MAR312140NYYSEA-SEA"""
            parts = ticker.split("-")
            if len(parts) >= 3:
                bet_team = parts[-1]
                mid = parts[1]
                teams_str = ""
                for i in range(len(mid)):
                    if mid[i:].isalpha():
                        teams_str = mid[i:]
                        break
                away_abbr, home_abbr = "", ""
                for split_pos in range(2, len(teams_str) - 1):
                    a = teams_str[:split_pos]
                    h = teams_str[split_pos:]
                    if a in _MLB_ABBR and h in _MLB_ABBR:
                        away_abbr, home_abbr = a, h
                        break
                away = _MLB_ABBR.get(away_abbr, away_abbr)
                home = _MLB_ABBR.get(home_abbr, home_abbr)
                side = "home" if bet_team == home_abbr else "away"
                return away, home, side
            return "", "", "away"

        # ── Sync bet log with live Kalshi positions ─────────────────────────────
        try:
            from baseball.bet_log import _load as _load_bets, _save as _save_bets, log_bet as _auto_log
            from datetime import timezone as _tz
            existing_bets = _load_bets()
            logged_tickers = {b.get("ticker") for b in existing_bets}
            open_tickers = {t for t, p in positions.items() if p.get("quantity", 0) > 0}
            dirty = False

            # 1. Auto-log positions not yet in bet log
            try:
                for ticker, pos in positions.items():
                    if pos.get("quantity", 0) <= 0:
                        continue
                    qty = pos["quantity"]
                    avg_cents = int(round(pos.get("avg_price", 50)))
                    if ticker in logged_tickers:
                        for b in existing_bets:
                            if b.get("ticker") == ticker and b.get("status") == "pending":
                                if b.get("price_cents") != avg_cents or b.get("contracts") != qty:
                                    b["price_cents"] = avg_cents
                                    b["contracts"] = qty
                                    b["cost"] = round(qty * avg_cents / 100, 2)
                                    dirty = True
                        continue
                    game_info = next((go for go in games_out
                        if go.get("home_ticker") == ticker or go.get("away_ticker") == ticker), None)
                    if game_info:
                        side_key = "home" if game_info.get("home_ticker") == ticker else "away"
                        home_name = game_info["home"]
                        away_name = game_info["away"]
                        team_name = home_name if side_key == "home" else away_name
                        game_id = game_info["id"]
                        game_date = game_info["commence"][:10] if game_info.get("commence") else ""
                    else:
                        away_name, home_name, side_key = _parse_ticker_matchup(ticker)
                        team_name = _parse_ticker_team(ticker)
                        game_id = ""
                        game_date = ""
                    _auto_log(
                        home=home_name, away=away_name, team=team_name,
                        side=side_key, ticker=ticker,
                        contracts=qty, price_cents=avg_cents,
                        vegas_prob=0, edge=0,
                        game_id=game_id, game_date=game_date,
                    )
                    logged_tickers.add(ticker)
            except Exception as e1:
                print(f"[sync] Step 1 auto-log error: {e1}")

            # 2. Auto-cancel: pending bets with no open position (never touch won/lost)
            try:
                for b in existing_bets:
                    if b.get("status") != "pending":
                        continue
                    ticker = b.get("ticker", "")
                    if ticker and ticker not in open_tickers:
                        b["status"] = "canceled"
                        b["result"] = "canceled"
                        b["pnl"] = 0
                        b["resolved_at"] = datetime.now(_tz.utc).isoformat()
                        dirty = True
            except Exception as e2:
                print(f"[sync] Step 2 auto-cancel error: {e2}")

            # 3. Fix corrupted team names by re-parsing ticker (always runs)
            try:
                for b in existing_bets:
                    t = b.get("ticker", "")
                    if not t or "KXMLBGAME" not in t:
                        continue
                    parsed_away, parsed_home, parsed_side = _parse_ticker_matchup(t)
                    parsed_team = _parse_ticker_team(t)
                    if parsed_home and b.get("home") != parsed_home:
                        b["home"] = parsed_home
                        b["away"] = parsed_away
                        b["team"] = parsed_team
                        b["team_bet_on"] = parsed_team
                        b["side"] = parsed_side
                        b["bet_side"] = parsed_side
                        b["teams"] = f"{parsed_away} @ {parsed_home}"
                        dirty = True
                        print(f"[sync] Fixed team names for {t}: {parsed_away} @ {parsed_home} → {parsed_team}")
            except Exception as e3:
                print(f"[sync] Step 3 fix names error: {e3}")

            # 4. Reset wrongly-resolved future bets back to pending
            try:
                from baseball.bet_log import _parse_game_date_from_ticker
                today_str = datetime.now(_tz.utc).strftime("%Y-%m-%d")
                for b in existing_bets:
                    if b.get("status") not in ("won", "lost"):
                        continue
                    t = b.get("ticker", "")
                    if not t:
                        continue
                    gd = _parse_game_date_from_ticker(t)
                    if gd and gd > today_str:
                        print(f"[sync] Resetting wrongly-resolved future bet: {t} (game {gd}, resolved as {b['status']})")
                        b["status"] = "pending"
                        b["result"] = "pending"
                        b["pnl"] = None
                        b["resolved_at"] = None
                        dirty = True
            except Exception as e4:
                print(f"[sync] Step 4 reset future bets error: {e4}")

            if dirty:
                _save_bets(existing_bets)
                # Reload so subsequent code sees fixed data
                existing_bets = _load_bets()
        except Exception as e:
            print(f"[sync] Bet log sync error: {e}")
            import traceback; traceback.print_exc()

        # Build positions summary from live Kalshi data (source of truth)
        # Always parse team names from ticker to avoid game-card mismatches
        live_positions = []
        for ticker, pos in positions.items():
            if pos.get("quantity", 0) <= 0:
                continue
            avg_cents = int(round(pos.get("avg_price", 0)))
            qty = pos["quantity"]

            # Always parse from ticker (source of truth for team names)
            away, home, side = _parse_ticker_matchup(ticker)
            team = _parse_ticker_team(ticker)

            # Try to get commence/mins from matching game
            game_info = next((go for go in games_out
                              if go.get("home_ticker") == ticker or go.get("away_ticker") == ticker), None)
            if game_info:
                commence = game_info.get("commence")
                mins = game_info.get("mins_to_game")
            else:
                # Parse date+time from ticker: KXMLBGAME-26MAR312140NYYSEA-SEA
                commence = None
                mins = None
                try:
                    mid = ticker.split("-")[1]
                    yr = int(mid[:2]) + 2000
                    mon_str = mid[2:5]
                    mon_map = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                               "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
                    mon = mon_map.get(mon_str, 1)
                    day = int(mid[5:7])
                    hhmm = mid[7:11]
                    hr, mn = int(hhmm[:2]), int(hhmm[2:])
                    from datetime import datetime, timezone
                    game_dt = datetime(yr, mon, day, hr, mn, tzinfo=timezone.utc)
                    commence = game_dt.isoformat()
                    import time as _time
                    mins = int((game_dt.timestamp() - _time.time()) / 60)
                except Exception:
                    pass

            live_positions.append({
                "ticker":    ticker,
                "quantity":  qty,
                "avg_price": avg_cents,
                "cost":      round(qty * avg_cents / 100, 2),
                "to_win":    round(qty * (100 - avg_cents) / 100, 2),
                "home":      home,
                "away":      away,
                "team":      team,
                "side":      side,
                "commence":  commence,
                "mins_to_game": mins,
            })

        # Last bot scan timestamp — always update on each API call since we're
        # fetching fresh data anyway. The worker also updates this independently.
        try:
            from main import get_last_scan_time, _record_scan_time
            _record_scan_time()  # always update — web server is scanning too
            last_scan = get_last_scan_time()
        except Exception:
            last_scan = datetime.now(ZoneInfo("UTC")).isoformat()

        quota = get_odds_quota()
        return jsonify({
            "games":          games_out,
            "positions":      live_positions,   # from Kalshi API (source of truth)
            "bankroll":       bankroll,
            "portfolio":      portfolio_val,     # cash + position market value
            "has_odds_key":   bool(os.getenv("ODDS_API_KEY")),
            "last_scan":      last_scan,
            "poll_interval":  config.POLL_INTERVAL_SECONDS,
            "scan_start_et":  config.BASEBALL_SCAN_START_ET,
            "scan_end_et":    config.BASEBALL_SCAN_END_ET,
            "odds_remaining": quota.get("remaining"),
            "odds_used":      quota.get("used"),
        })
    except Exception as e:
        return jsonify({"error": str(e), "games": [], "signals": []}), 500


@app.route("/api/baseball/log_bet", methods=["POST"])
def api_baseball_log_bet():
    from flask import request as req
    from baseball.bet_log import log_bet
    body = req.get_json()
    bet = log_bet(
        home        = body.get("home", ""),
        away        = body.get("away", ""),
        team        = body.get("team", ""),
        side        = body.get("side", ""),
        ticker      = body.get("ticker", ""),
        contracts   = int(body.get("contracts", 1)),
        price_cents = int(body.get("price_cents", 50)),
        vegas_prob  = float(body.get("vegas_prob", 0)),
        edge        = float(body.get("edge", 0)),
        game_id     = body.get("game_id", ""),
        game_date   = body.get("game_date", ""),
    )
    return jsonify({"status": "ok", "bet": bet})


@app.route("/api/baseball/bet", methods=["POST"])
def api_baseball_bet():
    """Place a baseball bet manually via the dashboard PLACE BET button."""
    from flask import request as req
    from baseball.bet_log import log_bet, _load as _load_bets
    try:
        body       = req.get_json()
        game_id    = body.get("game_id", "")
        side       = body.get("side", "")        # "home" or "away"
        contracts  = int(body.get("contracts", 1))
        price      = int(body.get("price", 50))  # cents
        team_bet_on = body.get("team_bet_on", "")

        # Duplicate check: don't place if we already have this game+side in the log
        existing = _load_bets()
        for b in existing:
            if b.get("game_id") == game_id and b.get("side") == side and b.get("status") == "pending":
                return jsonify({"success": True, "order_id": "already_placed", "note": "Duplicate prevented"})

        # Resolve the ticker and game metadata from matched games
        from baseball.odds_api import get_mlb_games
        from baseball.kalshi_mlb import get_mlb_events, match_to_odds

        odds_games    = get_mlb_games()
        kalshi_events = get_mlb_events()
        matched       = match_to_odds(kalshi_events, odds_games)

        game = next((g for g in matched if g["id"] == game_id), None)
        if not game:
            return jsonify({"error": f"Game not found: {game_id}"}), 404

        kalshi = game["kalshi"]
        ticker_key = "home_ticker" if side == "home" else "away_ticker"
        ticker = kalshi.get(ticker_key, "")
        if not ticker:
            return jsonify({"error": f"No Kalshi ticker found for {side} side of game {game_id}"}), 400

        # Place the order on Kalshi
        result = place_baseball_order(
            ticker      = ticker,
            side        = "yes",
            contracts   = contracts,
            price_cents = price,
        )

        order_id = (result.get("order") or {}).get("order_id") or result.get("status", "ok")

        # Log the bet
        from datetime import datetime, timezone
        game_date = game["commence"].strftime("%Y-%m-%d")
        vegas_prob = round(game["home_prob"] * 100 if side == "home" else game["away_prob"] * 100, 1)
        home_kalshi_ask = kalshi.get("home_ask", kalshi.get("home_yes", price / 100))
        away_kalshi_ask = kalshi.get("away_ask", kalshi.get("away_yes", price / 100))
        kalshi_ask = home_kalshi_ask if side == "home" else away_kalshi_ask
        edge = round(vegas_prob - kalshi_ask * 100, 1)

        log_bet(
            home        = game["home"],
            away        = game["away"],
            team        = team_bet_on or (game["home"] if side == "home" else game["away"]),
            side        = side,
            ticker      = ticker,
            contracts   = contracts,
            price_cents = price,
            vegas_prob  = vegas_prob,
            edge        = edge,
            game_id     = game_id,
            game_date   = game_date,
        )

        return jsonify({"success": True, "order_id": str(order_id)})

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/baseball/bets")
def api_baseball_bets():
    from baseball.bet_log import get_all_bets, pnl_summary, resolve_pending
    # Resolve any pending bets via MLB Stats API before returning
    resolve_pending()
    bets = get_all_bets()
    summary = pnl_summary(bets)
    return jsonify({"bets": list(reversed(bets)), "summary": summary})


@app.route("/api/baseball/bets/raw")
def api_baseball_bets_raw():
    """Raw bet log — for debugging. Shows all entries as-is."""
    from baseball.bet_log import _load
    return jsonify(_load())


@app.route("/api/baseball/bets/cleanup", methods=["POST"])
def api_baseball_bets_cleanup():
    """Remove duplicate and ghost bets. Keeps one entry per ticker."""
    from baseball.bet_log import _load, _save
    from kalshi.api import get_open_positions
    bets = _load()
    positions = get_open_positions()
    open_tickers = set(positions.keys()) if positions else set()

    seen_tickers = set()
    cleaned = []
    removed = []
    for b in bets:
        t = b.get("ticker", "")
        # Skip duplicates (keep first entry per ticker)
        if t and t in seen_tickers:
            removed.append({"ticker": t, "reason": "duplicate", "team": b.get("team")})
            continue
        # Skip ghost pending bets with no Kalshi position
        if b.get("status") == "pending" and t and t not in open_tickers:
            removed.append({"ticker": t, "reason": "no_position", "team": b.get("team")})
            continue
        if t:
            seen_tickers.add(t)
        cleaned.append(b)

    _save(cleaned)
    return jsonify({"kept": len(cleaned), "removed": len(removed), "removed_details": removed})


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
