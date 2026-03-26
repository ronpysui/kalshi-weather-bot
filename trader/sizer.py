"""
Position sizing via fractional Kelly Criterion.

Kelly formula for a binary bet:
    f* = (p * b - (1-p)) / b
    where b = (1 - price) / price  (net odds)

We use KELLY_FRACTION * f* as the bet fraction of bankroll.
Minimum bet size on Kalshi is $1 (1 contract).
"""

import math
import config
from trader.edge import Signal


def kelly_contracts(signal: Signal, bankroll: float) -> int:
    """
    Return the number of $1 contracts to buy given a signal and bankroll.
    Returns 0 if Kelly fraction is non-positive.
    """
    p = signal.our_prob
    price = signal.mkt_price

    if price <= 0 or price >= 1:
        return 0

    # Net odds: for every $price risked, win $(1-price)
    b = (1 - price) / price
    f_star = (p * b - (1 - p)) / b          # full Kelly fraction
    f_adj  = f_star * config.KELLY_FRACTION  # fractional Kelly

    if f_adj <= 0:
        return 0

    dollar_bet = bankroll * f_adj
    contracts  = int(dollar_bet)             # 1 contract = $1 notional
    return max(contracts, 1)                 # at least 1 if we have edge


def expected_value(signal: Signal) -> float:
    """Expected value per $1 bet."""
    p     = signal.our_prob
    price = signal.mkt_price
    win   = (1 - price)   # profit if correct
    loss  = price         # loss if wrong
    return p * win - (1 - p) * loss


def expected_profit(signal: Signal, bankroll: float) -> float:
    contracts = kelly_contracts(signal, bankroll)
    return contracts * expected_value(signal)
