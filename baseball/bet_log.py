"""
Baseball bet log — stores placed bets and resolves outcomes via MLB Stats API
and Kalshi market status.

Storage:
  - Local:   data/baseball_bets.json
  - Railway: Redis key "kalshi:baseball_bets" (JSON list)
"""

import json
import os
import uuid
from datetime import datetime, timezone

_STARTING_BANKROLL_DEFAULT = 100.0  # fallback if Kalshi balance unavailable
_LOG_PATH  = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "baseball_bets.json")
_META_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "data", "baseball_meta.json")
REDIS_META_KEY = "kalshi:baseball_meta"


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


# ── MLB Stats API outcome resolution ─────────────────────────────────────────

def _parse_game_date_from_ticker(ticker: str) -> str | None:
    """Extract game date (YYYY-MM-DD) from ticker like KXMLBGAME-26MAR312140NYYSEA-SEA."""
    try:
        mid = ticker.split("-")[1]  # "26MAR312140NYYSEA"
        yr = int(mid[:2]) + 2000
        mon_str = mid[2:5]
        mon_map = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                   "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        mon = mon_map.get(mon_str)
        if not mon:
            return None
        day = int(mid[5:7])
        return f"{yr}-{mon:02d}-{day:02d}"
    except Exception:
        return None


def _resolve_via_mlb_stats(bet: dict) -> bool:
    """
    Check the MLB Stats API for the game result.
    Returns True if the bet was resolved (win/loss), False if still pending.
    Uses the game_date field (YYYY-MM-DD) on the bet, or falls back to
    extracting the date from the timestamp.
    """
    try:
        import urllib.request

        # Determine the game date — prefer ticker-derived date (most accurate)
        game_date = None
        ticker = bet.get("ticker", "")
        if ticker:
            game_date = _parse_game_date_from_ticker(ticker)
        if not game_date:
            game_date = bet.get("game_date")
        if not game_date:
            ts = bet.get("timestamp", "")
            game_date = ts[:10] if ts else None
        if not game_date:
            return False

        # Don't try to resolve future games
        from datetime import date
        if game_date > date.today().isoformat():
            return False

        url = (
            f"https://statsapi.mlb.com/api/v1/schedule"
            f"?sportId=1&date={game_date}&hydrate=linescore"
        )
        with urllib.request.urlopen(url, timeout=8) as resp:
            schedule = json.loads(resp.read().decode())

        home_team = (bet.get("home") or "").lower().strip()
        away_team = (bet.get("away") or "").lower().strip()
        bet_team  = (bet.get("team") or "").lower().strip()

        for date_obj in schedule.get("dates", []):
            for game in date_obj.get("games", []):
                status = game.get("status", {}).get("abstractGameState", "")
                if status != "Final":
                    continue

                g_home = game.get("teams", {}).get("home", {}).get("team", {}).get("name", "").lower()
                g_away = game.get("teams", {}).get("away", {}).get("team", {}).get("name", "").lower()

                # Fuzzy match: check if our stored team names appear in the MLB names or vice versa
                def _teams_match(a: str, b: str) -> bool:
                    a_words = set(a.split())
                    b_words = set(b.split())
                    return bool(a_words & b_words) or a in b or b in a

                if not (_teams_match(home_team, g_home) and _teams_match(away_team, g_away)):
                    if not (_teams_match(home_team, g_away) and _teams_match(away_team, g_home)):
                        continue

                # Determine winner
                home_score = game.get("teams", {}).get("home", {}).get("score", 0) or 0
                away_score = game.get("teams", {}).get("away", {}).get("score", 0) or 0

                if home_score == away_score:
                    continue  # tie / incomplete data

                home_won = home_score > away_score
                # Which team did we bet on?
                bet_side = bet.get("side", "").lower()
                # Map side to whether home won
                if bet_side == "home":
                    we_won = home_won
                elif bet_side == "away":
                    we_won = not home_won
                else:
                    # Fallback: check team name
                    if _teams_match(bet_team, g_home):
                        we_won = home_won
                    elif _teams_match(bet_team, g_away):
                        we_won = not home_won
                    else:
                        continue

                price_cents = bet.get("price_cents", 50)
                contracts   = bet.get("contracts", 1)
                cost        = contracts * price_cents / 100

                if we_won:
                    pnl = round(contracts * (100 - price_cents) / 100, 2)
                    bet["status"]  = "won"
                    bet["result"]  = "win"
                else:
                    pnl = round(-cost, 2)
                    bet["status"]  = "lost"
                    bet["result"]  = "loss"

                bet["pnl"]         = pnl
                bet["resolved_at"] = datetime.now(timezone.utc).isoformat()
                return True

    except Exception as e:
        print(f"[bet_log] MLB Stats API lookup failed for bet {bet.get('id')}: {e}")

    return False


