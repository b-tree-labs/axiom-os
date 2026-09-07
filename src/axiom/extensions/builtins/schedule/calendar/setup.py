# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Calendar connector setup utility — automate what's automatable, diagnose the rest.

The GCP console steps (create project, enable API, create service account + key)
need an interactive browser login, so they stay human — but this utility:

- **validates** the service-account JSON and extracts its email/project;
- **generates the `gcloud` commands** that do the enable-API + create-SA + create-key
  steps non-interactively (run them yourself, or pass --run);
- **diagnoses access** to a target calendar and turns a raw 404/403 into a precise
  next action ("share calendar X with <sa-email>" / "enable the Calendar API here");
- **chains into the live round-trip** once access is green.

CLI:
    python -m axiom.extensions.builtins.schedule.calendar.setup bootstrap --project-id <id>
    python -m axiom.extensions.builtins.schedule.calendar.setup doctor \
        --credentials-file sa.json --calendar-id <id> [--roundtrip]
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

SHARE_URL = "https://calendar.google.com/calendar/u/0/r/settings"
ENABLE_API_URL = "https://console.cloud.google.com/apis/library/calendar-json.googleapis.com"

_REQUIRED_SA_FIELDS = ("client_email", "private_key", "project_id")


class SetupError(Exception):
    """A setup precondition failed with an actionable message."""


def load_service_account(path: str) -> dict:
    """Validate a service-account JSON key and return its parsed contents."""
    p = Path(path).expanduser()
    if not p.exists():
        raise SetupError(f"no service-account file at {p}")
    try:
        data = json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        raise SetupError(f"{p} is not valid JSON: {exc}") from exc
    if data.get("type") != "service_account":
        raise SetupError(f"{p} is not a service-account key (type != 'service_account')")
    missing = [k for k in _REQUIRED_SA_FIELDS if not data.get(k)]
    if missing:
        raise SetupError(f"service-account JSON missing required fields: {missing}")
    return data


def gcloud_bootstrap_commands(
    *,
    project_id: str,
    sa_name: str = "axiom-pulse-calendar",
    key_path: str = "~/.config/axiom/pulse-calendar-sa.json",
) -> list[str]:
    """The exact gcloud commands that automate enable-API + create-SA + create-key."""
    sa_email = f"{sa_name}@{project_id}.iam.gserviceaccount.com"
    return [
        f"gcloud config set project {project_id}",
        "gcloud services enable calendar-json.googleapis.com",
        f"gcloud iam service-accounts create {sa_name} "
        f"--display-name='Axiom PULSE calendar'",
        f"gcloud iam service-accounts keys create {key_path} "
        f"--iam-account={sa_email}",
    ]


def _classify(exc: Exception) -> str:
    """Map a calendar-access exception to a remediation kind."""
    text = str(exc).lower().replace(" ", "")
    status = getattr(getattr(exc, "resp", None), "status", None) or getattr(
        exc, "status_code", None
    )
    if status == 404 or "404" in text or "notfound" in text:
        return "share"
    if "hasnotbeenenabled" in text or "accessnotconfigured" in text or "apinotenabled" in text:
        return "enable_api"
    if status == 403 or "403" in text or "permission" in text:
        return "share"
    return "auth"


def preflight(*, calendar_id: str, sa_email: str, provider: Any) -> dict:
    """Probe calendar access; return ``{state, sa_email, summary, remediation}``."""
    now = datetime.now(UTC)
    try:
        provider.list_events(
            calendar_id=calendar_id, start=now - timedelta(days=1), end=now + timedelta(days=1)
        )
    except Exception as exc:  # noqa: BLE001 — classify into remediation
        kind = _classify(exc)
        remediation = {
            "share": f"Share calendar {calendar_id} with {sa_email} "
                     f"(permission: 'Make changes to events') at {SHARE_URL}",
            "enable_api": f"Enable the Google Calendar API for the service-account's "
                          f"project at {ENABLE_API_URL}",
            "auth": f"Authentication failed for {sa_email}: {exc}",
        }[kind]
        return {"state": "broken", "sa_email": sa_email, "summary": str(exc),
                "remediation": remediation}
    return {"state": "configured", "sa_email": sa_email,
            "summary": f"{sa_email} can access {calendar_id}", "remediation": None}


