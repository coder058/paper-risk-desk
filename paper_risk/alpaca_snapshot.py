"""Optional, strictly read-only Alpaca paper account and IEX market snapshot."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .core import config

# SOURCE: Alpaca paper trading and stock data API reference URLs linked in README.
PAPER_ORIGIN = "https://paper-api.alpaca.markets"
DATA_ORIGIN = "https://data.alpaca.markets"


def _fetch(url: str, key: str, secret: str, opener=urlopen) -> dict:
    if not url.startswith((PAPER_ORIGIN + "/v2/", DATA_ORIGIN + "/v2/")):
        raise ValueError("Only fixed Alpaca read-only API origins are allowed")
    request = Request(url, headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret,
                                    "Accept": "application/json"}, method="GET")
    try:
        # UNCALIBRATED GUESS: ten seconds is an operational timeout, not a market freshness claim.
        with opener(request, timeout=10) as response:
            payload = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"Alpaca read-only request failed: HTTP {error.code}") from None
    if not isinstance(payload, dict):
        raise ValueError("Unexpected Alpaca response")
    return payload


def snapshot(opener=urlopen, now: datetime | None = None) -> dict:
    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        raise RuntimeError("Set ALPACA_API_KEY and ALPACA_SECRET_KEY for a paper read-only snapshot")
    symbol = config()["symbol"]
    account = _fetch(PAPER_ORIGIN + "/v2/account", key, secret, opener)
    clock = _fetch(PAPER_ORIGIN + "/v2/clock", key, secret, opener)
    latest = _fetch(DATA_ORIGIN + f"/v2/stocks/{symbol}/bars/latest?feed=iex", key, secret, opener)
    bar = latest.get("bar")
    if not isinstance(bar, dict) or not isinstance(bar.get("t"), str):
        raise ValueError("Missing latest IEX bar timestamp")
    timestamp = datetime.fromisoformat(bar["t"].replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("Latest IEX bar must have a timezone")
    observed = now or datetime.now(timezone.utc)
    age_seconds = (observed - timestamp).total_seconds()
    return {"mode": "read-only; no order endpoint", "broker": "Alpaca paper",
            "symbol": symbol, "market_data_feed": "IEX only, not consolidated US market",
            "account": {"status": account.get("status"), "equity": account.get("equity"),
                        "buying_power": account.get("buying_power"),
                        "trading_blocked": account.get("trading_blocked")},
            "clock": {"is_open": clock.get("is_open"), "timestamp": clock.get("timestamp")},
            "latest_bar": {"timestamp": bar["t"], "close": bar.get("c"),
                           "observed_age_seconds": round(age_seconds, 3)}}
