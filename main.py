"""
Kalshi NYC Daily Temperature Bot
=================================

Usage
-----
  python main.py              # run live bot (polls every 30 min)
  python main.py --backtest   # run 30-day backtest and exit
  python main.py --once       # single live pass and exit (no loop)

Environment variables (set in .env or shell)
--------------------------------------------
  KALSHI_API_KEY_ID        API key ID (UUID) from Kalshi account
  KALSHI_PRIVATE_KEY_PATH  Path to your .key PEM file
  DRY_RUN                  true (default) / false
  BANKROLL                 Starting bankroll in USD (default 100)
  MIN_EDGE                 Minimum edge in dollars to trigger a bet (default 0.07)
  BET_HOUR_ET              Hour (ET) to place bets each morning (default 7)
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from rich.console import Console

import config
from kalshi.api import get_todays_markets, place_order, get_account_balance
from weather.nws_forecast import get_effective_forecast, get_running_high
from predictor.probability import parse_brackets, assign_probabilities
from trader.edge import compute_signals
from trader.sizer import kelly_contracts
from trader.daily_lock import already_bet, record_bet, is_locked, get_lock, lock_prediction, in_bet_window
from display.dashboard import render_live, render_backtest, console

ET = ZoneInfo("America/New_York")


# ── Trade logger ───────────────────────────────────────────────────────────────

def _ensure_log():
    os.makedirs("data", exist_ok=True)
    if not os.path.exists(config.TRADE_LOG_PATH):
        with open(config.TRADE_LOG_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "ticker", "side", "contracts",
                "price_cents", "edge_cents", "our_prob", "dry_run",
            ])


def _log_trade(ticker, side, contracts, price, edge, our_prob):
    _ensure_log()
    with open(config.TRADE_LOG_PATH, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now(ET).isoformat(),
            ticker, side, contracts,
            round(price * 100), round(edge * 100, 2),
            round(our_prob, 4), config.DRY_RUN,
        ])


# ── Single live pass ───────────────────────────────────────────────────────────

def run_once(bankroll: float) -> float:
    """
    Fetch latest data and render dashboard.
    Places orders only during the morning bet window (BET_HOUR_ET) and only
    if today's lock has not already been written.
    """
    hour_et = datetime.now(ET).hour

    # 1. Market hours guard
    if hour_et < config.MARKET_OPEN_HOUR_ET or hour_et >= config.MARKET_CLOSE_HOUR_ET:
        console.print(
            f"[dim]Outside market hours "
            f"({config.MARKET_OPEN_HOUR_ET}am–{config.MARKET_CLOSE_HOUR_ET % 12}pm ET). "
            f"Sleeping.[/]"
        )
        return bankroll

    # 2. Fetch markets
    markets = get_todays_markets()
    if not markets:
        console.print("[yellow]No open markets for today.[/]")
        return bankroll

    # 3. Use locked forecast if available — probabilities frozen at market open.
    #    Only fetch fresh NWS on the first call of the day (locks immediately).
    existing_lock = get_lock()
    if existing_lock:
        forecast = existing_lock["forecast"]
        sigma    = existing_lock["sigma"]
    else:
        forecast, _ = get_effective_forecast()
        sigma = config.SIGMA_MORNING
        lock_prediction(forecast, sigma)
        console.print(f"[green]Prediction locked at market open: {forecast}°F σ={sigma}°F[/]")

    # 4. Probability model
    brackets = parse_brackets(markets)
    brackets = assign_probabilities(brackets, mu=forecast, sigma=sigma)

    # 5. Signals — filter out brackets already ruled out by today's running high
    running_high = get_running_high()
    signals = compute_signals(brackets, running_high=running_high)

    # 6. Render
    render_live(brackets, signals, forecast, sigma, bankroll)

    # 7. Place orders opportunistically — fire each bracket the moment edge appears.
    #    Each bracket ticker can only be bet ONCE per day.
    if in_bet_window():
        for sig in signals:
            if already_bet(sig.ticker):
                continue  # already placed for this bracket today

            contracts = kelly_contracts(sig, bankroll)
            if contracts <= 0:
                continue

            price_cents = round(sig.mkt_price * 100)
            place_order(
                ticker=sig.ticker,
                side=sig.side,
                count=contracts,
                price_cents=price_cents,
            )
            _log_trade(
                ticker=sig.ticker,
                side=sig.side,
                contracts=contracts,
                price=sig.mkt_price,
                edge=sig.edge,
                our_prob=sig.our_prob,
            )
            bet = {
                "ticker":    sig.ticker,
                "side":      sig.side,
                "contracts": contracts,
                "price":     price_cents,
                "edge":      round(sig.edge * 100, 1),
                "our_prob":  round(sig.our_prob * 100, 1),
                "label":     sig.label,
            }
            record_bet(bet)
            console.print(
                f"[green]Bet placed: {sig.label} {sig.side.upper()} "
                f"{contracts} contracts @ {price_cents}c (edge +{round(sig.edge*100,1)}c)[/]"
            )
    else:
        console.print("[dim]Outside market hours — monitoring only.[/]")

    return bankroll


# ── Baseball worker ────────────────────────────────────────────────────────────

def run_baseball_once(bankroll: float) -> None:
    """
    Scan today's MLB games for edges and auto-place bets.

    Logic:
    - Runs every poll cycle alongside the temp bot.
    - For each upcoming game, checks if Vegas consensus vs Kalshi implied edge > 4¢.
    - If edge found AND game hasn't started AND ticker not already bet → place + log.
    - If no edge found now → does nothing. Will check again next poll (every 30 min).
    - Hard lock: stops betting 5 min before first pitch (game odds become stale).
    """
    if not os.getenv("ODDS_API_KEY"):
        return  # silently skip if no API key configured

    try:
        from baseball.odds_api    import get_mlb_games
        from baseball.kalshi_mlb  import get_mlb_events, match_to_odds
        from baseball.analyzer    import analyze_all
        from baseball.bet_log     import log_bet
        from kalshi.api           import place_order

        odds_games    = get_mlb_games()
        kalshi_events = get_mlb_events()
        matched       = match_to_odds(kalshi_events, odds_games)
        signals       = analyze_all(matched)

        if not signals:
            return

        # Load already-bet tickers for today (reuse daily_lock infra)
        from trader.daily_lock import already_bet as _already_bet, record_bet as _record_bet

        for sig in signals:
            bb_key = f"bb:{sig.ticker}"   # prefix to avoid collisions with temp bot
            if _already_bet(bb_key):
                continue  # already placed this game/side today

            contracts   = max(1, int(bankroll * sig.kelly_frac / max(sig.kalshi_prob, 0.01)))
            price_cents = round(sig.kalshi_prob * 100)

            place_order(
                ticker      = sig.ticker,
                side        = "yes",
                count       = contracts,
                price_cents = price_cents,
            )

            # Log to baseball bet tracker (shows in dashboard)
            log_bet(
                home        = sig.home,
                away        = sig.away,
                team        = sig.team,
                side        = sig.side,
                ticker      = sig.ticker,
                contracts   = contracts,
                price_cents = price_cents,
                vegas_prob  = round(sig.vegas_prob * 100, 1),
                edge        = round(sig.edge * 100, 1),
            )

            # Guard so we don't bet the same ticker again today
            _record_bet({"ticker": bb_key, "side": "yes", "contracts": contracts,
                         "price": price_cents, "edge": round(sig.edge * 100, 1),
                         "our_prob": round(sig.vegas_prob * 100, 1), "label": sig.team})

            console.print(
                f"[cyan]Baseball bet: {sig.team} ({sig.side}) "
                f"{contracts}x @ {price_cents}c  edge=+{round(sig.edge*100,1)}c  "
                f"({sig.mins_to_game}m to pitch)[/]"
            )

    except Exception as e:
        console.print(f"[yellow]Baseball worker error: {e}[/]")


# ── Backtest ───────────────────────────────────────────────────────────────────

def run_backtest():
    from backtest.backtest import run_backtest as _run
    result = _run(days=config.BACKTEST_DAYS)
    render_backtest(result)


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Kalshi NYC Temp Bot")
    parser.add_argument("--backtest", action="store_true",
                        help="Run 30-day backtest and exit")
    parser.add_argument("--once", action="store_true",
                        help="Run a single live pass and exit")
    args = parser.parse_args()

    if args.backtest:
        run_backtest()
        return

    # Use live Kalshi balance if authenticated, fall back to config
    live_balance = get_account_balance()
    bankroll = live_balance if live_balance is not None else config.BANKROLL
    _ensure_log()

    console.print(
        f"[bold cyan]Kalshi NYC Temp Bot started[/]  "
        f"DRY_RUN={config.DRY_RUN}  Bankroll=${bankroll:.2f}  "
        f"BetWindow={config.BET_HOUR_ET}:00 AM ET  "
        f"Poll={config.POLL_INTERVAL_SECONDS // 60}m  "
        f"MinEdge={config.MIN_EDGE * 100:.0f}c"
    )

    if args.once:
        run_once(bankroll)
        run_baseball_once(bankroll)
        return

    try:
        while True:
            bankroll = run_once(bankroll)
            run_baseball_once(bankroll)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        console.print("\n[yellow]Bot stopped.[/]")


if __name__ == "__main__":
    main()
