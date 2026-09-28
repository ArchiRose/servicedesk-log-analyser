# Service desk log analyser

![tests](https://github.com/ArchiRose/servicedesk-log-analyser/actions/workflows/tests.yml/badge.svg)

A command-line tool that turns a ticket export from ServiceNow or GLPI into the weekly report a service desk lead needs. It loads the CSV into SQLite, then reports:

- **Volume by category**, so you can see where the work comes from.
- **SLA by priority**: tickets resolved, breaches, breach rate and median resolution time.
- **Busier than usual**: categories whose volume in the last 7 days is well above their recent weekly average. These are candidates for a problem record.
- **Open tickets already past SLA**, so nothing ages quietly in the queue.

Standard library only (Python 3.10+). Caller names and contact details are dropped at load time and never reach the database or the report.

Part of my portfolio: [archirose.github.io](https://archirose.github.io) (design IT-02).

## Quick start

```bash
python sdla.py data/tickets_sample.csv --as-of 2026-09-28 --out report.md
```

The sample file is synthetic: every ticket, name and address in it is made up. It covers 12 weeks and includes a deliberate jump in VPN tickets in the last week. Here is part of the report it produces (full version in [`data/sample_report.md`](data/sample_report.md)):

```text
## Busier than usual (last 7 days vs previous 4 weeks)

| Category | Last 7 days | Weekly average before | Change |
|---|---:|---:|---:|
| VPN | 24 | 4.5 | 5.3x |
```

### Options

| Option | Default | Meaning |
|---|---|---|
| `--out FILE` | print to screen | Write the Markdown report to a file |
| `--as-of DATE` | now | Report date; the "last 7 days" end here |
| `--db FILE` | in memory | Keep the SQLite database for your own queries |
| `--baseline-weeks N` | 4 | Weeks used for the "usual" weekly average |
| `--spike-factor X` | 2.0 | How far above average counts as a spike |
| `--min-count N` | 5 | Ignore categories with fewer tickets than this in the last 7 days |

## Input format

A CSV with a header row. Column names are not case-sensitive.

| Column | Required | Notes |
|---|---|---|
| `ticket_id` | yes | Duplicate IDs are skipped and reported |
| `opened` | yes | `2026-09-01 09:30`, ISO `2026-09-01T09:30:00`, or UK `01/09/2026 09:30` |
| `resolved` | no | Blank means the ticket is still open |
| `priority` | yes | `P1`–`P4`, `1`–`4`, `1 - Critical`, or words like `High` |
| `category` | yes | Used as-is after trimming spaces |
| `sla_hours` | no | Per-ticket target; otherwise P1 4 h, P2 8 h, P3 24 h, P4 72 h |
| `caller_name`, `caller_email`, `caller_phone` | no | **Dropped on load** |

Bad rows (unreadable dates, resolved before opened, duplicates) are skipped and listed at the top of the report instead of stopping the run.

## How the checks work

- **Breach:** a resolved ticket breaches when its resolution time in calendar hours is above its SLA target. Business-hours calendars are out of scope.
- **Spike:** a category counts as a spike when it has at least `--min-count` tickets in the last 7 days and more than `--spike-factor` times its weekly average over the previous `--baseline-weeks` weeks.
- **Open past SLA:** a ticket with no resolved date that has been open longer than its SLA target at the report date.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests cover date and priority parsing, the removal of personal data, skipping bad rows, SLA maths, spike detection and the command line. GitHub Actions runs them on Python 3.10 and 3.12 for every push.

## Licence

MIT
