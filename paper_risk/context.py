"""Read-only snapshots from Pattern Forge and Energy Monitor.

These observations are kept outside the strategy, risk and execution paths.
This command makes bounded GET requests only; it never persists or submits orders.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

# SOURCE: public Pattern Forge API allowlist in app/marketApi.ts.
PATTERN_ORIGIN = "https://pattern-forge-five.vercel.app"
# SOURCE: public Energy Monitor origin and API routes.
ENERGY_ORIGIN = "https://energy-monitor-jordi.jlpmccs.chatgpt.site"
# SOURCE: Energy Monitor provider.ts MAX_BYTES; bounds this optional context read.
MAX_BODY_BYTES = 2_000_000
# SOURCE: Pattern Forge upstream timeout; local request bound, not a freshness SLA.
HTTP_TIMEOUT_SECONDS = 15
# SOURCE: Pattern Forge public market API allowlist.
PATTERN_SYMBOLS = {"BTC", "ETH", "SOL"}
# SOURCE: Pattern Forge API timeframe definitions.
INTERVAL_MS = {"5m": 300_000, "30m": 1_800_000, "1h": 3_600_000,
               "4h": 14_400_000, "1d": 86_400_000}


def received_at() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _NoRedirect(HTTPRedirectHandler):
    """Reject redirects so fixed-origin requests cannot be redirected elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _allowed_url(url: str) -> bool:
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "https" or parsed.username or parsed.password or port:
        return False
    if parsed.netloc == urlsplit(PATTERN_ORIGIN).netloc and parsed.path.startswith("/api/markets/"):
        return not parsed.query and not parsed.fragment
    if parsed.netloc == urlsplit(ENERGY_ORIGIN).netloc:
        return (parsed.path == "/api/market" and bool(parsed.query) and not parsed.fragment) or (
            parsed.path == "/api/gas" and bool(parsed.query) and not parsed.fragment
        )
    return False