def doctor(*, credentials_file: str, calendar_id: str, provider: Optional[Any] = None) -> dict:
    """Validate the key + diagnose calendar access in one call."""
    sa = load_service_account(credentials_file)
    sa_email = sa["client_email"]
    if provider is None:
        from axiom.extensions.builtins.schedule.calendar import get_provider

        provider = get_provider("google", {
            "credentials_file": credentials_file, "calendar_id": calendar_id,
        })
    return preflight(calendar_id=calendar_id, sa_email=sa_email, provider=provider)


def _google_provider(credentials_file: str, calendar_id: Optional[str] = None) -> Any:
    from axiom.extensions.builtins.schedule.calendar import get_provider

    return get_provider("google", {
        "credentials_file": credentials_file, "calendar_id": calendar_id or "primary",
    })


def _roundtrip(provider: Any, calendar_id: str) -> dict:
    """Create a recurring event, bind it to a cadence, then delete it — the live
    proof the whole path works. Returns ``{ok, next_fires}``."""
    from axiom.extensions.builtins.schedule.cadence import compute_next_fire_at
    from axiom.extensions.builtins.schedule.calendar import binding
    from axiom.extensions.builtins.schedule.calendar.protocol import EventSpec

    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)
    ref = provider.create_event(
        EventSpec(summary="Axiom PULSE round-trip", start=start,
                  end=start + timedelta(minutes=30),
                  rrule="RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR",
                  metadata={"pulse": "verify"}),
        calendar_id=calendar_id,
    )
    events = provider.list_events(
        calendar_id=calendar_id, start=start - timedelta(hours=1),
        end=start + timedelta(hours=1),
    )
    got = next((e for e in events if e.ref and e.ref.event_id == ref.event_id), None)
    fires: list = []
    if got is not None:
        cadence = binding.event_to_cadence(got)
        last = None
        for _ in range(3):
            nxt = compute_next_fire_at(cadence, last, start if last is None else last)
            if nxt is None:
                break
            fires.append(nxt)
            last = nxt
    provider.delete_event(ref)
    return {"ok": got is not None, "next_fires": fires}


def provision(
    *,
    credentials_file: str,
    share_with: str,
    calendar_name: str = "Axiom PULSE",
    provider: Optional[Any] = None,
) -> dict:
    """Dead-simple calendar setup from just the service-account key.

    Creates an SA-owned calendar, shares it back to ``share_with`` (so it shows
    in their Google Calendar UI — no manual create, no manual share), and
    verifies the whole path with a live round-trip.
    """
    from axiom.extensions.builtins.schedule.calendar.protocol import (
        CalendarCapability,
        require,
    )

    sa = load_service_account(credentials_file)
    sa_email = sa["client_email"]
    prov = provider or _google_provider(credentials_file)
    if not prov.health():
        raise SetupError(
            f"Calendar API not reachable for {sa_email}. Enable it ({ENABLE_API_URL}) "
            "and confirm the key is valid."
        )
    require(prov, CalendarCapability.CREATE_CALENDAR)
    require(prov, CalendarCapability.MANAGE_ACL)

    calendar_id = prov.create_calendar(summary=calendar_name, timezone="UTC")
    prov.share_calendar(calendar_id=calendar_id, email=share_with, role="owner")
    rt = _roundtrip(prov, calendar_id)
    return {
        "calendar_id": calendar_id,
        "sa_email": sa_email,
        "shared_with": share_with,
        "verified": rt["ok"],
        "next_fires": rt["next_fires"],
    }


ENTRA_PERMS_URL = "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade"
M365_DEV_TENANT_URL = "https://developer.microsoft.com/microsoft-365/dev-program"


def _m365_provider(config: dict) -> Any:
    from axiom.extensions.builtins.schedule.calendar import get_provider

    return get_provider("m365", config)


