"""
Baseball bet log — stores placed bets and resolves outcomes via Kalshi market status.

Storage:
  - Local:   data/baseball_bets.json
  - Railway: Redis key "kalshi:baseball_bets" (JSON list)
"""

import json
import os
import uuid
from datetime import datetime, timezone

STARTING_BANKROLL = 100.0  # $100 paper bankroll
_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "baseball_bets.json")


# ── Redis helpers (same pattern as daily_lock) ────────────────────────────────

def _redis():
    try:
        import redis as _redis_lib
        url = os.getenv("REDIS_URL") or os.getenv("KV_URL")
        if url:
            return _redis_lib.from_url(url, decode_responses=True)
    except Exception:
        pass
    return None


REDIS_KEY = "kalshi:baseball_bets"


def _load() -> list[dict]:
    r = _redis()
    if r:
        try:
            raw = r.get(REDIS_KEY)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    if os.path.exists(_LOG_PATH):
        try:
            with open(_LOG_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save(bets: list[dict]) -> None:
    r = _redis()
    if r:
        try:
            r.set(REDIS_KEY, json.dumps(bets))
        except Exception:
            pass
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
    with open(_LOG_PATH, "w") as f:
        json.dump(bets, f, indent=2)


# ── Public API ────────────────────────────────────────────────────────────────

def log_bet(home: str, away: str, team: str, side: str, ticker: str,
            contracts: int, price_cents: int, vegas_prob: float, edge: float) -> dict:
    """Record a new placed bet (status=pending)."""
    bets = _load()
    bet = {
        "id":          str(uuid.uuid4())[:8],
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "home":        home,
        "away":        away,
        "team":        team,
        "side":        side,
        "ticker":      ticker,
        "contracts":   contracts,
        "price_cents": price_cents,
        "vegas_prob":  vegas_prob,
        "edge":        edge,
        "status":      "pending",
        "pnl":         None,
        "resolved_at": None,
    }
    bets.append(bet)
    _save(bets)
    return bet


def resolve_pending(dry_run: bool = False) -> int:
    """
    Check all pending bets against Kalshi market status.
    Returns number of bets resolved.
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    bets = _load()
    resolved = 0

    for bet in bets:
        if bet["status"] != "pending":
            continue

        try:
            from kalshi.api import get_market
            mkt = get_market(bet["ticker"])
            status = mkt.get("status", "")
            result = mkt.get("result", "")

            if status != "finalized" or not result:
                continue

            won = (result.lower() == "yes")
            if won:
                pnl = round(bet["contracts"] * (100 - bet["price_cents"]) / 100, 2)
            else:
                pnl = round(-bet["contracts"] * bet["price_cents"] / 100, 2)

            bet["status"]      = "won" if won else "lost"
            bet["pnl"]         = pnl
            bet["resolved_at"] = datetime.now(timezone.utc).isoformat()
            resolved += 1

        except Exception as e:
            print(f"[bet_log] Could not resolve {bet['ticker']}: {e}")

    if resolved:
        _save(bets)
    return resolved


def get_all_bets() -> list[dict]:
    """Return all bets, auto-resolving pending ones first."""
    resolve_pending()
    return _load()


def pnl_summary(bets: list[dict] = None) -> dict:
    """Compute bankroll curve and win/loss stats."""
    if bets is None:
        bets = _load()

    # Sort by timestamp
    sorted_bets = sorted(bets, key=lambda b: b["timestamp"])

    bankroll = STARTING_BANKROLL
    wins = losses = pending = 0
    total_wagered = 0.0
    curve = [{"label": "start", "bankroll": round(bankroll, 2), "timestamp": None}]

    for b in sorted_bets:
        cost = b["contracts"] * b["price_cents"] / 100
        total_wagered += cost

        if b["status"] == "pending":
            pending += 1
        elif b["status"] == "won":
            wins += 1
            bankroll += b["pnl"]
            curve.append({
                "label":     f"{b['team']} W",
                "bankroll":  round(bankroll, 2),
                "timestamp": b["resolved_at"],
                "pnl":       b["pnl"],
                "won":       True,
            })
        elif b["status"] == "lost":
            losses += 1
            bankroll += b["pnl"]  # pnl is negative
            curve.append({
                "label":     f"{b['team']} L",
                "bankroll":  round(bankroll, 2),
                "timestamp": b["resolved_at"],
                "pnl":       b["pnl"],
                "won":       False,
            })

    total_bets = wins + losses
    win_rate   = wins / total_bets if total_bets else 0.0

    return {
        "starting_bankroll": STARTING_BANKROLL,
        "current_bankroll":  round(bankroll, 2),
        "total_pnl":         round(bankroll - STARTING_BANKROLL, 2),
        "wins":              wins,
        "losses":            losses,
        "pending":           pending,
        "win_rate":          round(win_rate * 100, 1),
        "total_wagered":     round(total_wagered, 2),
        "curve":             curve,
    }
