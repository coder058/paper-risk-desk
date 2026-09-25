"""Typed observations, two research signals, and fail-closed paper risk rules."""
from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from statistics import fmean, pstdev
from zoneinfo import ZoneInfo

from .fixture import TAPE

NY = ZoneInfo("America/New_York")
CONFIG = Path(__file__).resolve().parents[1] / "config.json"


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def tape(path: Path = TAPE) -> list[Bar]:
    result: list[Bar] = []
    with path.open(newline="", encoding="utf-8") as source:
        for row in csv.DictReader(source):
            bar = Bar(datetime.fromisoformat(row["timestamp"]), *(float(row[x]) for x in ("open", "high", "low", "close")), int(row["volume"]))
            if bar.timestamp.tzinfo is None or (result and bar.timestamp <= result[-1].timestamp):
                raise ValueError("Tape timestamps must be aware and strictly increasing")
            if any(not math.isfinite(x) for x in (bar.open, bar.high, bar.low, bar.close)) or bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
                raise ValueError("Invalid OHLC observation")
            result.append(bar)
    if not result:
        raise ValueError("Empty tape")
    return result


def config() -> dict:
    value = json.loads(CONFIG.read_text(encoding="utf-8"))
    if value["symbol"] != "SPY" or value["starting_cash"] <= 0 or value["shares_per_proposal"] <= 0:
        raise ValueError("Invalid paper config")
    for key, setting in value["risk"].items():
        if key != "rth_only" and (not isinstance(setting, (int, float)) or setting <= 0):
            raise ValueError(f"Invalid risk limit: {key}")
    return value


def signal(name: str, bars: list[Bar], settings: dict) -> str | None:
    """Only closed bars through the current index are visible here."""
    closes = [bar.close for bar in bars]
    if name == "sma_cross":
        fast, slow = settings["fast"], settings["slow"]
        if not 1 <= fast < slow or len(closes) < slow + 1:
            return None
        before = fmean(closes[-fast-1:-1]) - fmean(closes[-slow-1:-1])
        current = fmean(closes[-fast:]) - fmean(closes[-slow:])
        return "buy" if before <= 0 < current else "sell" if before >= 0 > current else None
    if name == "mean_reversion_band":
        window, band = settings["window"], settings["band_std"]
        if window < 2 or band <= 0 or len(closes) < window + 1:
            return None
        # SOURCE: the previous window sets the threshold, then the just-closed bar is compared.
        reference = closes[-window-1:-1]
        mean, spread = fmean(reference), pstdev(reference)
        if spread == 0:
            return None
        return "buy" if closes[-1] < mean - band * spread else "sell" if closes[-1] > mean + band * spread else None
    raise ValueError("Only the two documented strategies are admitted")


@dataclass
class Account:
    cash: float
    shares: int = 0
    peak_equity: float = 0
    day_anchor: float = 0
    day: str = ""
    orders_today: int = 0
    last_order_at: str = ""
    last_equity: float = 0
    halted: bool = False
    halt_reason: str = ""

    def equity(self, price: float) -> float:
        return self.cash + self.shares * price

    def mark(self, price: float, at: datetime, risk: dict) -> str | None:
        equity = self.equity(price)
        date = at.astimezone(NY).date().isoformat()
        if date != self.day:
            # SOURCE: retain prior close equity across a session change, so an overnight gap
            # cannot reset the daily-loss anchor before the first new-session mark.
            self.day, self.day_anchor, self.orders_today = date, self.last_equity or equity, 0
        self.last_equity = equity
        self.peak_equity = max(self.peak_equity, equity)
        if self.halted:
            return self.halt_reason
        if self.day_anchor - equity >= risk["max_daily_loss"]:
            self.halted, self.halt_reason = True, "daily-loss"
        elif self.peak_equity > 0 and (self.peak_equity - equity) / self.peak_equity >= risk["max_peak_drawdown_fraction"]:
            self.halted, self.halt_reason = True, "peak-drawdown"
        return self.halt_reason or None

    def check(self, side: str, qty: int, price: float, at: datetime, risk: dict) -> str | None:
        if self.halted:
            return "HALTED: " + self.halt_reason
        if side not in {"buy", "sell"} or not isinstance(qty, int) or qty <= 0 or price <= 0 or not math.isfinite(price):
            return "invalid-order"
        if qty > risk["max_order_shares"] or qty * price > risk["max_order_notional"]:
            return "order-size"
        if risk["rth_only"]:
            wall = at.astimezone(NY)
            # SOURCE: weekday regular NY session time window. Broker holidays are not inferred.
            if wall.weekday() >= 5 or not time(9, 30) <= wall.time() < time(16):
                return "outside-RTH"
        if self.orders_today >= risk["max_orders_per_day"]:
            return "orders-per-day"
        if self.last_order_at and (at - datetime.fromisoformat(self.last_order_at)).total_seconds() < risk["cooldown_minutes"] * 60:
            return "cooldown"
        if side == "buy" and qty * price > self.cash:
            return "insufficient-cash"
        if side == "sell" and qty > self.shares:
            return "insufficient-shares"
        return None

    def fill_simulated(self, side: str, qty: int, price: float, at: datetime) -> None:
        if side == "buy":
            self.cash -= qty * price
            self.shares += qty
        else:
            self.cash += qty * price
            self.shares -= qty
        self.orders_today += 1
        self.last_order_at = at.isoformat()
