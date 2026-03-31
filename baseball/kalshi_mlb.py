"""
Fetch Kalshi MLB game markets and match them to Odds API games.

Kalshi MLB win markets follow the pattern:
  Series: KXBASEBALLMLB  (or similar — auto-discovered)
  Event:  KXBASEBALLMLB-HOMETEAM-AWAYTEAM-YYMONDD
  Markets: home_win / away_win binary contracts
"""

import re
import requests
from datetime import datetime, timezone
from difflib import SequenceMatcher

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config

BASE    = config.KALSHI_BASE_URL
HEADERS = {"Content-Type": "application/json"}

def _get(path: str, params: dict = None) -> dict:
    resp = requests.get(BASE + path, headers=HEADERS, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _team_similarity(a: str, b: str) -> float:
    """Fuzzy match two team name strings."""
    a, b = a.lower(), b.lower()
    return SequenceMatcher(None, a, b).ratio()


MLB_SERIES_CANDIDATES = [
    "KXMLBGAME",
    "KXMLBWIN",
    "KXBASEBALLWIN",
    "KXBASEBALLMLBWIN",
]

def get_open_mlb_markets(series: str = None) -> list[dict]:
    """
    Return today's open Kalshi MLB individual game markets.
    Tries known MLB win series tickers first, then falls back to broad search.
    """
    # Try known series tickers first
    for s in MLB_SERIES_CANDIDATES:
        try:
            data = _get("/markets", {"series_ticker": s, "status": "open", "limit": 50})
            markets = data.get("markets", [])
            if markets:
                print(f"[kalshi_mlb] Found MLB game markets under series: {s}")
                return markets
        except Exception:
            continue

    # Fallback: broad date-range search filtered to MLB game markets
    from datetime import date, timedelta
    today    = date.today()
    tomorrow = today + timedelta(days=1)

    all_markets = []
    cursor = None
    while True:
        params = {
            "status":       "open",
            "limit":        200,
            "min_close_ts": int(datetime(today.year, today.month, today.day, 10, 0, 0, tzinfo=timezone.utc).timestamp()),
            "max_close_ts": int(datetime(tomorrow.year, tomorrow.month, tomorrow.day, 8, 0, 0, tzinfo=timezone.utc).timestamp()),
        }
        if cursor:
            params["cursor"] = cursor
        try:
            data = _get("/markets", params)
        except Exception as e:
            print(f"[kalshi_mlb] Error: {e}")
            break
        markets = data.get("markets", [])
        all_markets.extend([m for m in markets if _is_game_market(m)])
        cursor = data.get("cursor")
        if not cursor or len(markets) < 200:
            break

    return all_markets


def _is_game_market(m: dict) -> bool:
    """Return True if this market looks like an individual MLB game win market."""
    title = (m.get("title") or "").lower()
    rules = (m.get("rules_primary") or "").lower()
    ticker = (m.get("ticker") or "").upper()

    # Must mention "win" and be a short-expiry market (closes today)
    if "win" not in title and "win" not in rules:
        return False
    # Exclude futures (close time far in future)
    close = m.get("close_time", "")
    if "2027" in close or "2028" in close or "2029" in close:
        return False
    # Must look like a game (not a season future)
    if "championship" in title or "pennant" in title or "world series" in title:
        return False
    return True


def discover_mlb_series() -> str | None:
    """Legacy — series not needed for game markets."""
    return "KXMLB"


def _parse_event_teams(event_ticker: str, market_tickers: list[str]) -> tuple[str, str]:
    """
    Determine which market ticker is away and which is home.

    Kalshi event format: KXMLBGAME-{YY}{MON}{DD}{HHMM}{away_abbr}{home_abbr}
    Market tickers end with -{team_abbr}.

    Strategy: extract the per-market team suffix and check which comes first
    in the concatenated teams portion of the event ticker.
    """
    # Get suffix after last hyphen for each market ticker
    suffixes = [t.rsplit("-", 1)[-1] for t in market_tickers]

    # Event ticker date/time prefix is YY(2) + MON(3) + DD(2) + HHMM(4) = 11 chars
    # after the series prefix "KXMLBGAME-"
    inner = event_ticker.split("-", 1)[-1] if "-" in event_ticker else event_ticker
    # inner = e.g. "26MAR291920CLESEA"
    teams_str = inner[11:]  # strip date+time prefix

    # Find which suffix appears first in the concatenated teams string
    positions = {}
    for s in suffixes:
        pos = teams_str.find(s)
        positions[s] = pos if pos >= 0 else 999

    if len(suffixes) == 2:
        s0, s1 = suffixes
        if positions[s0] <= positions[s1]:
            away_suffix, home_suffix = s0, s1
        else:
            away_suffix, home_suffix = s1, s0
        away_ticker = next(t for t in market_tickers if t.endswith(f"-{away_suffix}"))
        home_ticker = next(t for t in market_tickers if t.endswith(f"-{home_suffix}"))
        return away_ticker, home_ticker

    return market_tickers[0], market_tickers[1]


def get_mlb_events(series: str = None) -> list[dict]:
    """
    Group open markets by event and extract team + price info.

    Returns list of:
    {
        "event_ticker": str,
        "home":         str,
        "away":         str,
        "home_ticker":  str,    # market ticker for home win
        "away_ticker":  str,
        "home_yes":     float,  # Kalshi YES price 0-1
        "away_yes":     float,
        "commence":     datetime | None,
    }
    """
    markets = get_open_mlb_markets(series)
    if not markets:
        return []

    from collections import defaultdict
    grouped = defaultdict(list)
    for m in markets:
        grouped[m["event_ticker"]].append(m)

    events = []
    for event_ticker, mkts in grouped.items():
        if len(mkts) < 2:
            continue

        # Determine home/away from ticker structure
        away_ticker, home_ticker = _parse_event_teams(
            event_ticker, [m["ticker"] for m in mkts]
        )

        away_mkt = next((m for m in mkts if m["ticker"] == away_ticker), mkts[0])
        home_mkt = next((m for m in mkts if m["ticker"] == home_ticker), mkts[1])

        # Parse team names from title: "Away Team vs Home Team Winner?"
        title = (away_mkt.get("title") or "").replace(" Winner?", "").replace(" winner?", "")
        if " vs " in title:
            parts = title.split(" vs ", 1)
            away_name = parts[0].strip()
            home_name = parts[1].strip()
        else:
            away_name = away_mkt.get("ticker", "Away").rsplit("-", 1)[-1]
            home_name = home_mkt.get("ticker", "Home").rsplit("-", 1)[-1]

        def _ask(m) -> float:
            """Actual YES ask price — what you pay to buy YES."""
            ask_d = m.get("yes_ask_dollars")
            if ask_d is not None:
                return float(ask_d)
            ask = m.get("yes_ask") or 99
            return ask / 100

        # Kalshi displays prices anchored on the away team's ask, home derived
        # as complement so they sum to 100% (matching Kalshi's "Chance" UI).
        away_ask = _ask(away_mkt)
        home_ask = _ask(home_mkt)

        # Commence time from close_time (trading closes at/near game start)
        commence = None
        ct = away_mkt.get("close_time") or home_mkt.get("close_time")
        if ct:
            try:
                commence = datetime.fromisoformat(ct.replace("Z", "+00:00"))
            except Exception:
                pass
        print(f"[kalshi_mlb] Event {event_ticker}: close_time={ct}, "
              f"home={home_name}, away={away_name}, "
              f"commence={commence}")

        events.append({
            "event_ticker": event_ticker,
            "home":         home_name,
            "away":         away_name,
            "home_ticker":  home_ticker,
            "away_ticker":  away_ticker,
            # Display: away anchors, home = complement → sums to 100% like Kalshi UI
            "home_yes":     round(1.0 - away_ask, 4),
            "away_yes":     round(away_ask, 4),
            # Actual ask prices for edge calculation (what you'd really pay)
            "home_ask":     round(home_ask, 4),
            "away_ask":     round(away_ask, 4),
            "commence":     commence,
        })

    return events


def _parse_ticker_date(event_ticker: str) -> str | None:
    """Extract date from Kalshi event ticker as 'YYYY-MM-DD' in US/Eastern.

    Ticker format: KXMLBGAME-26MAR312140NYYSEA
    → 26=2026, MAR=March, 31=day, 2140=time(ET)
    Returns '2026-03-31' or None on failure.
    """
    try:
        mid = event_ticker.split("-")[1]  # "26MAR312140NYYSEA"
        yr = int(mid[:2]) + 2000
        mon_str = mid[2:5]
        mon_map = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
                   "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}
        mon = mon_map.get(mon_str)
        if not mon:
            return None
        day = int(mid[5:7])
        # Ticker time is ET local, so the date in the ticker IS the ET game date
        return f"{yr}-{mon:02d}-{day:02d}"
    except Exception:
        return None


def match_to_odds(kalshi_events: list[dict], odds_games: list[dict]) -> list[dict]:
    """
    Match Kalshi events to Odds API games by team name fuzzy match + date.

    Returns list of matched dicts with both Kalshi and Vegas data.
    """
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
    matched = []

    for game in odds_games:
        # Get game date in ET (same timezone Kalshi tickers use)
        game_dt = game["commence"]
        if hasattr(game_dt, 'astimezone'):
            game_date_et = game_dt.astimezone(ET).strftime("%Y-%m-%d")
        else:
            game_date_et = str(game_dt)[:10]

        best_match = None
        best_score = 0.0

        for ev in kalshi_events:
            # Date must match (prevents cross-day false matches)
            # Prefer commence (from close_time = actual game time) over ticker date
            ev_commence = ev.get("commence")
            if ev_commence and hasattr(ev_commence, 'astimezone'):
                ev_date = ev_commence.astimezone(ET).strftime("%Y-%m-%d")
            else:
                ev_date = _parse_ticker_date(ev.get("event_ticker", ""))
            if ev_date and game_date_et and ev_date != game_date_et:
                continue  # different day — skip

            # Score based on home+away team name similarity
            score = max(
                _team_similarity(game["home"], ev["home"]) +
                _team_similarity(game["away"], ev["away"]),
                _team_similarity(game["home"], ev["away"]) +
                _team_similarity(game["away"], ev["home"]),
            )
            if score > best_score:
                best_score = score
                best_match = ev

        if best_match and best_score > 1.0:  # both teams reasonably matched
            matched.append({
                **game,
                "kalshi":       best_match,
                "match_score":  round(best_score, 2),
            })

    return matched