def _classify_m365(exc: Exception) -> str:
    """Map an AAD/Graph error to a remediation kind — the cryptic-error translator."""
    t = str(exc).lower()
    if "aadsts90002" in t or ("tenant" in t and "not found" in t):
        return "tenant"
    if "aadsts7000215" in t or "invalid client secret" in t or "invalid_client" in t:
        return "secret"
    if "aadsts700016" in t or ("application with identifier" in t and "not found" in t):
        return "client"
    if any(k in t for k in ("accessdenied", "403", "consent", "insufficient privileges",
                            "authorization_requestdenied")):
        return "consent"
    if "resourcenotfound" in t or "mailboxnotenabledforrestapi" in t or "404" in t:
        return "mailbox"
    return "unknown"


_M365_REMEDIATION = {
    "tenant": "Wrong tenant id. Entra > Overview > 'Directory (tenant) ID'. "
              f"({ENTRA_PERMS_URL})",
    "secret": "Wrong/expired client secret. Entra > your app > Certificates & secrets "
              "> New client secret, copy the *Value* (not the Secret ID).",
    "client": "Wrong client id. Entra > your app > Overview > 'Application (client) ID'.",
    "consent": "The app lacks consented permission. Entra > your app > API permissions: "
               "add Microsoft Graph **Application** permission 'Calendars.ReadWrite', then "
               f"click **Grant admin consent**. ({ENTRA_PERMS_URL})",
    "mailbox": "The user_id mailbox isn't reachable in this tenant — confirm the UPN exists "
               "and has an Exchange Online mailbox (or add an application access policy).",
    "unknown": None,
}


def m365_doctor(*, config: dict, provider: Optional[Any] = None) -> dict:
    """Diagnose an M365 calendar connection: localizes auth vs consent vs mailbox
    and returns the exact portal fix. ``config`` = tenant_id/client_id/
    client_secret/user_id[/calendar_id]."""
    prov = provider or _m365_provider(config)
    now = datetime.now(UTC)
    try:
        prov.list_events(start=now - timedelta(days=1), end=now + timedelta(days=1))
    except Exception as exc:  # noqa: BLE001
        kind = _classify_m365(exc)
        return {"state": "broken", "kind": kind, "summary": str(exc),
                "remediation": _M365_REMEDIATION.get(kind) or f"Unrecognized error: {exc}"}
    return {"state": "configured", "kind": "ok",
            "summary": f"Graph calendar reachable for {config.get('user_id')}",
            "remediation": None}


def m365_provision(
    *, config: dict, calendar_name: str = "Axiom PULSE", provider: Optional[Any] = None
) -> dict:
    """Once auth works: create a calendar in the target mailbox and verify with a
    live round-trip. No sharing step — the calendar is in the user's mailbox."""
    from axiom.extensions.builtins.schedule.calendar.protocol import (
        CalendarCapability,
        require,
    )

    prov = provider or _m365_provider(config)
    diag = m365_doctor(config=config, provider=prov)
    if diag["state"] != "configured":
        raise SetupError(diag["remediation"] or diag["summary"])
    require(prov, CalendarCapability.CREATE_CALENDAR)
    calendar_id = prov.create_calendar(summary=calendar_name)
    rt = _roundtrip(prov, calendar_id)
    return {"calendar_id": calendar_id, "user_id": config.get("user_id"),
            "verified": rt["ok"], "next_fires": rt["next_fires"]}


# --- CLI ----------------------------------------------------------------------

