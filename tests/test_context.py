"""SYNTHETIC contracts for read-only external observations."""
import json
from io import BytesIO

import pytest

from paper_risk.context import (
    ENERGY_ORIGIN,
    PATTERN_ORIGIN,
    capture,
    fetch_json,
    validate_energy,
    validate_pattern,
)

# SYNTHETIC fixture values; they are not market observations.
SYNTHETIC_PATTERN = {
    "symbol": "BTC", "venue": "Hyperliquid", "asOf": 300_000, "failures": {},
    "frames": {"5m": [
        {"t": 0, "closeTime": 300_000, "o": 1, "h": 2, "l": 1, "c": 2, "v": 3, "closed": True},
        {"t": 300_000, "closeTime": 600_000, "o": 2, "h": 3, "l": 2, "c": 3, "v": 1, "closed": False},
    ]},
}
SYNTHETIC_ENERGY = {
    "country": "nl", "date": "2026-09-26", "servedAt": "2026-09-26T12:00:00Z",
    "priceZone": "NL", "price": {"stale": False, "data": {
        "unit": "EUR/MWh", "timezone": "Europe/Amsterdam", "license": "SYNTHETIC",
        "source": "SYNTHETIC", "data": [{"timestamp": "2026-09-26T12:00:00Z", "values": {"value": 42}}]}},
    "power": {"stale": True, "data": None, "error": "SYNTHETIC outage"},
}


class Response:
    status = 200

    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        return self.body[:size]


def test_synthetic_pattern_keeps_only_closed_asof_candles():
    result = validate_pattern(SYNTHETIC_PATTERN, "BTC")
    assert result["available"] is True
    assert len(result["frames"]["5m"]) == 1
    assert "not a strategy signal" in result["role"]


def test_synthetic_pattern_rejects_identity_and_ohlc_errors():
    bad = json.loads(json.dumps(SYNTHETIC_PATTERN))
    bad["symbol"] = "ETH"
    with pytest.raises(ValueError, match="identity"):
        validate_pattern(bad, "BTC")
    bad = json.loads(json.dumps(SYNTHETIC_PATTERN))
    bad["frames"]["5m"][0]["h"] = 0
    with pytest.raises(ValueError, match="OHLCV"):
        validate_pattern(bad, "BTC")


def test_synthetic_energy_preserves_physical_source_fields_and_missing_feed():
    result = validate_energy(SYNTHETIC_ENERGY, "nl", "2026-09-26")
    assert result["available"] is True
    assert result["feeds"]["price"]["data"]["unit"] == "EUR/MWh"
    assert result["feeds"]["power"]["data"] is None
    assert "not directly tradable" in result["role"]


def test_synthetic_energy_rejects_naive_timestamp_and_wrong_identity():
    bad = json.loads(json.dumps(SYNTHETIC_ENERGY))
    bad["price"]["data"]["data"][0]["timestamp"] = "2026-09-26T12:00:00"
    with pytest.raises(ValueError, match="timezone"):
        validate_energy(bad, "nl", "2026-09-26")
    with pytest.raises(ValueError, match="identity"):
        validate_energy(SYNTHETIC_ENERGY, "de", "2026-09-26")


def test_synthetic_capture_uses_only_gets_and_keeps_observation_separate():
    requested = []
    replies = [json.dumps(SYNTHETIC_PATTERN).encode(), json.dumps(SYNTHETIC_ENERGY).encode()]

    def opener(request, timeout):
        requested.append((request.full_url, request.method, timeout))
        return Response(replies.pop(0))

    result = capture("BTC", "nl", "2026-09-26", "NL", opener=opener,
                     now=lambda: "2026-09-26T12:00:00Z")
    assert result["execution_enabled"] is False
    assert result["order_endpoint_called"] is False
    assert result["pattern_forge"]["frames"]["5m"][0]["c"] == 2
    assert result["energy_monitor"]["feeds"]["price"]["data"]["unit"] == "EUR/MWh"
    assert len(requested) == 2
    assert all(method == "GET" for _, method, _ in requested)
    assert requested[0][0].startswith(PATTERN_ORIGIN + "/api/markets/")
    assert requested[1][0].startswith(ENERGY_ORIGIN + "/api/market?")


def test_synthetic_url_allowlist_and_explicit_calendar_date():
    with pytest.raises(ValueError, match="allowlist"):
        fetch_json("https://127.0.0.1/api/market?country=nl")
    with pytest.raises(ValueError, match="calendar date"):
        capture("BTC", "nl", "2026-99-99", opener=lambda *_a, **_k: pytest.fail("network called"))
    with pytest.raises(ValueError, match="two-letter"):
        capture("BTC", "../../x", "2026-09-26", opener=lambda *_a, **_k: pytest.fail("network called"))


@pytest.mark.parametrize("body", [b'{"price":NaN}', b'{"price":1e999}'])
def test_synthetic_nonfinite_source_json_is_rejected(body):
    with pytest.raises(ValueError, match="invalid JSON"):
        fetch_json(ENERGY_ORIGIN + "/api/market?country=nl", opener=lambda *_a, **_k: Response(body))


def test_synthetic_source_outage_stays_observation_unavailable():
    def offline(*_args, **_kwargs):
        raise OSError("SYNTHETIC offline")

    result = capture("BTC", "nl", "2026-09-26", opener=offline)
    assert result["execution_enabled"] is False
    assert result["pattern_forge"]["available"] is False
    assert result["energy_monitor"]["available"] is False
    assert all("SYNTHETIC" in result[key]["error"] for key in ("pattern_forge", "energy_monitor"))
