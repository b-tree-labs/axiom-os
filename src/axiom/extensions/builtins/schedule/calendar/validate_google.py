# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Live validation of the GoogleCalendarProvider against a real calendar.

Round-trips a recurring event through the production adapter and binds it to a
PULSE cadence — the live confirmation of the mapping the unit tests cover with a
fake service. Run after the human setup in ``docs/validate-google.md``.

    python -m axiom.extensions.builtins.schedule.calendar.validate_google \
        --calendar-id "<calendar id>" \
        --credentials-file "$GOOGLE_APPLICATION_CREDENTIALS"

For a Google Workspace calendar, add ``--subject you@your-domain`` to use
domain-wide delegation; for a shared personal calendar, omit it (the calendar is
shared directly with the service-account email).
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime, timedelta


def main() -> int:
    ap = argparse.ArgumentParser(description="Live GoogleCalendarProvider round-trip.")
    ap.add_argument("--calendar-id", required=True, help="target calendar id")
    ap.add_argument(
        "--credentials-file",
        default=os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"),
        help="service-account JSON (or set GOOGLE_APPLICATION_CREDENTIALS)",
    )
    ap.add_argument("--subject", default=None, help="DWD impersonation (Workspace only)")
    ap.add_argument("--keep", action="store_true", help="don't delete the test event")
    args = ap.parse_args()

    if not args.credentials_file:
        ap.error("provide --credentials-file or set GOOGLE_APPLICATION_CREDENTIALS")

    from axiom.extensions.builtins.schedule import calendar
    from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at
    from axiom.extensions.builtins.schedule.calendar import binding
    from axiom.extensions.builtins.schedule.calendar.protocol import EventSpec

    provider = calendar.get_provider("google", {
        "credentials_file": args.credentials_file,
        "calendar_id": args.calendar_id,
        "subject": args.subject,
    })

    print("1. detect/health ->", calendar.detect("google", {
        "credentials_file": args.credentials_file,
        "calendar_id": args.calendar_id,
        "subject": args.subject,
    }))

    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    spec = EventSpec(
        summary="Axiom PULSE round-trip",
        start=start,
        end=start + timedelta(minutes=30),
        rrule="RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR",
        metadata={"pulse": "validate"},
    )
    ref = provider.create_event(spec)
    print("2. created event ->", ref.event_id)

    events = provider.list_events(
        start=start - timedelta(hours=1), end=start + timedelta(hours=1)
    )
    got = next(e for e in events if e.ref and e.ref.event_id == ref.event_id)
    print("3. read back     -> rrule:", got.rrule, "| meta:", got.metadata)

    cadence = binding.event_to_cadence(got)
    nxt = compute_next_fire_at(cadence, None, start)
    print("4. bound cadence ->", cadence.kind, cadence.rrule)
    print("   next fire     ->", nxt)

    if args.keep:
        print("5. kept event (cleanup skipped):", ref.event_id)
    else:
        provider.delete_event(ref)
        print("5. deleted event (cleanup) ->", ref.event_id)

    print("\nOK — live round-trip succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