# ── Public API ────────────────────────────────────────────────────────────────

def _load_meta() -> dict:
    r = _redis()
    if r:
        try:
            raw = r.get(REDIS_META_KEY)
            if raw:
                return json.loads(raw)
        except Exception:
            pass
    if os.path.exists(_META_PATH):
        try:
            with open(_META_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_meta(meta: dict) -> None:
    r = _redis()
    if r:
        try:
            r.set(REDIS_META_KEY, json.dumps(meta))
        except Exception:
            pass
    os.makedirs(os.path.dirname(_META_PATH), exist_ok=True)
    with open(_META_PATH, "w") as f:
        json.dump(meta, f, indent=2)


def _get_starting_bankroll(bets: list) -> float:
    """
    Return the starting bankroll to use for a fresh bet log.
    On the very first bet, fetch the live Kalshi balance so the curve starts
    at the real account value. Falls back to config.BANKROLL then the default.
    """
    try:
        import config as _config
        from kalshi.api import get_account_balance
        balance = get_account_balance()
        if balance is not None:
            return balance
        return _config.BANKROLL
    except Exception:
        return _STARTING_BANKROLL_DEFAULT


def log_bet(home: str, away: str, team: str, side: str, ticker: str,
            contracts: int, price_cents: int, vegas_prob: float, edge: float,
            game_id: str = None, game_date: str = None) -> dict:
    """Record a new placed bet (status=pending). Bets accumulate and never reset."""
    bets = _load()
    now_utc = datetime.now(timezone.utc)
    bet = {
        "id":          str(uuid.uuid4())[:8],
        "timestamp":   now_utc.isoformat(),
        "date_placed": now_utc.strftime("%Y-%m-%d"),
        "game_id":     game_id or "",
        "game_date":   game_date or now_utc.strftime("%Y-%m-%d"),
        "home":        home,
        "away":        away,
        "teams":       f"{away} @ {home}",
        "team":        team,
        "team_bet_on": team,
        "bet_side":    side,
        "side":        side,
        "ticker":      ticker,
        "contracts":   contracts,
        "price_cents": price_cents,
        "cost":        round(contracts * price_cents / 100, 2),
        "vegas_prob":  vegas_prob,
        "edge":        edge,
        "status":      "pending",
        "result":      "pending",
        "pnl":         None,
        "resolved_at": None,
    }
    # On the very first bet, record the real starting bankroll
    meta = _load_meta()
    if not bets and "starting_bankroll" not in meta:
        meta["starting_bankroll"] = _get_starting_bankroll(bets)
        _save_meta(meta)

    bets.append(bet)
    _save(bets)
    return bet


def resolve_pending(dry_run: bool = False) -> int:
    """
    Check all pending bets against MLB Stats API and Kalshi market status.
    Returns number of bets resolved.
    """
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    bets = _load()
    resolved = 0

    for bet in bets:
        if bet["status"] != "pending":
            continue

        # Try MLB Stats API first (more reliable for completed games)
        if _resolve_via_mlb_stats(bet):
            resolved += 1
            continue

        # Fallback: Kalshi market finalization
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
            bet["result"]      = "win" if won else "loss"
            bet["pnl"]         = pnl
            bet["resolved_at"] = datetime.now(timezone.utc).isoformat()
            resolved += 1

        except Exception as e:
            print(f"[bet_log] Could not resolve {bet.get('ticker')}: {e}")

    if resolved:
        _save(bets)
    return resolved


def get_all_bets() -> list[dict]:
    """Return all bets (all historical), auto-resolving pending ones first."""
    resolve_pending()
    return _load()


def pnl_summary(bets: list[dict] = None) -> dict:
    """Compute bankroll curve and win/loss stats."""
    if bets is None:
        bets = _load()

    # Sort by timestamp
    sorted_bets = sorted(bets, key=lambda b: b["timestamp"])

    # Use real starting bankroll from meta if available, else default
    meta = _load_meta()
    starting_bankroll = meta.get("starting_bankroll", _STARTING_BANKROLL_DEFAULT)

    bankroll = starting_bankroll
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
        "starting_bankroll": starting_bankroll,
        "current_bankroll":  round(bankroll, 2),
        "total_pnl":         round(bankroll - starting_bankroll, 2),
        "wins":              wins,
        "losses":            losses,
        "pending":           pending,
        "win_rate":          round(win_rate * 100, 1),
        "total_wagered":     round(total_wagered, 2),
        "curve":             curve,
    }
