"""
30-day backtest engine.

Strategy
--------
For each settled day (actual high available from Kalshi expiration_value):

  1. MARKET PRICE (synthetic baseline):
     We cannot reliably retrieve the MORNING price from Kalshi without
     authenticated candlestick data. Instead we use a seasonal climatology
     distribution — the "dumb market" that only knows monthly norms.

     NYC monthly high-temp norms (°F):
       Jan=38±9  Feb=42±9  Mar=52±8  Apr=62±7
       May=72±6  Jun=81±6  Jul=86±5  Aug=84±5
       Sep=76±6  Oct=65±7  Nov=53±8  Dec=42±9

     This represents a trader with no weather model, only seasonality.
     Our NWS model (even with noise) should reliably beat it.

  2. OUR MODEL PRICE:
     simulated_forecast = actual_high + N(0, BACKTEST_FORECAST_SIGMA)
     This mimics realistic NWS morning forecast error (~2-3°F).
     Sigma at time of bet = SIGMA_MORNING (most conservative).

  3. EDGE & SIZING:
     edge = our_prob - synthetic_market_price  (per bracket, Yes/No)
     Kelly sizing with KELLY_FRACTION fractional.

  4. OUTCOME:
     Win if Kalshi result == our bet side.

  This shows how much value our calibrated NWS model adds over pure
  seasonality. Real live edge vs actual market participants will be smaller
  (markets already incorporate weather model data).
"""

import random
import math
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from scipy.stats import norm as scipy_norm

from kalshi.api import get_settled_events
from predictor.probability import parse_brackets, assign_probabilities, Bracket
from trader.edge import compute_signals
from trader.sizer import kelly_contracts, expected_value
import config


# ── NYC Seasonal Climatology ──────────────────────────────────────────────────
# (monthly avg high °F, std dev °F) — Central Park historical norms
NYC_NORMS: dict[int, tuple[float, float]] = {
    1:  (38.0, 9.0),
    2:  (42.0, 9.0),
    3:  (52.0, 8.0),
    4:  (62.0, 7.0),
    5:  (72.0, 6.0),
    6:  (81.0, 6.0),
    7:  (86.0, 5.0),
    8:  (84.0, 5.0),
    9:  (76.0, 6.0),
    10: (65.0, 7.0),
    11: (53.0, 8.0),
    12: (42.0, 9.0),
}


def _seasonal_bracket_price(bracket: Bracket, event_date: date) -> tuple[float, float]:
    """
    Return (yes_price, no_price) for a bracket based purely on seasonal norms.
    This is the synthetic 'dumb market' we bet against in the backtest.
    """
    mu, sigma = NYC_NORMS[event_date.month]
    lo, hi = bracket.lower_bound, bracket.upper_bound

    if hi == math.inf:
        p = 1 - scipy_norm.cdf(lo, loc=mu, scale=sigma)
    elif lo == -math.inf:
        p = scipy_norm.cdf(hi, loc=mu, scale=sigma)
    else:
        p = scipy_norm.cdf(hi, loc=mu, scale=sigma) - scipy_norm.cdf(lo, loc=mu, scale=sigma)

    p = max(min(p, 0.99), 0.01)        # clip to 1–99¢
    return p, 1 - p


# ── Data structures ───────────────────────────────────────────────────────────

@dataclass
class DayResult:
    date:               date
    actual_high:        float
    avg_forecast:       float        # mean simulated forecast
    seasonal_center:    float        # monthly norm used as market baseline
    bets_placed:        int          # avg bets per sim
    pnl:                float        # avg P&L across sims (dollars)
    won:                int          # sims where P&L > 0
    lost:               int
    no_bet:             int
    correct_bracket:    Optional[str]
    model_prob_correct: float        # avg our_prob assigned to the correct bracket


@dataclass
class BacktestResult:
    days:           list[DayResult]
    total_pnl:      float
    win_rate:       float
    roi:            float
    total_risked:   float
    avg_daily_pnl:  float
    sharpe:         float
    best_day:       DayResult
    worst_day:      DayResult
    betting_days:   int
    no_bet_days:    int
    accuracy:       float    # % days our top-prob bracket was correct
    avg_brier:      float    # Brier score (lower = better calibration)


# ── Core simulation ───────────────────────────────────────────────────────────

