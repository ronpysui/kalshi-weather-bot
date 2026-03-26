"""
Probability engine.

Builds a calibrated normal distribution N(μ, σ²) over the predicted high
temperature and integrates it over each Kalshi bracket to produce bracket
probabilities.

Bracket boundary convention (Kalshi KXHIGHNY)
----------------------------------------------
Temperature is reported as an integer °F.

Ticker suffix → range
  T{N}  (no floor_strike) → (-∞, N)       e.g. T65  = ≤ 64°  → "64° or below"
  B{N}.5                  → [N-0.5, N+1.5) e.g. B65.5 = 65-66° → "65° to 66°"
  T{N}  (has floor_strike) → [N+1, +∞)    e.g. T72  = ≥ 73°  → "73° or above"

For a continuous normal CDF, the integration bounds are:
  bottom cap : (-∞,  64.5]
  band       : (lo-0.5, lo+1.5]   where lo = int(strike - 0.5)
  top cap    : (72.5, +∞)
"""

from dataclasses import dataclass, field
from typing import Optional
import math
from scipy.stats import norm


@dataclass
class Bracket:
    ticker:       str
    label:        str             # e.g. "71° to 72°"
    lower_bound:  float           # inclusive (use -inf for bottom cap)
    upper_bound:  float           # exclusive (use +inf for top cap)
    market_yes_price: float       # Kalshi ask price (0–1)
    market_no_price:  float       # Kalshi no-ask price (0–1)
    result:       Optional[str]   # "yes" / "no" / None (open)
    last_price:   Optional[float] # last traded price (for backtest)

    our_prob:     float = field(default=0.0, init=False)


def parse_brackets(markets: list[dict]) -> list["Bracket"]:
    """Convert raw Kalshi market dicts into Bracket objects."""
    brackets = []
    for m in markets:
        ticker = m["ticker"]
        suffix = ticker.split("-")[-1]

        has_floor = m.get("floor_strike") is not None

        if suffix.startswith("B"):
            strike = float(suffix[1:])             # e.g. 65.5
            lo = strike - 0.5                       # 65.0
            label = f"{int(lo)}° to {int(lo)+1}°"
            lower = lo - 0.5                        # integration: 64.5
            upper = lo + 1.5                        # integration: 66.5
        elif suffix.startswith("T") and not has_floor:
            cap = float(suffix[1:])                 # e.g. 65
            label = f"{int(cap)-1}° or below"
            lower = -math.inf
            upper = cap - 0.5
        elif suffix.startswith("T") and has_floor:
            floor_val = float(suffix[1:])           # e.g. 72
            label = f"{int(floor_val)+1}° or above"
            lower = floor_val + 0.5
            upper = math.inf
        else:
            label = ticker
            lower = -math.inf
            upper = math.inf

        yes_ask = float(m.get("yes_ask_dollars") or m.get("last_price_dollars") or 0.5)
        no_ask  = float(m.get("no_ask_dollars") or (1 - yes_ask))

        brackets.append(Bracket(
            ticker=ticker,
            label=label,
            lower_bound=lower,
            upper_bound=upper,
            market_yes_price=yes_ask,
            market_no_price=no_ask,
            result=m.get("result"),
            last_price=float(m["last_price_dollars"]) if m.get("last_price_dollars") else None,
        ))

    return brackets


def assign_probabilities(brackets: list[Bracket],
                         mu: float, sigma: float) -> list[Bracket]:
    """
    Fill in `our_prob` for each bracket using N(mu, sigma).
    Probabilities are renormalised to sum to 1.
    """
    raw = []
    for b in brackets:
        if b.upper_bound == math.inf:
            p = 1 - norm.cdf(b.lower_bound, loc=mu, scale=sigma)
        elif b.lower_bound == -math.inf:
            p = norm.cdf(b.upper_bound, loc=mu, scale=sigma)
        else:
            p = (norm.cdf(b.upper_bound, loc=mu, scale=sigma) -
                 norm.cdf(b.lower_bound, loc=mu, scale=sigma))
        raw.append(max(p, 1e-6))

    total = sum(raw)
    for b, p in zip(brackets, raw):
        b.our_prob = p / total

    return brackets
