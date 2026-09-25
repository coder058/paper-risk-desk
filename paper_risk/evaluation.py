"""Chronological holdout on a SYNTHETIC fixture; no claim of market edge."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from .core import Account, config, signal, tape
from .fixture import TAPE

BASE = Path(__file__).resolve().parents[1]
EVAL = BASE / "evals" / "SYNTHETIC_holdout.json"


def risk_challenge() -> dict:
    """Six constructed adverse gates; computed independently of strategy results."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    now = datetime(2026, 2, 2, 15, 0, tzinfo=ZoneInfo("America/New_York"))
    rules = config()["risk"]
    def base():
        s = Account(cash=10000, peak_equity=10000, day_anchor=10000, day=now.date().isoformat())
        return s
    cases = []
    s = base(); cases.append(("max-shares", s.check("buy", rules["max_order_shares"] + 1, 100, now, rules)))
    s = base(); cases.append(("max-notional", s.check("buy", rules["max_order_shares"], rules["max_order_notional"], now, rules)))
    s = base(); s.orders_today = rules["max_orders_per_day"]; cases.append(("orders-per-day", s.check("buy", 1, 100, now, rules)))
    s = base(); s.last_order_at = (now - timedelta(minutes=1)).isoformat(); cases.append(("cooldown", s.check("buy", 1, 100, now, rules)))
    s = base(); s.halted, s.halt_reason = True, "daily-loss"; cases.append(("persistent-halt", s.check("buy", 1, 100, now, rules)))
    s = base(); cases.append(("outside-RTH", s.check("buy", 1, 100, now.replace(hour=8), rules)))
    return {"scenario": "SYNTHETIC constructed risk checks", "tested": len(cases),
            "blocked": sum(reason is not None for _, reason in cases),
            "cases": [{"name": name, "decision": reason or "allowed"} for name, reason in cases]}


def evaluate() -> dict:
    cfg, bars = config(), tape()
    days = list(dict.fromkeys(bar.timestamp.date().isoformat() for bar in bars))
    train_days = cfg["walk_forward"]["train_days"]
    if not 1 < train_days < len(days):
        raise ValueError("Holdout boundary is invalid")
    holdout_start = days[train_days]
    boundary = next(i for i, bar in enumerate(bars) if bar.timestamp.date().isoformat() == holdout_start)
    results = {}
    for name, settings in cfg["strategies"].items():
        state = Account(cash=cfg["starting_cash"], peak_equity=cfg["starting_cash"], day_anchor=cfg["starting_cash"])
        proposed = approved = blocked = halts = 0
        maximum_drawdown = 0.0
        for i in range(boundary, len(bars) - 1):
            closed, following = bars[i], bars[i + 1]
            before_halt = state.halted
            state.mark(closed.close, closed.timestamp, cfg["risk"])
            if state.halted and not before_halt:
                halts += 1
            maximum_drawdown = max(maximum_drawdown, (state.peak_equity - state.equity(closed.close)) / state.peak_equity)
            choice = signal(name, bars[:i + 1], settings)
            if not choice or following.timestamp.date().isoformat() != closed.timestamp.date().isoformat():
                continue
            proposed += 1
            qty = cfg["shares_per_proposal"]
            # SOURCE: signal uses bar i close; experimental fill uses bar i+1 open.
            # This auto-approval is research simulation only; interactive desk remains human gated.
            reason = state.check(choice, qty, following.open, following.timestamp, cfg["risk"])
            if reason:
                blocked += 1
            else:
                state.fill_simulated(choice, qty, following.open, following.timestamp)
                approved += 1
        final_equity = state.equity(bars[-1].close)
        # SOURCE: buy-and-hold baseline is one fractional unit of exposure to compare direction,
        # not an executable benchmark or proof of profitability on invented prices.
        baseline_pct = (bars[-1].close / bars[boundary].open - 1) * 100
        results[name] = {"proposals": proposed, "simulated_fills": approved, "blocked": blocked,
                         "halts": halts, "holdout_return_percent": round((final_equity / cfg["starting_cash"] - 1) * 100, 6),
                         "buy_hold_price_change_percent": round(baseline_pct, 6),
                         "max_drawdown_fraction": round(maximum_drawdown, 8)}
    return {"label": "SYNTHETIC laboratory tape; not historical SPY or evidence of trading performance",
            "fixture_sha256": hashlib.sha256(TAPE.read_bytes()).hexdigest(),
            "config_sha256": hashlib.sha256((BASE / "config.json").read_bytes()).hexdigest(),
            "train_dates": [days[0], days[train_days - 1]], "holdout_dates": [holdout_start, days[-1]],
            "train_bars_used_only_for_indicator_warmup": boundary, "holdout_bars": len(bars) - boundary,
            "baseline": "holdout SPY-shaped synthetic price change, same start/end; no fees or slippage modeled",
            "strategies": results, "risk_challenge": risk_challenge()}


def render() -> bytes:
    return (json.dumps(evaluate(), indent=2, sort_keys=True) + "\n").encode("utf-8")


def main(check: bool = False):
    expected = render()
    if check:
        if not EVAL.exists() or EVAL.read_bytes() != expected:
            raise SystemExit("SYNTHETIC holdout regression: committed eval differs")
    else:
        EVAL.parent.mkdir(parents=True, exist_ok=True)
        EVAL.write_bytes(expected)
    report = json.loads(expected)
    print(f"SYNTHETIC holdout {report['holdout_dates']}: {report['risk_challenge']['blocked']}/{report['risk_challenge']['tested']} adverse cases blocked")
    for name, result in report["strategies"].items():
        print(f"{name}: {result['proposals']} signals, {result['simulated_fills']} fixture fills, {result['halts']} halts; max DD {result['max_drawdown_fraction']:.4f}")


if __name__ == "__main__":
    main()
