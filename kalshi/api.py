"""
Kalshi API client.

Public endpoints (market data) work without credentials.
Order placement requires KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH in .env.
"""

import base64
import time
import os
from datetime import datetime, timezone
from typing import Optional

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _load_private_key():
    # Option 1: key contents pasted directly as env var (Railway / cloud)
    pem = os.getenv("KALSHI_PRIVATE_KEY_CONTENTS", "")
    if pem:
        # Allow \n to be stored as literal \n in env var
        pem = pem.replace("\\n", "\n").encode()
        return serialization.load_pem_private_key(pem, password=None)

    # Option 2: path to .key file on disk (local dev)
    path = config.KALSHI_PRIVATE_KEY_PATH
    if path and os.path.exists(path):
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    return None


def _sign(private_key, timestamp_ms: int, method: str, path: str) -> str:
    path_no_query = path.split("?")[0]
    message = f"{timestamp_ms}{method.upper()}{path_no_query}".encode()
    sig = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode()


def _auth_headers(method: str, path: str) -> dict:
    key = _load_private_key()
    if not key or not config.KALSHI_API_KEY_ID:
        return {}
    ts = int(time.time() * 1000)
    return {
        "KALSHI-ACCESS-KEY": config.KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": str(ts),
        "KALSHI-ACCESS-SIGNATURE": _sign(key, ts, method, path),
    }


# ── Generic request ───────────────────────────────────────────────────────────

def _get(path: str, params: dict = None, auth: bool = False) -> dict:
    url = config.KALSHI_BASE_URL + path
    headers = {"Content-Type": "application/json"}
    if auth:
        headers.update(_auth_headers("GET", "/trade-api/v2" + path))
    resp = requests.get(url, headers=headers, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, body: dict) -> dict:
    url = config.KALSHI_BASE_URL + path
    headers = {
        "Content-Type": "application/json",
        **_auth_headers("POST", "/trade-api/v2" + path),
    }
    resp = requests.post(url, headers=headers, json=body, timeout=10)
    if not resp.ok:
        print(f"[order] POST {path} → {resp.status_code}: {resp.text}")
    resp.raise_for_status()
    return resp.json()


# ── Account ───────────────────────────────────────────────────────────────────

def get_account_balance() -> float | None:
    """
    Return available Kalshi balance in USD, or None if unauthenticated.
    Kalshi returns balance in cents — divide by 100.
    """
    try:
        data = _get("/portfolio/balance", auth=True)
        cents = data.get("balance", data.get("available_balance", 0))
        return round(cents / 100, 2)
    except Exception:
        return None


def get_portfolio_value() -> dict | None:
    """
    Return full portfolio breakdown: cash balance + position cost = portfolio.
    Kalshi balance API doesn't include portfolio_value, so we compute it
    from cash + sum of open position exposure.
    """
    try:
        data = _get("/portfolio/balance", auth=True)
        print(f"[kalshi] Raw balance response: {data}")
        # balance = total cash including locked in resting orders
        # available_balance = free cash (not locked)
        total_cash = data.get("balance", data.get("available_balance", 0))
        available_cash = data.get("available_balance", total_cash)

        # Get position values from positions API
        positions_value = 0
        try:
            pos_data = _get("/portfolio/positions", params={"limit": 1000}, auth=True)
            for p in pos_data.get("market_positions", []):
                qty = float(p.get("position_fp", 0) or 0)
                exposure = abs(float(p.get("market_exposure_dollars", 0) or 0))
                if qty > 0:
                    positions_value += exposure
        except Exception:
            pass

        portfolio = round((total_cash / 100) + positions_value, 2)
        return {
            "cash": round(available_cash / 100, 2),
            "portfolio": portfolio,
            "total_cash": round(total_cash / 100, 2),
            "positions_value": round(positions_value, 2),
        }
    except Exception as e:
        print(f"[kalshi] Portfolio value error: {e}")
        return None


# ── Market data ───────────────────────────────────────────────────────────────

def get_event(event_ticker: str) -> dict:
    """Return event + all its markets."""
    return _get(f"/events/{event_ticker}")


def get_markets_for_series(series_ticker: str, status: str = "open",
                           limit: int = 200) -> list[dict]:
    """Return all markets for a series filtered by status, paginating through all results."""
    all_markets = []
    cursor = None
    page_size = 200  # max per request

    while True:
        params = {
            "series_ticker": series_ticker,
            "status": status,
            "limit": page_size,
        }
        if cursor:
            params["cursor"] = cursor

        data = _get("/markets", params=params)
        markets = data.get("markets", [])
        all_markets.extend(markets)

        cursor = data.get("cursor")
        if not cursor or len(markets) < page_size:
            break  # no more pages

        # Stop early if we've exceeded the requested limit
        if limit and len(all_markets) >= limit:
            break

    return all_markets


def get_market(ticker: str) -> dict:
    return _get(f"/markets/{ticker}")["market"]


def get_orderbook(ticker: str) -> dict:
    return _get(f"/markets/{ticker}/orderbook")["orderbook_fp"]


def get_todays_event_ticker(city_key: str = None) -> str:
    """Build the event ticker for today's high-temp market for the given city."""
    city_key = city_key or config.DEFAULT_CITY
    series = config.CITIES[city_key]["kalshi_series"]
    now = datetime.now(timezone.utc)
    return f"{series}-{now.strftime('%y%b%d').upper()}"


def get_todays_markets(city_key: str = None) -> list[dict]:
    """Return today's open bracket markets for a city, sorted low → high."""
    ticker = get_todays_event_ticker(city_key)
    try:
        data = get_event(ticker)
    except requests.HTTPError as e:
        if e.response.status_code == 404:
            return []
        raise
    markets = data.get("markets", [])
    return _sort_brackets(markets)


