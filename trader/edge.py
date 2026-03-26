"""
Edge detection.

For each bracket we can bet Yes or No.

  Yes edge = our_prob  - market_yes_ask   (positive → Yes is underpriced)
  No edge  = (1 - our_prob) - market_no_ask  (positive → No is underpriced)

A bet is worth taking when edge > MIN_EDGE.
"""

from dataclasses import dataclass
from predictor.probability import Bracket
import config


@dataclass
class Signal:
    bracket:    Bracket
    side:       str        # "yes" or "no"
    our_prob:   float
    mkt_price:  float
    edge:       float

    @property
    def ticker(self):
        return self.bracket.ticker

    @property
    def label(self):
        return self.bracket.label


def compute_signals(brackets: list[Bracket]) -> list[Signal]:
    """Return all actionable signals (edge > MIN_EDGE), sorted by edge desc."""
    signals = []
    for b in brackets:
        yes_edge = b.our_prob - b.market_yes_price
        no_edge  = (1 - b.our_prob) - b.market_no_price

        if yes_edge > config.MIN_EDGE:
            signals.append(Signal(
                bracket=b,
                side="yes",
                our_prob=b.our_prob,
                mkt_price=b.market_yes_price,
                edge=yes_edge,
            ))

        if no_edge > config.MIN_EDGE:
            signals.append(Signal(
                bracket=b,
                side="no",
                our_prob=1 - b.our_prob,
                mkt_price=b.market_no_price,
                edge=no_edge,
            ))

    signals.sort(key=lambda s: s.edge, reverse=True)
    return signals
