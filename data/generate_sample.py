#!/usr/bin/env python3
"""Generate a synthetic ticket export for demos and testing.

Every ticket, name and address in the output is made up. The last week has a
deliberate jump in VPN tickets so the spike detection has something to find.

    python data/generate_sample.py > data/tickets_sample.csv
"""
import csv
import random
import sys
from datetime import datetime, timedelta

SEED = 42
END = datetime(2026, 9, 28)          # report date used in the README example
WEEKS = 12
CATEGORIES = {                       # category: typical tickets per week
    "Password reset": 22, "Account lockout": 12, "Outlook / email": 14, "VPN": 6,
    "MFA / Authenticator": 8, "Printer": 7, "Laptop hardware": 6, "Software request": 10,
    "Network / Wi-Fi": 5, "Teams": 5,
}
PRIORITY_WEIGHTS = [("P1", 0.03), ("P2", 0.15), ("P3", 0.62), ("P4", 0.20)]
# median resolution in hours and spread, calendar hours
RESOLUTION = {"P1": (2.0, 0.45), "P2": (4.5, 0.45), "P3": (10.0, 0.6), "P4": (26.0, 0.55)}


def pick_priority(rng):
    r, acc = rng.random(), 0.0
    for priority, weight in PRIORITY_WEIGHTS:
        acc += weight
        if r <= acc:
            return priority
    return "P4"


def main(out=sys.stdout):
    rng = random.Random(SEED)
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["ticket_id", "opened", "resolved", "priority", "category", "caller_name", "caller_email"])
    start = END - timedelta(weeks=WEEKS)
    n = 0
    for week in range(WEEKS):
        week_start = start + timedelta(weeks=week)
        last_week = week == WEEKS - 1
        for category, per_week in CATEGORIES.items():
            count = max(0, round(rng.gauss(per_week, per_week ** 0.5)))
            if last_week and category == "VPN":
                count = 24                                   # the incident the report should surface
            for _ in range(count):
                n += 1
                opened = week_start + timedelta(days=rng.randrange(0, 5),        # weekdays
                                                hours=rng.randrange(8, 18), minutes=rng.randrange(0, 60))
                priority = pick_priority(rng)
                median, spread = RESOLUTION[priority]
                hours = rng.lognormvariate(0, spread) * median
                resolved = opened + timedelta(hours=hours)
                still_open = resolved >= END or (last_week and rng.random() < 0.12)
                resolved_text = "" if still_open else resolved.strftime("%Y-%m-%d %H:%M")
                caller = f"User {rng.randrange(1, 400):04d}"
                writer.writerow([f"INC{100000 + n}", opened.strftime("%Y-%m-%d %H:%M"), resolved_text,
                                 priority, category, caller, caller.replace(" ", ".").lower() + "@example.com"])


if __name__ == "__main__":
    main()
