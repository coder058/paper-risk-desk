"""Generate and verify a reproducible SYNTHETIC one-minute SPY-shaped tape."""
from __future__ import annotations

import argparse
import csv
import hashlib
import math
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TAPE = Path(__file__).resolve().parents[1] / "fixtures" / "SYNTHETIC_SPY_1m_2026-01-05_2026-02-06.csv"
NY = ZoneInfo("America/New_York")


def rows():
    """Weekday-only laboratory tape; this is NOT an exchange calendar or price history."""
    day = date(2026, 1, 5)  # SOURCE: fixed fixture coverage, deliberately selected lab dates.
    stop = date(2026, 2, 6)  # SOURCE: fixed fixture coverage, one calendar-month span.
    index = 0
    while day <= stop:
        if day.weekday() < 5:
            for minute in range(390):  # SOURCE: US regular session 09:30–16:00, local NY wall clock.
                stamp = datetime.combine(day, time(9, 30), NY) + timedelta(minutes=minute)
                # UNCALIBRATED GUESS: arbitrary deterministic waveform for mechanics testing only.
                mid = 100 + 1.6 * math.sin(index / 23) + 0.9 * math.sin(index / 143) + index * 0.00015
                opening = mid - 0.025 * math.sin(index / 3)
                closing = mid + 0.025 * math.sin(index / 3)
                high = max(opening, closing) + 0.02
                low = min(opening, closing) - 0.02
                yield [stamp.isoformat(), *(f"{x:.5f}" for x in (opening, high, low, closing)), "1000"]
                index += 1
        day += timedelta(days=1)


def content() -> bytes:
    from io import StringIO
    sink = StringIO(newline="")
    writer = csv.writer(sink, lineterminator="\n")
    writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
    writer.writerows(rows())
    return sink.getvalue().encode("utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = content()
    if args.check:
        if not TAPE.exists() or TAPE.read_bytes() != expected:
            raise SystemExit("SYNTHETIC fixture differs from the committed generator")
    else:
        TAPE.parent.mkdir(parents=True, exist_ok=True)
        TAPE.write_bytes(expected)
    print(f"SYNTHETIC fixture: {len(expected)} bytes, SHA-256 {hashlib.sha256(expected).hexdigest()}")


if __name__ == "__main__":
    main()
