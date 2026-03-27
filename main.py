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
from kalshi.api import get_todays_markets, place_order
from weather.nws_forecast import get_effective_forecast
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

    # 5. Signals
    signals = compute_signals(brackets)

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

    bankroll = config.BANKROLL
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
        return

    try:
        while True:
            bankroll = run_once(bankroll)
            time.sleep(config.POLL_INTERVAL_SECONDS)
    except KeyboardInterrupt:
        console.print("\n[yellow]Bot stopped.[/]")


if __name__ == "__main__":
    main()