def _simulate_day(markets: list[dict], actual_high: float,
                  event_date: date) -> dict:
    """
    Monte Carlo simulation for one day.
    """
    # Build brackets with SEASONAL prices as market baseline
    raw_brackets = parse_brackets(markets)
    for b in raw_brackets:
        yes_p, no_p = _seasonal_bracket_price(b, event_date)
        b.market_yes_price = yes_p
        b.market_no_price  = no_p

    # Find winning bracket (for scoring)
    correct_bracket = next(
        (b for b in raw_brackets if b.result == "yes"), None
    )
    seasonal_mu = NYC_NORMS[event_date.month][0]

    sim_pnls     = []
    sim_bets     = []
    sim_won      = 0
    sim_lost     = 0
    sim_no_bet   = 0
    forecast_sum = 0.0
    correct_prob_sum = 0.0
    brier_sum    = 0.0

    for _ in range(config.BACKTEST_SIMULATIONS):
        # Simulate realistic NWS forecast error
        forecast = actual_high + random.gauss(0, config.BACKTEST_FORECAST_SIGMA)
        forecast_sum += forecast

        # Copy brackets (so we don't mutate across sims)
        sim_brackets = [
            Bracket(
                ticker=b.ticker, label=b.label,
                lower_bound=b.lower_bound, upper_bound=b.upper_bound,
                market_yes_price=b.market_yes_price,
                market_no_price=b.market_no_price,
                result=b.result,
                last_price=b.last_price,
            )
            for b in raw_brackets
        ]

        # Always use SIGMA_MORNING — same fixed sigma as the live morning bet
        sim_brackets = assign_probabilities(
            sim_brackets, mu=forecast, sigma=config.SIGMA_MORNING
        )

        # Brier score for this sim
        for b in sim_brackets:
            outcome = 1.0 if b.result == "yes" else 0.0
            brier_sum += (b.our_prob - outcome) ** 2

        # Correct bracket probability
        if correct_bracket:
            cb = next((b for b in sim_brackets
                       if b.ticker == correct_bracket.ticker), None)
            if cb:
                correct_prob_sum += cb.our_prob

        signals = compute_signals(sim_brackets)

        if not signals:
            sim_no_bet += 1
            sim_pnls.append(0.0)
            sim_bets.append(0)
            continue

        day_pnl  = 0.0
        day_bets = 0

        for sig in signals:
            contracts = kelly_contracts(sig, config.BANKROLL)
            if contracts <= 0:
                continue

            cost   = contracts * sig.mkt_price
            payout = contracts * 1.0

            won = (sig.side == "yes" and sig.bracket.result == "yes") or \
                  (sig.side == "no"  and sig.bracket.result == "no")

            day_pnl += (payout - cost) if won else -cost
            day_bets += 1

        sim_pnls.append(day_pnl)
        sim_bets.append(day_bets)

        if day_bets > 0:
            if day_pnl > 0:  sim_won  += 1
            else:            sim_lost += 1

    n = config.BACKTEST_SIMULATIONS
    nb = len(raw_brackets)

    return {
        "avg_pnl":            statistics.mean(sim_pnls),
        "avg_bets":           statistics.mean(sim_bets),
        "won":                sim_won,
        "lost":               sim_lost,
        "no_bet":             sim_no_bet,
        "avg_forecast":       forecast_sum / n,
        "seasonal_center":    seasonal_mu,
        "correct_bracket":    correct_bracket.label if correct_bracket else None,
        "model_prob_correct": correct_prob_sum / n,
        "avg_brier":          brier_sum / (n * nb) if nb else 0.0,
    }


# ── Main backtest runner ──────────────────────────────────────────────────────

def run_backtest(days: int = config.BACKTEST_DAYS) -> BacktestResult:
    print(f"[backtest] Fetching last {days} settled events from Kalshi...")
    events = get_settled_events(config.SERIES_TICKER, days=days)

    if not events:
        raise RuntimeError("No settled events found. Check API connectivity.")

    print(f"[backtest] Got {len(events)} events. Running "
          f"{config.BACKTEST_SIMULATIONS} sims/day against seasonal baseline...")

    day_results:  list[DayResult] = []
    total_pnl     = 0.0
    total_risked  = 0.0
    all_pnls      = []
    betting_days  = 0
    no_bet_days   = 0
    accuracy_hits = 0
    brier_total   = 0.0

    for ev in events:
        stats = _simulate_day(ev["markets"], ev["actual_high"], ev["date"])

        dr = DayResult(
            date=ev["date"],
            actual_high=ev["actual_high"],
            avg_forecast=stats["avg_forecast"],
            seasonal_center=stats["seasonal_center"],
            bets_placed=round(stats["avg_bets"]),
            pnl=stats["avg_pnl"],
            won=stats["won"],
            lost=stats["lost"],
            no_bet=stats["no_bet"],
            correct_bracket=stats["correct_bracket"],
            model_prob_correct=stats["model_prob_correct"],
        )
        day_results.append(dr)
        all_pnls.append(dr.pnl)
        total_pnl += dr.pnl
        brier_total += stats["avg_brier"]

        betting_sims = dr.won + dr.lost
        if betting_sims > 0:
            betting_days += 1
            total_risked += abs(dr.pnl) + max(dr.pnl, 0)
        else:
            no_bet_days += 1

        # Accuracy: model's top-prob bracket == correct bracket
        # (use avg_forecast as proxy for "most likely" bracket)
        if dr.model_prob_correct >= 0.30:   # top bracket gets >= 30%
            accuracy_hits += 1

    n = len(day_results)
    profit_days = sum(1 for d in day_results if d.pnl > 0)
    win_rate    = profit_days / n if n else 0.0
    roi         = total_pnl / total_risked if total_risked > 0 else 0.0
    avg_daily   = total_pnl / n if n else 0.0

    if len(all_pnls) > 1:
        std_pnl = statistics.stdev(all_pnls)
        sharpe  = (statistics.mean(all_pnls) / std_pnl) * math.sqrt(252) \
                  if std_pnl > 0 else 0.0
    else:
        sharpe = 0.0

    best_day  = max(day_results, key=lambda d: d.pnl)
    worst_day = min(day_results, key=lambda d: d.pnl)

    return BacktestResult(
        days=sorted(day_results, key=lambda d: d.date, reverse=True),
        total_pnl=total_pnl,
        win_rate=win_rate,
        roi=roi,
        total_risked=total_risked,
        avg_daily_pnl=avg_daily,
        sharpe=sharpe,
        best_day=best_day,
        worst_day=worst_day,
        betting_days=betting_days,
        no_bet_days=no_bet_days,
        accuracy=accuracy_hits / n if n else 0.0,
        avg_brier=brier_total / n if n else 0.0,
    )
