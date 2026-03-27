"""
The Odds API integration — fetches today's MLB games with Vegas consensus odds.
Free tier: 500 requests/month  (https://the-odds-api.com)

Set ODDS_API_KEY in environment.
"""

import os
import requests
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
BASE = "https://api.the-odds-api.com/v4"


def _api_key() -> str:
    return os.getenv("ODDS_API_KEY", "")


def get_mlb_games() -> list[dict]:
    """
    Return today's MLB games with devigged consensus win probabilities.

    Each game:
    {
        "id":          str,            # Odds API game ID
        "home":        str,            # home team name
        "away":        str,            # away team name
        "commence":    datetime,       # first pitch (UTC)
        "home_prob":   float,          # devigged consensus prob (0-1)
        "away_prob":   float,
        "home_ml":     float,          # best American moneyline (home)
        "away_ml":     float,
        "num_books":   int,            # number of books used
    }
    """
    key = _api_key()
    if not key:
        return []

    try:
        resp = requests.get(
            f"{BASE}/sports/baseball_mlb/odds/",
            params={
                "apiKey":      key,
                "regions":     "us",
                "markets":     "h2h",
                "oddsFormat":  "american",
                "dateFormat":  "iso",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[odds_api] Error fetching MLB odds: {e}")
        return []

    games = []
    now = datetime.now(timezone.utc)

    for g in data:
        commence = datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))

        # Skip games that started more than 3 hours ago
        hours_ago = (now - commence).total_seconds() / 3600
        if hours_ago > 3:
            continue

        home = g["home_team"]
        away = g["away_team"]

        # Collect moneylines across all books
        home_odds_list = []
        away_odds_list = []

        for bm in g.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != "h2h":
                    continue
                for outcome in mkt["outcomes"]:
                    if outcome["name"] == home:
                        home_odds_list.append(outcome["price"])
                    elif outcome["name"] == away:
                        away_odds_list.append(outcome["price"])

        if not home_odds_list or not away_odds_list:
            continue

        # Consensus = average implied probability, then devig
        def to_prob(ml):
            return 100 / (ml + 100) if ml > 0 else abs(ml) / (abs(ml) + 100)

        home_probs = [to_prob(o) for o in home_odds_list]
        away_probs = [to_prob(o) for o in away_odds_list]

        avg_home = sum(home_probs) / len(home_probs)
        avg_away = sum(away_probs) / len(away_probs)

        # Devig: normalize so they sum to 1
        total = avg_home + avg_away
        home_prob = avg_home / total
        away_prob = avg_away / total

        # Best available moneyline (closest to consensus)
        home_ml = sorted(home_odds_list, key=lambda x: abs(x))[0]
        away_ml = sorted(away_odds_list, key=lambda x: abs(x))[0]

        games.append({
            "id":        g["id"],
            "home":      home,
            "away":      away,
            "commence":  commence,
            "home_prob": round(home_prob, 4),
            "away_prob": round(away_prob, 4),
            "home_ml":   home_ml,
            "away_ml":   away_ml,
            "num_books": len(g.get("bookmakers", [])),
        })

    # Sort by first pitch
    games.sort(key=lambda g: g["commence"])
    return games
