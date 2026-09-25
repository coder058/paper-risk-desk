"""Human gate and append-only SQLite audit for fixture proposals."""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from .core import Account, Bar, config, tape


class Desk:
    def __init__(self, path: Path | str = ":memory:"):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS account (id INTEGER PRIMARY KEY CHECK(id=1), state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY, strategy TEXT NOT NULL, side TEXT NOT NULL,
                qty INTEGER NOT NULL, signal_at TEXT NOT NULL, status TEXT NOT NULL,
                fill_at TEXT, fill_price REAL
            );
            CREATE TABLE IF NOT EXISTS audit (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                action TEXT NOT NULL, proposal_id TEXT, reason TEXT NOT NULL, details TEXT NOT NULL
            );
        """)
        if not self.db.execute("SELECT id FROM account WHERE id=1").fetchone():
            start = config()["starting_cash"]
            account = Account(cash=start, peak_equity=start, day_anchor=start, last_equity=start)
            self._save(account)
            self.db.commit()

    def close(self):
        self.db.close()

    def _state(self) -> Account:
        return Account(**json.loads(self.db.execute("SELECT state FROM account WHERE id=1").fetchone()[0]))

    def _save(self, state: Account):
        self.db.execute("INSERT INTO account(id,state) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state", (json.dumps(asdict(state), sort_keys=True),))

    def _event(self, action: str, proposal_id: str | None, reason: str, details: dict | None = None):
        self.db.execute("INSERT INTO audit(at,action,proposal_id,reason,details) VALUES(?,?,?,?,?)",
                        (datetime.now().astimezone().isoformat(), action, proposal_id, reason, json.dumps(details or {}, sort_keys=True)))

    def account(self) -> Account:
        return self._state()

    def events(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM audit ORDER BY seq")]

    def proposals(self) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM proposals ORDER BY signal_at,id")]

    def mark(self, bar: Bar):
        with self.db:
            state = self._state()
            before = state.halted
            state.mark(bar.close, bar.timestamp, config()["risk"])
            self._save(state)
            if state.halted and not before:
                self._event("halt", None, state.halt_reason, {"equity": state.equity(bar.close), "source": "SYNTHETIC fixture"})

    def propose(self, strategy: str, side: str, bar: Bar, qty: int) -> str:
        if strategy not in config()["strategies"] or side not in ("buy", "sell") or not isinstance(qty, int) or qty <= 0:
            raise ValueError("Invalid strategy, side or quantity")
        if bar not in tape():
            raise ValueError("Fixture proposal must use a committed observation")
        # SOURCE: stable ID makes replay idempotent for the same closed-bar signal.
        proposal_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"SYNTHETIC/SPY/{strategy}/{side}/{bar.timestamp.isoformat()}"))
        if self._state().halted:
            with self.db:
                self._event("blocked-proposal", proposal_id, "HALTED")
            raise ValueError("Desk is HALTED")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO proposals(id,strategy,side,qty,signal_at,status) VALUES(?,?,?,?,?,?)",
                            (proposal_id, strategy, side, qty, bar.timestamp.isoformat(), "proposed"))
            if self.db.execute("SELECT changes()").fetchone()[0]:
                self._event("propose", proposal_id, "awaiting-human", {"closed_bar": bar.timestamp.isoformat(), "source": "SYNTHETIC fixture"})
        return proposal_id

    def reject(self, proposal_id: str, reason: str):
        if not reason.strip():
            raise ValueError("Rejection needs a reason")
        with self.db:
            changed = self.db.execute("UPDATE proposals SET status='rejected' WHERE id=? AND status='proposed'", (proposal_id,)).rowcount
            if not changed:
                raise ValueError("Proposal is missing or already resolved")
            self._event("reject", proposal_id, reason.strip())

    def halt(self, reason: str):
        if not reason.strip():
            raise ValueError("HALT needs a reason")
        with self.db:
            state = self._state()
            state.halted, state.halt_reason = True, reason.strip()
            self._save(state)
            self._event("halt", None, reason.strip(), {"actor": "human"})

    def resume(self, reason: str):
        if not reason.strip():
            raise ValueError("Resume needs a human reason")
        with self.db:
            state = self._state()
            if not state.halted:
                raise ValueError("Desk is not HALTED")
            previous = state.halt_reason
            state.halted, state.halt_reason = False, ""
            self._save(state)
            self._event("resume", None, reason.strip(), {"actor": "human", "prior_halt": previous})

    def approve(self, proposal_id: str, next_bar: Bar, mode: str = "sim") -> str:
        if mode != "sim":
            raise ValueError("SYNTHETIC fixture cannot authorize an Alpaca paper order")
        with self.db:
            row = self.db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
            if not row or row["status"] != "proposed":
                raise ValueError("Proposal is missing or already resolved")
            if next_bar.timestamp <= datetime.fromisoformat(row["signal_at"]):
                raise ValueError("Approval needs a later bar; same-bar fill is look-ahead")
            observations = tape()
            signal_index = next((index for index, bar in enumerate(observations[:-1])
                                 if bar.timestamp.isoformat() == row["signal_at"]), None)
            if signal_index is None:
                raise ValueError("No same-session next fixture bar")
            expected = observations[signal_index + 1]
            if expected.timestamp.date() != observations[signal_index].timestamp.date():
                raise ValueError("No same-session next fixture bar")
            if next_bar != expected:
                raise ValueError("Approval must use the next committed fixture bar")
            state = self._state()
            was_halted = state.halted
            state.mark(next_bar.open, next_bar.timestamp, config()["risk"])
            reason = state.check(row["side"], row["qty"], next_bar.open, next_bar.timestamp, config()["risk"])
            self._save(state)
            if state.halted and not was_halted:
                self._event("halt", None, state.halt_reason, {"equity": state.equity(next_bar.open)})
            if reason:
                self.db.execute("UPDATE proposals SET status='blocked' WHERE id=?", (proposal_id,))
                self._event("block", proposal_id, reason, {"next_bar": next_bar.timestamp.isoformat()})
                return "blocked: " + reason
            if mode == "sim":
                state.fill_simulated(row["side"], row["qty"], next_bar.open, next_bar.timestamp)
                self._save(state)
                self.db.execute("UPDATE proposals SET status='sim-filled',fill_at=?,fill_price=? WHERE id=?",
                                (next_bar.timestamp.isoformat(), next_bar.open, proposal_id))
                self._event("sim-fill", proposal_id, "human-approved", {"price": next_bar.open, "source": "SYNTHETIC fixture"})
                return "sim-filled"
