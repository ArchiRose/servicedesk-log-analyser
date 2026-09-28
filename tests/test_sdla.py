import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import sdla  # noqa: E402

HEADER = "ticket_id,opened,resolved,priority,category,caller_name,caller_email\n"


def write_csv(tmp_path, body, header=HEADER):
    path = tmp_path / "tickets.csv"
    path.write_text(header + body, encoding="utf-8")
    return path


def test_parse_dt_accepts_common_export_formats():
    assert sdla.parse_dt("2026-09-01 09:30") == datetime(2026, 9, 1, 9, 30)
    assert sdla.parse_dt("2026-09-01T09:30:15") == datetime(2026, 9, 1, 9, 30, 15)
    assert sdla.parse_dt("01/09/2026 09:30") == datetime(2026, 9, 1, 9, 30)   # UK day-first
    assert sdla.parse_dt("") is None
    with pytest.raises(sdla.InputError):
        sdla.parse_dt("next Tuesday")


@pytest.mark.parametrize("raw,expected", [("P1", "P1"), ("2", "P2"), ("3 - Moderate", "P3"), ("Low", "P4")])
def test_normalise_priority(raw, expected):
    assert sdla.normalise_priority(raw) == expected


def test_personal_data_never_reaches_the_database(tmp_path):
    path = write_csv(tmp_path, "INC1,2026-09-01 09:00,2026-09-01 10:00,P3,Printer,Jane Doe,jane@example.com\n")
    loaded = sdla.load_tickets(path)
    con = sdla.build_db(loaded.tickets)
    columns = [row[1] for row in con.execute("PRAGMA table_info(tickets)")]
    assert "caller_name" not in columns and "caller_email" not in columns
    dump = "\n".join(con.iterdump())
    assert "Jane Doe" not in dump and "jane@example.com" not in dump


def test_bad_rows_are_skipped_not_fatal(tmp_path):
    body = ("INC1,2026-09-01 09:00,2026-09-01 10:00,P3,VPN,,\n"
            "INC1,2026-09-01 09:00,2026-09-01 10:00,P3,VPN,,\n"          # duplicate
            "INC2,not a date,,P2,VPN,,\n"                                 # bad date
            "INC3,2026-09-02 09:00,2026-09-01 09:00,P2,VPN,,\n")          # resolved before opened
    loaded = sdla.load_tickets(write_csv(tmp_path, body))
    assert [t.ticket_id for t in loaded.tickets] == ["INC1"]
    assert len(loaded.skipped) == 3


def test_missing_required_column_is_an_error(tmp_path):
    path = write_csv(tmp_path, "INC1,2026-09-01 09:00\n", header="ticket_id,opened\n")
    with pytest.raises(sdla.InputError):
        sdla.load_tickets(path)


def test_sla_breaches_and_median_by_priority(tmp_path):
    body = ("A,2026-09-01 09:00,2026-09-01 12:00,P1,Network,,\n"    # 3 h, within 4 h
            "B,2026-09-01 09:00,2026-09-01 14:00,P1,Network,,\n"    # 5 h, breached
            "C,2026-09-01 09:00,,P1,Network,,\n"                    # open, not counted as resolved
            "D,2026-09-01 09:00,2026-09-02 08:00,P3,Printer,,\n")   # 23 h, within 24 h
    con = sdla.build_db(sdla.load_tickets(write_csv(tmp_path, body)).tickets)
    by_priority = {s.priority: s for s in sdla.sla_by_priority(con)}
    p1 = by_priority["P1"]
    assert (p1.total, p1.resolved, p1.breached) == (3, 2, 1)
    assert p1.breach_rate == pytest.approx(0.5)
    assert p1.median_hours == pytest.approx(4.0)
    assert by_priority["P3"].breached == 0


def test_explicit_sla_hours_column_overrides_default(tmp_path):
    header = "ticket_id,opened,resolved,priority,category,sla_hours\n"
    body = "A,2026-09-01 09:00,2026-09-01 12:00,P1,Network,2\n"      # 3 h against a 2 h target
    con = sdla.build_db(sdla.load_tickets(write_csv(tmp_path, body, header)).tickets)
    assert sdla.sla_by_priority(con)[0].breached == 1


def test_weekly_spike_detection(tmp_path):
    rows = []
    n = 0
    # four quiet baseline weeks: 2 VPN tickets and 5 printer tickets a week
    for week in range(4):
        for i in range(2):
            n += 1
            rows.append(f"T{n},2026-08-{24 - 7 * week + i:02d} 10:00,,P3,VPN,,")
        for i in range(5):
            n += 1
            rows.append(f"T{n},2026-08-{24 - 7 * week + i:02d} 11:00,,P3,Printer,,")
    # last week: VPN jumps to 9, printers stay at 5
    for i in range(9):
        n += 1
        rows.append(f"T{n},2026-09-0{1 + i % 6} 10:00,,P3,VPN,,")
    for i in range(5):
        n += 1
        rows.append(f"T{n},2026-09-0{1 + i} 11:00,,P3,Printer,,")
    con = sdla.build_db(sdla.load_tickets(write_csv(tmp_path, "\n".join(rows) + "\n")).tickets)
    spikes = sdla.weekly_spikes(con, as_of=datetime(2026, 9, 7), baseline_weeks=4, factor=2.0, min_count=5)
    assert [s.category for s in spikes] == ["VPN"]
    assert spikes[0].this_week == 9 and spikes[0].baseline_avg == pytest.approx(2.0)


def test_open_tickets_past_sla(tmp_path):
    body = ("A,2026-09-01 09:00,,P1,Network,,\n"        # open 27 h against 4 h -> breach
            "B,2026-09-02 10:00,,P4,Printer,,\n")       # open 2 h against 72 h -> fine
    con = sdla.build_db(sdla.load_tickets(write_csv(tmp_path, body)).tickets)
    breaches = sdla.open_breaches(con, as_of=datetime(2026, 9, 2, 12, 0))
    assert [b.ticket_id for b in breaches] == ["A"]
    assert breaches[0].age_hours == pytest.approx(27.0)


def test_cli_writes_markdown_report(tmp_path):
    path = write_csv(tmp_path, "INC1,2026-09-01 09:00,2026-09-01 10:00,P3,Printer,Jane Doe,jane@example.com\n")
    out = tmp_path / "report.md"
    assert sdla.main([str(path), "--as-of", "2026-09-02", "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "## SLA by priority" in text and "Printer" in text
    assert "Jane Doe" not in text


def test_cli_rejects_unusable_file(tmp_path, capsys):
    path = write_csv(tmp_path, "", header="ticket_id,opened\n")
    assert sdla.main([str(path)]) == 2
    assert "missing required column" in capsys.readouterr().err
