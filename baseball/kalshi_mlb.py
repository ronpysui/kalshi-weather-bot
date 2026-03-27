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


def get_open_mlb_markets(series: str = None) -> list[dict]:
    """
    Return today's open Kalshi MLB individual game markets.
    Searches for markets closing today (game win markets close at first pitch).
    """
    from datetime import date, timedelta
    today     = date.today()
    tomorrow  = today + timedelta(days=1)

    all_markets = []
    cursor = None

    while True:
        params = {
            "status":            "open",
            "limit":             200,
            "min_close_ts":      int(datetime(today.year, today.month, today.day, 0, 0, 0, tzinfo=timezone.utc).timestamp()),
            "max_close_ts":      int(datetime(tomorrow.year, tomorrow.month, tomorrow.day, 6, 0, 0, tzinfo=timezone.utc).timestamp()),
        }
        if cursor:
            params["cursor"] = cursor
        try:
            data = _get("/markets", params)
        except Exception as e:
            print(f"[kalshi_mlb] Error: {e}")
            break

        markets = data.get("markets", [])
        # Filter to baseball game markets — title contains "win" and team names
        baseball = [
            m for m in markets
            if _is_game_market(m)
        ]
        all_markets.extend(baseball)
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

    # Group by event_ticker
    from collections import defaultdict
    grouped = defaultdict(list)
    for m in markets:
        grouped[m["event_ticker"]].append(m)

    events = []
    for event_ticker, mkts in grouped.items():
        if len(mkts) < 2:
            continue  # need at least home + away

        # Parse teams from subtitle or title
        home, away = None, None
        home_ticker, away_ticker = None, None
        home_yes, away_yes = None, None
        commence = None

        for m in mkts:
            title = (m.get("title") or "").lower()
            subtitle = (m.get("subtitle") or "").lower()
            ticker = m["ticker"]

            # Try to extract close time as commence time
            if not commence and m.get("close_time"):
                try:
                    commence = datetime.fromisoformat(
                        m["close_time"].replace("Z", "+00:00")
                    )
                except Exception:
                    pass

            # yes_bid / yes_ask midpoint as price
            yes_bid = m.get("yes_bid", 0) or 0
            yes_ask = m.get("yes_ask", 99) or 99
            yes_price = (yes_bid + yes_ask) / 2 / 100  # cents → fraction

            # Identify home vs away from title keywords
            if "home" in title or "home" in subtitle:
                home_ticker = ticker
                home_yes = yes_price
                # Try extracting team from title
                home = m.get("result_at_expiry") or m.get("title", "")
            elif "away" in title or "away" in subtitle or "visitor" in title:
                away_ticker = ticker
                away_yes = yes_price
                away = m.get("result_at_expiry") or m.get("title", "")

        # Fallback: use first two markets
        if not home_ticker and len(mkts) >= 2:
            home_ticker = mkts[0]["ticker"]
            away_ticker = mkts[1]["ticker"]
            home_yes = ((mkts[0].get("yes_bid", 0) or 0) + (mkts[0].get("yes_ask", 99) or 99)) / 2 / 100
            away_yes = ((mkts[1].get("yes_bid", 0) or 0) + (mkts[1].get("yes_ask", 99) or 99)) / 2 / 100
            home = mkts[0].get("title", "Team A")
            away = mkts[1].get("title", "Team B")

        if home_ticker and away_ticker:
            events.append({
                "event_ticker": event_ticker,
                "home":         home or "Home",
                "away":         away or "Away",
                "home_ticker":  home_ticker,
                "away_ticker":  away_ticker,
                "home_yes":     home_yes or 0.5,
                "away_yes":     away_yes or 0.5,
                "commence":     commence,
            })

    return events


def match_to_odds(kalshi_events: list[dict], odds_games: list[dict]) -> list[dict]:
    """
    Match Kalshi events to Odds API games by team name fuzzy match.

    Returns list of matched dicts with both Kalshi and Vegas data.
    """
    matched = []

    for game in odds_games:
        best_match = None
        best_score = 0.0

        for ev in kalshi_events:
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
