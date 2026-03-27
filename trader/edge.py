"""
Edge detection — directional, forecast-aligned.

Philosophy
----------
The model locks a predicted high (e.g. 75°F) at market open.
Every signal must AGREE with that prediction:

  • High-probability brackets (our_prob > 50%)  → look for cheap YES
  • Low-probability brackets  (our_prob < 50%)  → look for cheap NO

We never bet AGAINST the forecast just because Kalshi misprices the other side.
A bet is worth taking only when edge > MIN_EDGE (covers fees + noise).

Edge definitions
----------------
  YES edge = our_prob        - market_yes_price   (positive → YES underpriced)
  NO  edge = (1 - our_prob)  - market_no_price    (positive → NO  underpriced)
"""

from dataclasses import dataclass
from predictor.probability import Bracket
import config


@dataclass
class Signal:
    bracket:    Bracket
    side:       str        # "yes" or "no"
    our_prob:   float      # probability we assign to the bet winning
    mkt_price:  float      # what Kalshi charges for this side (0–1)
    edge:       float      # our_prob - mkt_price  (always positive for signals)

    @property
    def ticker(self):
        return self.bracket.ticker

    @property
    def label(self):
        return self.bracket.label


def compute_signals(brackets: list[Bracket]) -> list[Signal]:
    """
    Return directional, forecast-aligned signals sorted by edge (best first).

    For each bracket:
      - If model says YES likely (our_prob >= 0.5): only surface a YES signal
        if Kalshi is offering YES too cheaply.
      - If model says NO likely  (our_prob <  0.5): only surface a NO  signal
        if Kalshi is offering NO too cheaply.

    This keeps every trade in the same direction as the locked forecast.
    """
    signals: list[Signal] = []

    for b in brackets:
        if b.our_prob >= 0.5:
            # ── Model favours YES ─────────────────────────────────────────────
            yes_edge = b.our_prob - b.market_yes_price
            if yes_edge > config.MIN_EDGE:
                signals.append(Signal(
                    bracket=b,
                    side="yes",
                    our_prob=b.our_prob,
                    mkt_price=b.market_yes_price,
                    edge=yes_edge,
                ))

        else:
            # ── Model favours NO ──────────────────────────────────────────────
            no_prob = 1.0 - b.our_prob
            no_edge = no_prob - b.market_no_price
            if no_edge > config.MIN_EDGE:
                signals.append(Signal(
                    bracket=b,
                    side="no",
                    our_prob=no_prob,
                    mkt_price=b.market_no_price,
                    edge=no_edge,
                ))

    # Best edge first — natural priority for Kelly sizing
    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals
