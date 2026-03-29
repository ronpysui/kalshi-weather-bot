"""
Baseball EV analyzer.

Compares devigged Vegas consensus probability against Kalshi implied probability.
Generates signals when edge exceeds threshold.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

ET = ZoneInfo("America/New_York")

BASEBALL_MIN_EDGE    = float(os.getenv("BASEBALL_MIN_EDGE", "0.04"))  # 4 cents
BASEBALL_KELLY_FRAC  = 0.25
LOCK_OUT_MIN         = 30   # stop betting 30 min before first pitch (odds get stale)
MAX_BET_HOURS_BEFORE = 12   # only bet on games starting within 12 hours


@dataclass
class BaseballSignal:
    game_id:       str
    home:          str
    away:          str
    commence:      datetime       # first pitch UTC
    side:          str            # "home" or "away"
    team:          str            # team we're betting on
    ticker:        str            # Kalshi market ticker
    vegas_prob:    float          # devigged Vegas consensus
    kalshi_prob:   float          # Kalshi implied (yes_price)
    edge:          float          # vegas_prob - kalshi_prob
    ev:            float          # expected value per dollar
    kelly_frac:    float          # fractional Kelly stake
    status:        str            # "pre_game" | "live" | "final"
    minutes_to_game: int


def _kelly(prob: float, price: float) -> float:
    """Fractional Kelly for a binary bet."""
    if price <= 0 or price >= 1:
        return 0.0
    b = (1 - price) / price   # net odds per dollar risked
    f = (prob * b - (1 - prob)) / b
    return max(0.0, f * BASEBALL_KELLY_FRAC)


def _ev(prob: float, price: float) -> float:
    """Expected value per dollar risked."""
    payout = (1 - price) / price
    return prob * payout - (1 - prob)


def minutes_to_first_pitch(commence: datetime) -> int:
    now = datetime.now(timezone.utc)
    delta = (commence - now).total_seconds() / 60
    return int(delta)


def analyze_game(game: dict) -> list[BaseballSignal]:
    """
    Analyze a single matched game (Odds API + Kalshi).
    Returns signals whenever edge > threshold and game hasn't started.
    The bot keeps checking on every poll — if no edge now, it will check again
    next poll. Once the game starts (mins_to_game <= -LOCK_OUT_MIN), skip it.
    """
    signals = []
    commence  = game["commence"]
    mins_left = minutes_to_first_pitch(commence)

    # Determine status label (for display only — doesn't gate signal generation)
    if mins_left > 60:
        status = "upcoming"
    elif mins_left > LOCK_OUT_MIN:
        status = "pre_game"
    elif mins_left > -180:
        status = "live"
    else:
        status = "final"

    # Hard stop: game has started — no more bets
    if mins_left <= LOCK_OUT_MIN:
        return []

    # Don't bet too early — wait until game is within MAX_BET_HOURS_BEFORE hours
    if mins_left > MAX_BET_HOURS_BEFORE * 60:
        return []

    kalshi = game.get("kalshi", {})
    if not kalshi:
        return []

    for side, team, vegas_prob, kalshi_yes, ticker in [
        # Use actual ask price for edge calc (home_ask/away_ask), not display price
        ("home", game["home"], game["home_prob"], kalshi.get("home_ask", kalshi.get("home_yes", 0.5)), kalshi.get("home_ticker", "")),
        ("away", game["away"], game["away_prob"], kalshi.get("away_ask", kalshi.get("away_yes", 0.5)), kalshi.get("away_ticker", "")),
    ]:
        if not ticker:
            continue

        edge = vegas_prob - kalshi_yes
        if edge < BASEBALL_MIN_EDGE:
            continue  # no edge yet — will re-check next poll

        signals.append(BaseballSignal(
            game_id         = game["id"],
            home            = game["home"],
            away            = game["away"],
            commence        = commence,
            side            = side,
            team            = team,
            ticker          = ticker,
            vegas_prob      = round(vegas_prob, 4),
            kalshi_prob     = round(kalshi_yes, 4),
            edge            = round(edge, 4),
            ev              = round(_ev(vegas_prob, kalshi_yes), 4),
            kelly_frac      = round(_kelly(vegas_prob, kalshi_yes), 4),
            status          = status,
            minutes_to_game = mins_left,
        ))

    return signals


def analyze_all(matched_games: list[dict]) -> list[BaseballSignal]:
    """Analyze all matched games. Returns all signals sorted by edge desc."""
    all_signals = []
    for game in matched_games:
        all_signals.extend(analyze_game(game))
    all_signals.sort(key=lambda s: s.edge, reverse=True)
    return all_signals