def fetch_json(url: str, opener=None, now=received_at) -> tuple[object, dict]:
    """Read one allowlisted JSON URL and retain local receipt/response evidence."""
    if not _allowed_url(url):
        raise ValueError("URL is outside the fixed read-only source allowlist")
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "paper-risk-desk-context/0.1"}, method="GET")
    if opener is None:
        opener = build_opener(_NoRedirect()).open
    started = time.perf_counter()
    try:
        with opener(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise ValueError(f"Source returned HTTP {status}")
            body = response.read(MAX_BODY_BYTES + 1)
    except HTTPError as exc:
        raise ValueError(f"Source returned HTTP {exc.code}") from None
    except URLError as exc:
        raise ValueError(f"Source request failed: {exc.reason}") from None
    if len(body) > MAX_BODY_BYTES:
        raise ValueError("Source response exceeds the documented body bound")
    def finite_float(raw: str) -> float:
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("Source JSON contains a non-finite number")
        return value

    def reject_constant(_raw: str):
        raise ValueError("Source JSON contains a non-finite number")

    try:
        payload = json.loads(body, parse_float=finite_float, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("Source returned invalid JSON") from None
    return payload, {"source_url": url, "received_at": now(),
                     "request_elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                     "response_sha256": hashlib.sha256(body).hexdigest()}


def validate_pattern(payload: object, symbol: str) -> dict:
    if symbol not in PATTERN_SYMBOLS:
        raise ValueError("Choose BTC, ETH or SOL")
    if not isinstance(payload, dict) or payload.get("symbol") != symbol or payload.get("venue") != "Hyperliquid":
        raise ValueError("Pattern Forge response identity or venue does not match")
    as_of = payload.get("asOf")
    if not isinstance(as_of, int) or isinstance(as_of, bool) or as_of < 0:
        raise ValueError("Pattern Forge response has no valid as-of time")
    frames, failures = payload.get("frames"), payload.get("failures", {})
    if not isinstance(frames, dict) or not isinstance(failures, dict):
        raise ValueError("Pattern Forge response has invalid frame metadata")
    clean_frames, gaps = {}, {}
    for interval, bars in frames.items():
        if interval not in INTERVAL_MS or not isinstance(bars, list):
            raise ValueError("Pattern Forge returned an unsupported interval")
        rows = {}
        for bar in bars:
            if not isinstance(bar, dict):
                raise ValueError("Malformed Pattern Forge candle")
            start, end = bar.get("t"), bar.get("closeTime")
            if (not isinstance(start, int) or isinstance(start, bool) or
                    not isinstance(end, int) or isinstance(end, bool)):
                raise ValueError("Candle timestamps must be integer UTC milliseconds")
            if end != start + INTERVAL_MS[interval]:
                raise ValueError("Candle interval does not match its timestamps")
            if bar.get("closed") is not True or end > as_of:
                continue
            values = [bar.get(key) for key in ("o", "h", "l", "c", "v")]
            if any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
                   for value in values):
                raise ValueError("Candle contains a non-finite OHLCV value")
            o, h, low, c, volume = values
            if low <= 0 or h < max(o, c, low) or low > min(o, c) or volume < 0:
                raise ValueError("Candle OHLCV bounds are inconsistent")
            row = {"t": start, "closeTime": end, "o": o, "h": h, "l": low,
                   "c": c, "v": volume, "closed": True}
            if start in rows and rows[start] != row:
                raise ValueError("Conflicting candles share a timestamp")
            rows[start] = row
        ordered = [rows[stamp] for stamp in sorted(rows)]
        if ordered:
            clean_frames[interval] = ordered
            gaps[interval] = sum(b["t"] - a["t"] != INTERVAL_MS[interval]
                                 for a, b in zip(ordered, ordered[1:]))
    if not clean_frames:
        raise ValueError("No closed Pattern Forge candles are available")
    return {"available": True, "venue": "Hyperliquid", "symbol": symbol, "as_of_ms": as_of,
            "frames": clean_frames, "upstream_failures": failures, "gap_count": gaps,
            "role": "cross-venue observation only; not a strategy signal or Alpaca order input"}


def validate_energy(payload: object, country: str, day: str) -> dict:
    if not isinstance(payload, dict) or payload.get("country") != country or payload.get("date") != day:
        raise ValueError("Energy Monitor response identity does not match the request")
    feeds = {}
    for name in ("price", "power"):
        feed = payload.get(name)
        if not isinstance(feed, dict):
            raise ValueError(f"Energy Monitor {name} feed is missing")
        source_data = feed.get("data")
        if source_data is not None:
            if not isinstance(source_data, dict) or not isinstance(source_data.get("data"), list):
                raise ValueError(f"Energy Monitor {name} schema is invalid")
            for row in source_data["data"]:
                if not isinstance(row, dict) or not isinstance(row.get("timestamp"), str) or not isinstance(row.get("values"), dict):
                    raise ValueError(f"Energy Monitor {name} contains a malformed observation")
                try:
                    stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                except ValueError:
                    raise ValueError(f"Energy Monitor {name} has an invalid timestamp") from None
                if stamp.tzinfo is None or stamp.utcoffset() is None:
                    raise ValueError(f"Energy Monitor {name} timestamp has no timezone")
        # Preserve source, units, timezone, licence and stale/error metadata verbatim.
        feeds[name] = feed
    return {"available": True, "country": country, "date": day, "served_at": payload.get("servedAt"),
            "price_zone": payload.get("priceZone"), "feeds": feeds,
            "role": "physical-grid observation only; not directly tradable via Alpaca"}


def capture(symbol: str, country: str, day: str, zone: str | None = None,
            gas_point: str | None = None, opener=None, now=received_at) -> dict:
    if symbol not in PATTERN_SYMBOLS:
        raise ValueError("Choose BTC, ETH or SOL")
    if not re.fullmatch(r"[a-z]{2}", country):
        raise ValueError("Use a lowercase two-letter country code")
    # SOURCE: ISO 8601 calendar date format; explicit local delivery day avoids guessing.
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("Use an explicit YYYY-MM-DD delivery date")
    try:
        if datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") != day:
            raise ValueError
    except ValueError:
        raise ValueError("Delivery date is not a calendar date") from None
    pattern_url = f"{PATTERN_ORIGIN}/api/markets/{symbol}"
    energy_query = {"country": country, "date": day}
    if zone:
        energy_query["zone"] = zone
    energy_url = f"{ENERGY_ORIGIN}/api/market?{urlencode(energy_query)}"
    result = {"schema": "paper-risk-desk.read-only-context.v1", "captured_at": now(),
              "execution_enabled": False, "order_endpoint_called": False}
    for name, url, validator, args in (
        ("pattern_forge", pattern_url, validate_pattern, (symbol,)),
        ("energy_monitor", energy_url, validate_energy, (country, day)),
    ):
        try:
            payload, provenance = fetch_json(url, opener, now)
            result[name] = {**validator(payload, *args), **provenance}
        except (ValueError, OSError) as exc:
            result[name] = {"available": False, "error": str(exc), "source_url": url}
    if gas_point:
        gas_url = f"{ENERGY_ORIGIN}/api/gas?{urlencode({'point': gas_point})}"
        try:
            payload, provenance = fetch_json(gas_url, opener, now)
            if not isinstance(payload, dict) or payload.get("point") != gas_point:
                raise ValueError("Energy Monitor gas identity does not match")
            result["gas_context"] = {"feed": payload, "role": "physical-market observation only", **provenance}
        except (ValueError, OSError) as exc:
            result["gas_context"] = {"available": False, "error": str(exc), "source_url": gas_url}
    return result