def _sort_brackets(markets: list[dict]) -> list[dict]:
    """Sort markets by their lower temperature bound."""
    def _lower(m):
        t = m["ticker"]
        part = t.split("-")[-1]
        if part.startswith("T") and m.get("floor_strike") is None:
            return -999          # bottom cap
        if part.startswith("T") and m.get("floor_strike") is not None:
            return 999           # top cap
        if part.startswith("B"):
            return float(part[1:])
        return 0
    return sorted(markets, key=_lower)


# ── Order placement ───────────────────────────────────────────────────────────

def place_order(ticker: str, side: str, count: int,
                price_cents: int, action: str = "buy") -> dict:
    """
    Place a limit order.

    side        : "yes" or "no"
    count       : number of contracts (each contract = $1 notional)
    price_cents : limit price in cents (1–99)
    """
    if config.DRY_RUN:
        print(f"[DRY RUN] Would place: {action} {count}x {side.upper()} "
              f"on {ticker} @ {price_cents}¢")
        return {"status": "dry_run"}

    price_key = "yes_price" if side.lower() == "yes" else "no_price"
    body = {
        "ticker":     ticker,
        "action":     action,
        "side":       side,
        "type":       "limit",
        "count":      count,
        price_key:    price_cents,
    }
    return _post("/portfolio/orders", body)


# ── Open positions ────────────────────────────────────────────────────────────

def get_open_positions() -> dict:
    """
    Return open Kalshi positions keyed by ticker.
    Each value: {"quantity": int, "avg_price": float (cents)}
    Returns empty dict on error or if unauthenticated.
    """
    try:
        data = _get("/portfolio/positions", params={"limit": 1000}, auth=True)
        # Kalshi returns market_positions; field names use _fp (fixed-point) and _dollars suffixes
        positions = data.get("market_positions", data.get("positions", []))
        result = {}
        for p in positions:
            ticker = p.get("ticker", "")
            # position_fp is a string like "4.00"
            qty = float(p.get("position_fp", p.get("position", p.get("quantity", 0))) or 0)
            if not ticker or qty == 0:
                continue
            # market_exposure_dollars = cost paid in dollars (e.g. 1.68)
            # avg price in cents = (exposure_dollars / contracts) * 100
            exposure_dollars = abs(float(p.get("market_exposure_dollars", p.get("market_exposure", 0)) or 0))
            if exposure_dollars and qty > 0:
                avg = (exposure_dollars / qty) * 100   # cents per contract
            else:
                avg = 0
            result[ticker] = {
                "quantity":  int(qty),
                "avg_price": round(avg, 1),  # cents e.g. 42.0
            }
        return result
    except Exception as e:
        print(f"[positions] Error fetching positions: {e}")
        return {}


def place_baseball_order(ticker: str, side: str, contracts: int,
                         price_cents: int) -> dict:
    """
    Place a baseball YES limit order on Kalshi.
    side: "yes" (always YES for home/away win markets)
    Returns the API response dict or raises on error.
    """
    if config.DRY_RUN:
        print(f"[DRY RUN] Baseball order: BUY {contracts}x YES on {ticker} @ {price_cents}¢")
        return {"status": "dry_run", "order": {"order_id": "dry-run"}}

    # Validate inputs
    price_cents = int(price_cents)
    contracts = int(contracts)
    if price_cents < 1 or price_cents > 99:
        raise ValueError(f"Invalid price: {price_cents}¢ (must be 1-99)")
    if contracts < 1:
        raise ValueError(f"Invalid contracts: {contracts} (must be >= 1)")

    body = {
        "ticker":     ticker,
        "action":     "buy",
        "side":       "yes",
        "type":       "limit",
        "count":      contracts,
        "yes_price":  price_cents,
    }
    print(f"[baseball] Placing order: {body}")
    try:
        result = _post("/portfolio/orders", body)
        print(f"[baseball] Order placed OK: {result.get('order', {}).get('order_id', '?')}")
        return result
    except Exception as e:
        print(f"[baseball] Order FAILED for {ticker}: {e}")
        raise


# ── Historical (backtest) ─────────────────────────────────────────────────────

def get_settled_markets(series_ticker: str, limit: int = 500) -> list[dict]:
    """Return settled markets for a series (all brackets, all past days)."""
    return get_markets_for_series(series_ticker, status="settled", limit=limit)


def get_settled_events(series_ticker: str, days: int = 30) -> list[dict]:
    """
    Return the last `days` unique settled event dates, each with its bracket
    markets attached.

    Returns list of dicts:
        {
            "event_ticker": str,
            "date":         datetime.date,
            "actual_high":  float,          # from expiration_value
            "markets":      list[dict],     # sorted brackets
        }
    """
    raw = get_settled_markets(series_ticker, limit=days * 10)

    # Group by event_ticker
    from collections import defaultdict
    grouped = defaultdict(list)
    for m in raw:
        grouped[m["event_ticker"]].append(m)

    events = []
    for event_ticker, mkts in grouped.items():
        # All markets in a day share the same expiration_value
        exp_vals = [m.get("expiration_value") for m in mkts
                    if m.get("expiration_value") not in (None, "")]
        if not exp_vals:
            continue
        actual_high = float(exp_vals[0])

        # Parse date from ticker: KXHIGHNY-26MAR26
        try:
            date_part = event_ticker.split("-", 1)[1]   # "26MAR26"
            date = datetime.strptime(date_part, "%y%b%d").date()
        except (IndexError, ValueError):
            continue

        events.append({
            "event_ticker": event_ticker,
            "date":         date,
            "actual_high":  actual_high,
            "markets":      _sort_brackets(mkts),
        })

    # Sort by date desc, take latest `days`
    events.sort(key=lambda e: e["date"], reverse=True)
    return events[:days]