def _cmd_bootstrap(args: argparse.Namespace) -> int:
    cmds = gcloud_bootstrap_commands(project_id=args.project_id, key_path=args.key_path)
    if args.run:
        import subprocess

        for c in cmds:
            print(f"$ {c}")
            rc = subprocess.run(c, shell=True).returncode
            if rc != 0:
                print(f"  ! exited {rc}; stopping")
                return rc
        return 0
    print("# Run these (needs gcloud authenticated). Or re-run with --run:")
    for c in cmds:
        print(c)
    print(f"\n# Then share the calendar with the SA email and run:\n"
          f"#   python -m {__spec__.name} doctor --credentials-file {args.key_path} "
          f"--calendar-id <id> --roundtrip")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    try:
        result = doctor(credentials_file=args.credentials_file, calendar_id=args.calendar_id)
    except SetupError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"service account : {result['sa_email']}")
    print(f"state           : {result['state']}")
    print(f"summary         : {result['summary']}")
    if result["remediation"]:
        print(f"\n→ NEXT: {result['remediation']}")
        return 1
    print("✓ access confirmed.")
    if args.roundtrip:
        from axiom.extensions.builtins.schedule.calendar import validate_google

        return validate_google.main()  # uses the same env/args contract
    return 0


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Axiom calendar connector setup utility.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("bootstrap", help="emit/run the gcloud setup commands")
    b.add_argument("--project-id", required=True)
    b.add_argument("--key-path", default="~/.config/axiom/pulse-calendar-sa.json")
    b.add_argument("--run", action="store_true", help="execute the commands (needs gcloud)")
    b.set_defaults(func=_cmd_bootstrap)

    d = sub.add_parser("doctor", help="validate key + diagnose calendar access")
    d.add_argument("--credentials-file", required=True)
    d.add_argument("--calendar-id", required=True)
    d.add_argument("--roundtrip", action="store_true", help="run a live round-trip if access is green")
    d.set_defaults(func=_cmd_doctor)

    p = sub.add_parser("provision", help="dead-simple: create + share a calendar, then verify")
    p.add_argument("--credentials-file", required=True)
    p.add_argument("--share-with", required=True, help="your email; the calendar is shared to you")
    p.add_argument("--calendar-name", default="Axiom PULSE")
    p.set_defaults(func=_cmd_provision)

    md = sub.add_parser("m365-doctor", help="diagnose an M365 (Graph) calendar connection")
    for _a in ("--tenant-id", "--client-id", "--client-secret", "--user-id"):
        md.add_argument(_a, required=True)
    md.add_argument("--calendar-id")
    md.set_defaults(func=_cmd_m365_doctor)

    mp = sub.add_parser("m365-provision", help="create a calendar in the mailbox + verify (after auth works)")
    for _a in ("--tenant-id", "--client-id", "--client-secret", "--user-id"):
        mp.add_argument(_a, required=True)
    mp.add_argument("--calendar-name", default="Axiom PULSE")
    mp.set_defaults(func=_cmd_m365_provision)

    args = ap.parse_args(argv)
    return args.func(args)


def _m365_config(args: argparse.Namespace) -> dict:
    return {"tenant_id": args.tenant_id, "client_id": args.client_id,
            "client_secret": args.client_secret, "user_id": args.user_id,
            "calendar_id": getattr(args, "calendar_id", None) or ""}


def _cmd_m365_doctor(args: argparse.Namespace) -> int:
    r = m365_doctor(config=_m365_config(args))
    print(f"state   : {r['state']}")
    print(f"summary : {r['summary']}")
    if r["remediation"]:
        print(f"\n→ NEXT: {r['remediation']}")
        return 1
    print("✓ Graph calendar reachable.")
    return 0


def _cmd_m365_provision(args: argparse.Namespace) -> int:
    try:
        r = m365_provision(config=_m365_config(args), calendar_name=args.calendar_name)
    except SetupError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ created calendar : {r['calendar_id']}  (\"{args.calendar_name}\") in {r['user_id']}'s mailbox")
    fires = ", ".join(f.isoformat() for f in r["next_fires"])
    print(f"✓ verified round-trip; next fires: {fires}")
    print("\nDone — PULSE is connected to your M365 calendar.")
    return 0


def _cmd_provision(args: argparse.Namespace) -> int:
    try:
        r = provision(credentials_file=args.credentials_file, share_with=args.share_with,
                      calendar_name=args.calendar_name)
    except SetupError as exc:
        print(f"✗ {exc}")
        return 1
    print(f"✓ service account : {r['sa_email']}")
    print(f"✓ created calendar : {r['calendar_id']}  (\"{args.calendar_name}\")")
    print(f"✓ shared with      : {r['shared_with']}  (it will appear in your Google Calendar)")
    fires = ", ".join(f.isoformat() for f in r["next_fires"])
    print(f"✓ verified round-trip; next fires: {fires}")
    print("\nDone — PULSE is connected to your calendar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
