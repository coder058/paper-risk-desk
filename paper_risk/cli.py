"""No network needed for demo/eval. No implicit order submission."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .core import config, signal, tape
from .desk import Desk
from .evaluation import main as evaluation_main

LOCAL = Path(__file__).resolve().parents[1] / ".local"


def demo():
    bars, cfg = tape(), config()
    LOCAL.mkdir(exist_ok=True)
    # SOURCE: unique run file prevents resetting or silently reusing a prior audit.
    path = LOCAL / ("demo-" + datetime.now().strftime("%Y%m%dT%H%M%S%f") + ".sqlite")
    desk = Desk(path)
    try:
        for i, closed in enumerate(bars):
            desk.mark(closed)
            choice = signal("sma_cross", bars[:i + 1], cfg["strategies"]["sma_cross"])
            if choice == "buy" and i + 1 < len(bars):
                identifier = desk.propose("sma_cross", choice, closed, cfg["shares_per_proposal"])
                print(json.dumps({"label": "SYNTHETIC fixture, proposed only", "database": str(path),
                                  "proposal_id": identifier, "signal_bar_closed": closed.timestamp.isoformat(),
                                  "next_bar_for_approval": bars[i + 1].timestamp.isoformat(), "desk_status": "awaiting human",
                                  "account": {"equity_at_close": desk.account().equity(closed.close),
                                              "halted": desk.account().halted},
                                  "proposals": desk.proposals(),
                                  "next_command": f"python -m paper_risk.cli approve {identifier} --db {path}"}, indent=2))
                return
        raise RuntimeError("No demo signal in the fixed fixture")
    finally:
        desk.close()


def main():
    parser = argparse.ArgumentParser(description="Paper risk desk, default fixture-only")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo")
    sub.add_parser("paper-snapshot", help="Read-only Alpaca paper account and IEX bar; never submits orders")
    ev = sub.add_parser("eval"); ev.add_argument("--check", action="store_true")
    for name in ("status", "approve", "reject", "halt", "resume", "audit"):
        command = sub.add_parser(name)
        command.add_argument("--db", type=Path, required=True)
        if name in ("approve", "reject"):
            command.add_argument("proposal_id")
        if name in ("reject", "halt", "resume"):
            command.add_argument("--reason", required=True)
    args = parser.parse_args()
    if args.command == "demo":
        return demo()
    if args.command == "paper-snapshot":
        from .alpaca_snapshot import snapshot
        print(json.dumps(snapshot(), indent=2))
        return
    if args.command == "eval":
        return evaluation_main(args.check)
    if not args.db.is_file():
        raise SystemExit("Existing desk database required; run demo first")
    desk = Desk(args.db)
    try:
        if args.command == "status":
            print(json.dumps({"account": desk.account().__dict__, "proposals": desk.proposals()}, indent=2))
        elif args.command == "audit":
            print(json.dumps(desk.events(), indent=2))
        elif args.command == "reject":
            desk.reject(args.proposal_id, args.reason); print("rejected")
        elif args.command == "halt":
            desk.halt(args.reason); print("HALTED")
        elif args.command == "resume":
            desk.resume(args.reason); print("RESUMED")
        elif args.command == "approve":
            proposal = next((p for p in desk.proposals() if p["id"] == args.proposal_id), None)
            if not proposal:
                raise ValueError("Unknown proposal")
            following = next((b for b in tape() if b.timestamp > datetime.fromisoformat(proposal["signal_at"])), None)
            if not following:
                raise ValueError("No later fixture bar")
            print(desk.approve(args.proposal_id, following))
    finally:
        desk.close()


if __name__ == "__main__":
    main()
