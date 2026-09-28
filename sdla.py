#!/usr/bin/env python3
"""Service desk log analyser.

Loads a ticket export (CSV) into SQLite and writes a Markdown report a service
desk lead can use each week:

* volume by category
* SLA performance by priority (breaches and median resolution time)
* categories that are suddenly busier than usual (problem-management candidates)
* open tickets that have already breached their SLA

Standard library only. Caller names and contact details are dropped when the
file is loaded and never reach the database or the report.

Usage:
    python sdla.py tickets.csv --as-of 2026-09-28 --out report.md
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

__version__ = "1.0.0"

# Calendar-hour SLA targets used when the export has no sla_hours column.
DEFAULT_SLA_HOURS = {"P1": 4.0, "P2": 8.0, "P3": 24.0, "P4": 72.0}
PRIORITY_WORDS = {"CRITICAL": "P1", "HIGH": "P2", "MEDIUM": "P3", "MODERATE": "P3", "LOW": "P4"}
REQUIRED_COLUMNS = {"ticket_id", "opened", "priority", "category"}
# Personal data that is removed at load time.
DROPPED_COLUMNS = {"caller_name", "caller_email", "caller_phone"}
DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M",
    "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d", "%d/%m/%Y",
)


class InputError(ValueError):
    """Raised when a value in the ticket export cannot be used."""


@dataclass
class Ticket:
    ticket_id: str
    opened: datetime
    resolved: datetime | None
    priority: str
    category: str
    sla_hours: float

    @property
    def resolution_hours(self) -> float | None:
        if self.resolved is None:
            return None
        return (self.resolved - self.opened).total_seconds() / 3600

    @property
    def breached(self) -> bool | None:
        hours = self.resolution_hours
        return None if hours is None else hours > self.sla_hours


@dataclass
class LoadResult:
    tickets: list[Ticket] = field(default_factory=list)
    skipped: list[tuple[int, str]] = field(default_factory=list)   # (line number, reason)


def parse_dt(value: str) -> datetime | None:
    """Parse the date formats common in ServiceNow and GLPI exports."""
    value = (value or "").strip()
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise InputError(f"unrecognised date/time {value!r}")


def normalise_priority(value: str) -> str:
    """Map 'P1', '1', '1 - Critical' or 'Critical' to P1..P4."""
    v = (value or "").strip().upper()
    match = re.match(r"^P?\s*([1-4])\b", v)
    if match:
        return f"P{match.group(1)}"
    for word, priority in PRIORITY_WORDS.items():
        if word in v:
            return priority
    raise InputError(f"unknown priority {value!r}")


def normalise_category(value: str) -> str:
    cleaned = " ".join((value or "").split())
    return cleaned or "Uncategorised"


def load_tickets(path: str | Path) -> LoadResult:
    """Read the CSV export. Bad rows are skipped and reported, not fatal."""
    result = LoadResult()
    seen: set[str] = set()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
        missing = REQUIRED_COLUMNS - headers
        if missing:
            raise InputError("missing required column(s): " + ", ".join(sorted(missing)))
        for line_no, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items() if k}
            for column in DROPPED_COLUMNS:
                row.pop(column, None)
            ticket_id = row.get("ticket_id", "")
            if not ticket_id:
                result.skipped.append((line_no, "no ticket_id"))
                continue
            if ticket_id in seen:
                result.skipped.append((line_no, f"duplicate ticket {ticket_id}"))
                continue
            try:
                opened = parse_dt(row.get("opened", ""))
                if opened is None:
                    raise InputError("no opened date")
                resolved = parse_dt(row.get("resolved", ""))
                if resolved is not None and resolved < opened:
                    raise InputError("resolved before opened")
                priority = normalise_priority(row.get("priority", ""))
                sla_raw = row.get("sla_hours", "")
                sla_hours = float(sla_raw) if sla_raw else DEFAULT_SLA_HOURS[priority]
            except (InputError, ValueError) as exc:
                result.skipped.append((line_no, f"{ticket_id}: {exc}"))
                continue
            seen.add(ticket_id)
            result.tickets.append(Ticket(ticket_id, opened, resolved, priority,
                                         normalise_category(row.get("category", "")), sla_hours))
    return result


def build_db(tickets: list[Ticket], db_path: str = ":memory:") -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("DROP TABLE IF EXISTS tickets")
    con.execute("""
        CREATE TABLE tickets (
            ticket_id        TEXT PRIMARY KEY,
            opened           TEXT NOT NULL,
            resolved         TEXT,
            priority         TEXT NOT NULL,
            category         TEXT NOT NULL,
            sla_hours        REAL NOT NULL,
            resolution_hours REAL,
            breached         INTEGER
        )""")
    con.executemany(
        "INSERT INTO tickets VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(t.ticket_id, t.opened.isoformat(sep=" "), t.resolved.isoformat(sep=" ") if t.resolved else None,
          t.priority, t.category, t.sla_hours, t.resolution_hours,
          None if t.breached is None else int(t.breached)) for t in tickets])
    con.commit()
    return con


# ------------------------------------------------------------------ analysis
def volume_by_category(con: sqlite3.Connection) -> list[tuple[str, int]]:
    return con.execute(
        "SELECT category, COUNT(*) AS n FROM tickets GROUP BY category ORDER BY n DESC, category").fetchall()


@dataclass
class PrioritySla:
    priority: str
    total: int
    resolved: int
    breached: int
    median_hours: float | None
    target_hours: float

    @property
    def breach_rate(self) -> float | None:
        return None if self.resolved == 0 else self.breached / self.resolved


def sla_by_priority(con: sqlite3.Connection) -> list[PrioritySla]:
    rows = con.execute("""
        SELECT priority, COUNT(*), COUNT(resolved), COALESCE(SUM(breached), 0), MIN(sla_hours)
        FROM tickets GROUP BY priority ORDER BY priority""").fetchall()
    out = []
    for priority, total, resolved, breached, target in rows:
        hours = [h for (h,) in con.execute(
            "SELECT resolution_hours FROM tickets WHERE priority = ? AND resolution_hours IS NOT NULL",
            (priority,))]
        out.append(PrioritySla(priority, total, resolved, breached,
                               statistics.median(hours) if hours else None, target))
    return out


@dataclass
class Spike:
    category: str
    this_week: int
    baseline_avg: float

    @property
    def ratio(self) -> float:
        return float("inf") if self.baseline_avg == 0 else self.this_week / self.baseline_avg


def weekly_spikes(con: sqlite3.Connection, as_of: datetime, baseline_weeks: int = 4,
                  factor: float = 2.0, min_count: int = 5) -> list[Spike]:
    """Categories whose volume in the last 7 days is above factor x their recent weekly average."""
    week_start = as_of - timedelta(days=7)
    base_start = week_start - timedelta(days=7 * baseline_weeks)
    rows = con.execute("""
        SELECT category,
               SUM(CASE WHEN opened >= :ws AND opened < :asof THEN 1 ELSE 0 END),
               SUM(CASE WHEN opened >= :bs AND opened < :ws THEN 1 ELSE 0 END)
        FROM tickets GROUP BY category""",
        {"ws": week_start.isoformat(sep=" "), "asof": as_of.isoformat(sep=" "),
         "bs": base_start.isoformat(sep=" ")}).fetchall()
    spikes = []
    for category, this_week, baseline_total in rows:
        avg = (baseline_total or 0) / baseline_weeks
        if this_week >= min_count and (avg == 0 or this_week > factor * avg):
            spikes.append(Spike(category, this_week, avg))
    return sorted(spikes, key=lambda s: s.ratio, reverse=True)


@dataclass
class OpenBreach:
    ticket_id: str
    priority: str
    category: str
    age_hours: float
    sla_hours: float


def open_breaches(con: sqlite3.Connection, as_of: datetime) -> list[OpenBreach]:
    out = []
    for tid, opened, priority, category, sla in con.execute(
            "SELECT ticket_id, opened, priority, category, sla_hours FROM tickets "
            "WHERE resolved IS NULL AND opened < ?", (as_of.isoformat(sep=" "),)):
        age = (as_of - datetime.fromisoformat(opened)).total_seconds() / 3600
        if age > sla:
            out.append(OpenBreach(tid, priority, category, age, sla))
    return sorted(out, key=lambda b: (b.priority, -b.age_hours))


# ------------------------------------------------------------------ report
def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _hours(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f} h"


def render_report(con: sqlite3.Connection, as_of: datetime, source: str, skipped: list[tuple[int, str]],
                  baseline_weeks: int = 4, factor: float = 2.0, min_count: int = 5) -> str:
    total, first, last = con.execute("SELECT COUNT(*), MIN(opened), MAX(opened) FROM tickets").fetchone()
    open_count = con.execute("SELECT COUNT(*) FROM tickets WHERE resolved IS NULL").fetchone()[0]
    lines = [
        "# Service desk report",
        "",
        f"Source: `{source}`  ",
        f"Report as of: {as_of:%Y-%m-%d %H:%M}  ",
        f"Tickets analysed: {total} (opened {first[:10] if first else 'n/a'} to {last[:10] if last else 'n/a'}); "
        f"{open_count} still open.",
        "",
    ]
    if skipped:
        lines += [f"Rows skipped: {len(skipped)}. First few: " +
                  "; ".join(f"line {n}: {why}" for n, why in skipped[:5]), ""]

    lines += ["## Volume by category", "", "| Category | Tickets | Share |", "|---|---:|---:|"]
    for category, n in volume_by_category(con):
        lines.append(f"| {category} | {n} | {n / total * 100:.1f}% |")

    lines += ["", "## SLA by priority", "",
              "| Priority | Target | Tickets | Resolved | Breached | Breach rate | Median resolution |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for s in sla_by_priority(con):
        lines.append(f"| {s.priority} | {s.target_hours:g} h | {s.total} | {s.resolved} | {s.breached} | "
                     f"{_pct(s.breach_rate)} | {_hours(s.median_hours)} |")

    spikes = weekly_spikes(con, as_of, baseline_weeks, factor, min_count)
    lines += ["", f"## Busier than usual (last 7 days vs previous {baseline_weeks} weeks)", ""]
    if spikes:
        lines += ["| Category | Last 7 days | Weekly average before | Change |", "|---|---:|---:|---:|"]
        for s in spikes:
            change = "new" if s.baseline_avg == 0 else f"{s.ratio:.1f}x"
            lines.append(f"| {s.category} | {s.this_week} | {s.baseline_avg:.1f} | {change} |")
        lines += ["", "These are candidates for a problem record: look for a shared cause before the next spike."]
    else:
        lines.append(f"No category is above {factor:g}x its recent average.")

    breaches = open_breaches(con, as_of)
    lines += ["", "## Open tickets already past SLA", ""]
    if breaches:
        lines += ["| Ticket | Priority | Category | Open for | SLA |", "|---|---|---|---:|---:|"]
        for b in breaches:
            lines.append(f"| {b.ticket_id} | {b.priority} | {b.category} | {b.age_hours:.0f} h | {b.sla_hours:g} h |")
    else:
        lines.append("None.")
    lines += ["", f"_Generated by sdla {__version__}. SLA measured in calendar hours._", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Weekly service desk report from a ticket CSV export.")
    parser.add_argument("csv", help="ticket export (CSV)")
    parser.add_argument("--out", help="write the Markdown report here (default: print it)")
    parser.add_argument("--db", default=":memory:", help="keep the SQLite database in this file")
    parser.add_argument("--as-of", help="report date, YYYY-MM-DD or 'YYYY-MM-DD HH:MM' (default: now)")
    parser.add_argument("--baseline-weeks", type=int, default=4)
    parser.add_argument("--spike-factor", type=float, default=2.0)
    parser.add_argument("--min-count", type=int, default=5)
    args = parser.parse_args(argv)

    try:
        loaded = load_tickets(args.csv)
        as_of = parse_dt(args.as_of) if args.as_of else datetime.now().replace(microsecond=0)
    except (InputError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not loaded.tickets:
        print("error: no usable tickets in the file", file=sys.stderr)
        return 2
    con = build_db(loaded.tickets, args.db)
    report = render_report(con, as_of, Path(args.csv).name, loaded.skipped,
                           args.baseline_weeks, args.spike_factor, args.min_count)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
        print(f"Report written to {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
