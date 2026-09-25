"""Regression checks for fixture provenance, human gate, and fail-closed risk."""
from dataclasses import replace
from datetime import timedelta
from datetime import datetime, timezone
from io import BytesIO
import json

import pytest

from paper_risk.core import Account, config, signal, tape
from paper_risk.desk import Desk
from paper_risk.evaluation import evaluate, render
from paper_risk.fixture import TAPE, content
from paper_risk.alpaca_snapshot import snapshot


@pytest.fixture(scope="module")
def bars():
    return tape()


def test_synthetic_fixture_is_reproducible(bars):
    assert TAPE.read_bytes() == content()
    assert len(bars) > 5_000
    assert all(bar.timestamp.tzinfo is not None for bar in bars)


@pytest.mark.parametrize("name", ["sma_cross", "mean_reversion_band"])
def test_signal_cannot_see_future_bars(bars, name):
    settings = config()["strategies"][name]
    past = bars[:100]
    observed = signal(name, past, settings)
    changed_future = bars[:100] + [replace(b, close=b.close * 20) for b in bars[100:110]]
    assert signal(name, changed_future[:100], settings) == observed


def test_proposal_needs_human_and_next_bar(tmp_path, bars):
    path = tmp_path / "desk.sqlite"
    desk = Desk(path)
    proposal = desk.propose("sma_cross", "buy", bars[100], 2)
    assert desk.propose("sma_cross", "buy", bars[100], 2) == proposal
    assert desk.proposals()[0]["status"] == "proposed"
    assert desk.account().shares == 0
    with pytest.raises(ValueError, match="later bar"):
        desk.approve(proposal, bars[100])
    with pytest.raises(ValueError, match="SYNTHETIC fixture"):
        desk.approve(proposal, bars[101], mode="paper")
    with pytest.raises(ValueError, match="next committed fixture bar"):
        desk.approve(proposal, replace(bars[101], open=1))
    assert desk.approve(proposal, bars[101]) == "sim-filled"
    assert desk.account().shares == 2
    assert [event["action"] for event in desk.events()] == ["propose", "sim-fill"]
    desk.close()


def test_halt_persists_and_blocks_until_reasoned_resume(tmp_path, bars):
    path = tmp_path / "halt.sqlite"
    desk = Desk(path)
    proposal = desk.propose("sma_cross", "buy", bars[100], 2)
    desk.halt("operator stop")
    desk.close()
    reopened = Desk(path)
    assert reopened.account().halted
    assert reopened.approve(proposal, bars[101]).startswith("blocked: HALTED")
    with pytest.raises(ValueError, match="human reason"):
        reopened.resume(" ")
    reopened.resume("review complete")
    assert not reopened.account().halted
    assert reopened.proposals()[0]["status"] == "blocked"
    assert [event["action"] for event in reopened.events()] == ["propose", "halt", "block", "resume"]
    reopened.close()


def test_loss_halt_survives_session_change(bars):
    rules = config()["risk"]
    state = Account(cash=10_000, shares=10, peak_equity=11_000,
                    day_anchor=11_000, day="2026-01-05", last_equity=11_000)
    next_day = bars[0].timestamp + timedelta(days=1)
    assert state.mark(80, next_day, rules) == "daily-loss"
    assert state.halted
    assert state.check("buy", 1, 80, next_day, rules).startswith("HALTED")


def test_drawdown_halt_and_audit_survive_reopen(tmp_path, bars):
    rules = {**config()["risk"], "max_daily_loss": 10_000}  # SOURCE: isolate the drawdown rule in this SYNTHETIC test.
    state = Account(cash=10_000, shares=10, peak_equity=11_000,
                    day_anchor=11_000, day=bars[0].timestamp.date().isoformat(), last_equity=11_000)
    assert state.mark(40, bars[0].timestamp, rules) == "peak-drawdown"
    path = tmp_path / "auto-halt.sqlite"
    desk = Desk(path)
    proposal = desk.propose("sma_cross", "buy", bars[100], 2)
    assert desk.approve(proposal, bars[101]) == "sim-filled"
    desk.mark(replace(bars[102], close=1))  # SYNTHETIC stress mark, not a market observation.
    assert desk.account().halted
    desk.close()
    reopened = Desk(path)
    assert reopened.account().halted
    assert any(event["action"] == "halt" and event["reason"] == "daily-loss" for event in reopened.events())
    reopened.close()


def test_committed_holdout_and_adverse_gates():
    from paper_risk.evaluation import EVAL
    assert EVAL.read_bytes() == render()
    report = evaluate()
    assert report["risk_challenge"]["blocked"] == report["risk_challenge"]["tested"] == 6
    assert report["train_dates"][1] < report["holdout_dates"][0]


def test_optional_paper_snapshot_is_get_only_and_reports_age(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "SYNTHETIC-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "SYNTHETIC-secret")
    seen = []

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    def opener(request, timeout):
        seen.append((request.full_url, request.get_method()))
        if request.full_url.endswith("/account"):
            data = {"status": "ACTIVE", "equity": "10000", "buying_power": "10000", "trading_blocked": False}
        elif request.full_url.endswith("/clock"):
            data = {"is_open": True, "timestamp": "2026-02-02T15:00:00Z"}
        else:
            data = {"bar": {"t": "2026-02-02T14:59:00Z", "c": 100.0}}
        return Response(json.dumps(data).encode())

    observed = snapshot(opener, now=datetime(2026, 2, 2, 15, 0, tzinfo=timezone.utc))
    assert observed["latest_bar"]["observed_age_seconds"] == 60
    assert observed["mode"] == "read-only; no order endpoint"
    assert len(seen) == 3 and all(method == "GET" for _, method in seen)
    assert all("paper-api.alpaca.markets" in url or "data.alpaca.markets" in url for url, _ in seen)
    assert not any("order" in url for url, _ in seen)


def test_optional_paper_snapshot_requires_keys(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="Set ALPACA_API_KEY"):
        snapshot()
