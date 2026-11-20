# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.cutover_gate``: compare a day of two copies of silver and say whether a move may cut over.

Meant for a daily timer during a platform move, when old and new run in
parallel. One run compares one UTC day (default: yesterday) of the two copies,
records it in the gate's ledger, and returns the verdict with every unmet
condition. It changes neither copy.

The same skill records the things the verdict needs that no database can tell
it: an event (``--event outage|upgrade --event-day D``) and the one-off history
check (``--history clean|differs``). ``--verdict-only`` reads the ledger and
compares nothing.

The two databases are named by vault references (``old_ref``/``new_ref``), so a
password never reaches argv, the ledger or the output.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult


def _csv(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value or "").split(",") if v.strip()]


def _is_day(value: str) -> bool:
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    from ..conformance.cutover_gate import GateLedger, Rule, check_day, verdict

    ledger_path = params.get("ledger") or str(Path(ctx.state_dir or ".") / "cutover-gate.jsonl")
    event, history = params.get("event"), params.get("history")
    event_day = str(params.get("event_day") or "")
    if history is not None and history not in ("clean", "differs"):
        return SkillResult(ok=False, errors=["--history is `clean` or `differs`"])
    if event is not None and not _is_day(event_day):
        return SkillResult(ok=False, errors=["--event needs --event-day YYYY-MM-DD"])
    compare = event is None and history is None and not params.get("verdict_only")
    if compare and not (params.get("old_ref") and params.get("new_ref") and _csv(params.get("sites"))):
        return SkillResult(ok=False, errors=["comparing needs --old-ref, --new-ref (vault references) and --sites"])
    day = str(params.get("day") or (datetime.now(UTC).date() - timedelta(days=1)).isoformat())
    if compare and not _is_day(day):
        return SkillResult(ok=False, errors=["--day is YYYY-MM-DD (UTC)"])

    ledger = GateLedger(ledger_path)
    actions: list[str] = []
    value: dict[str, Any] = {}
    if event is not None:
        ledger.record_event(str(event), event_day, note=str(params.get("note") or ""))
        actions.append(f"recorded {event} on {event_day}")
    if history is not None:
        ledger.record_history(history == "clean", note=str(params.get("note") or ""))
        actions.append(f"recorded history: {history}")

    if compare:
        import psycopg

        from axiom.extensions.builtins.secrets import SecretRef, resolve

        from ..conformance.reconcile import PgSide

        # The addresses are read here and handed to the driver; they are never
        # logged, returned or written to the ledger.
        with resolve(SecretRef.parse(str(params["old_ref"]))) as s:
            old_dsn = s.as_str().strip()
        with resolve(SecretRef.parse(str(params["new_ref"]))) as s:
            new_dsn = s.as_str().strip()
        with psycopg.connect(old_dsn) as old, psycopg.connect(new_dsn) as new:
            # Read-only on both sides: the comparison is a dry run, and this
            # makes that true of the connection too.
            old.read_only = new.read_only = True
            checked = check_day(PgSide(old), PgSide(new), sites=_csv(params["sites"]), day=day)
        ledger.record_day(checked)
        value["day"] = {"day": checked.day, "clean": checked.clean, "windows": checked.windows,
                        "missing_on_new": checked.missing_on_new, "extra_on_new": checked.extra_on_new,
                        "conflicts": checked.conflicts, "feeds": checked.feeds, "examples": checked.examples}
        actions.append(f"compared {day}: {'clean' if checked.clean else 'NOT clean'}")

    defaults = Rule()
    rule = Rule(
        min_clean_days=int(params.get("min_clean_days") or defaults.min_clean_days),
        min_active_days=int(params.get("min_active_days") or defaults.min_active_days),
        active_feeds=tuple(_csv(params.get("active_feeds"))),
        required_events=tuple(_csv(params.get("required_events"))) or defaults.required_events,
    )
    v = verdict(ledger.days(), rule=rule, events=ledger.events(), history_clean=ledger.history_clean())
    value["verdict"] = {"safe": v.safe, "clean_streak": v.clean_streak, "active_days": v.active_days,
                        "reasons": v.reasons}
    actions.append("SAFE to cut over" if v.safe else f"not yet safe: {len(v.reasons)} condition(s) unmet")
    return SkillResult(ok=True, value=value, actions_taken=actions)
