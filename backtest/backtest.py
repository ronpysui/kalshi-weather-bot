"""
30-day backtest engine — honest forecast accuracy.

Method
------
For each settled day in the past 30 days:

  1. FORECAST: NYC seasonal climatological normal for that month.
     This is completely honest — the model has NO knowledge of the
     actual outcome when computing probabilities.

  2. MODEL: Same as live — N(forecast, SIGMA_MORNING).
     Probabilities integrated over each Kalshi bracket.

  3. W/L: Did the model's highest-probability bracket match
     the bracket that actually settled YES on Kalshi?

No Monte Carlo, no P&L simulation, no cheating.
"""

from dataclasses import dataclass
from datetime import date
from typing import Optional

from kalshi.api import get_settled_events
from predictor.probability import parse_brackets, assign_probabilities
from weather.nws_forecast import get_historical_forecasts
import config


# NYC monthly avg high °F — Central Park historical norms
NYC_NORMS: dict[int, float] = {
    1:  38.0,
    2:  42.0,
    3:  52.0,
    4:  62.0,
    5:  72.0,
    6:  81.0,
    7:  86.0,
    8:  84.0,
    9:  76.0,
    10: 65.0,
    11: 53.0,
    12: 42.0,
}


@dataclass
class BracketSnap:
    label:   str
    prob:    float   # model probability (%)
    result:  str     # "yes" / "no" / ""
    is_top:  bool    # model's highest-probability pick
    is_win:  bool    # this bracket settled YES


@dataclass
class DayResult:
    date:             date
    actual_high:      float
    forecast:         float
    forecast_src:     str    # "open-meteo" or "seasonal"
    sigma:            float
    correct_bracket:  Optional[str]
    top_bracket:      Optional[str]
    correct:          bool
    brackets:         list[BracketSnap]


@dataclass
class BacktestResult:
    days:      list[DayResult]
    wins:      int
    losses:    int
    win_rate:  float
    total:     int


def run_backtest(days: int = config.BACKTEST_DAYS) -> BacktestResult:
    print(f"[backtest] fetching last {days} settled events...")
    events = get_settled_events(config.SERIES_TICKER, days=days)

    if not events:
        raise RuntimeError("No settled events found. Check API connectivity.")

    # Fetch historical forecasts from Open-Meteo for the full date range
    sorted_events = sorted(events, key=lambda e: e["date"])
    start_date = sorted_events[0]["date"]
    end_date   = sorted_events[-1]["date"]
    print(f"[backtest] fetching Open-Meteo historical forecasts {start_date} to {end_date}...")
    hist_forecasts = get_historical_forecasts(start_date, end_date)
    print(f"[backtest] got {len(hist_forecasts)} historical forecast days")

    day_results: list[DayResult] = []
    wins = 0
    losses = 0

    for ev in events:
        event_date  = ev["date"]
        actual_high = ev["actual_high"]
        markets     = ev["markets"]

        # ── Use Open-Meteo historical forecast; fall back to seasonal norm ─────
        date_str = str(event_date)
        if date_str in hist_forecasts:
            forecast      = hist_forecasts[date_str]
            forecast_src  = "open-meteo"
        else:
            forecast      = NYC_NORMS[event_date.month]
            forecast_src  = "seasonal"
        sigma = config.SIGMA_MORNING

        # ── Compute bracket probabilities ─────────────────────────────────────
        brackets = parse_brackets(markets)
        if not brackets:
            continue
        brackets = assign_probabilities(brackets, mu=forecast, sigma=sigma)

        # ── Find correct bracket (settled YES on Kalshi) ──────────────────────
        correct_b = next((b for b in brackets if b.result == "yes"), None)

        # ── Model's top pick (highest probability bracket) ────────────────────
        top_b = max(brackets, key=lambda b: b.our_prob)

        # ── W/L ───────────────────────────────────────────────────────────────
        is_correct = (correct_b is not None and
                      correct_b.ticker == top_b.ticker)

        if is_correct:
            wins += 1
        else:
            losses += 1

        snaps = [
            BracketSnap(
                label=b.label,
                prob=round(b.our_prob * 100, 1),
                result=b.result or "",
                is_top=(b.ticker == top_b.ticker),
                is_win=(b.result == "yes"),
            )
            for b in brackets
        ]

        day_results.append(DayResult(
            date=event_date,
            actual_high=actual_high,
            forecast=forecast,
            forecast_src=forecast_src,
            sigma=sigma,
            correct_bracket=correct_b.label if correct_b else None,
            top_bracket=top_b.label,
            correct=is_correct,
            brackets=snaps,
        ))

    n = len(day_results)
    return BacktestResult(
        days=sorted(day_results, key=lambda d: d.date, reverse=True),
        wins=wins,
        losses=losses,
        win_rate=wins / n if n else 0.0,
        total=n,
    )
