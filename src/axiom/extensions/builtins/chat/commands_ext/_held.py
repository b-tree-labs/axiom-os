# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The hold-for-human queue, as ``/permissions`` shows it.

An action the person answered ``h`` to is written by ``agent._hold_for_later`` to the durable approval
queue (``default_approval_path``). ``axi approve`` reads that same file through the ``approval.pending``
skill. This reads it the same way, so what chat lists is what ``approve`` will list, and an unreadable
queue is reported as unreadable: ``ApprovalStoreUnavailable`` is never converted into "nothing waiting".

The queue holds every pending action, not only chat's. That is the point of one queue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001 - a label never takes the command down
        return "axi"


def held_actions() -> list:
    """Pending actions in the durable queue, oldest first. Raises ``ApprovalStoreUnavailable``."""
    from axiom.infra.orchestrator.approval import ApprovalGate
    from axiom.infra.orchestrator.approval_store import FileActionStore, default_approval_path

    gate = ApprovalGate(FileActionStore(default_approval_path()))
    return sorted(gate.pending(), key=lambda a: a.created_at)


def _expires(created_at: str) -> str:
    from axiom.infra.orchestrator.approval_store import DEFAULT_TTL_HOURS

    try:
        when = datetime.fromisoformat(created_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
    except ValueError:
        return "expiry unknown"
    left = when + timedelta(hours=DEFAULT_TTL_HOURS) - datetime.now(UTC)
    hours = int(left.total_seconds() // 3600)
    return f"expires in {hours}h" if hours >= 1 else "expires within the hour"


def format_held() -> str:
    """The queue section: ids, ages, the argument *names* (never values), and how to answer."""
    from axiom.infra.cli_format import relative_age
    from axiom.infra.orchestrator.approval_store import ApprovalStoreUnavailable

    cli = _brand_cli()
    try:
        actions = held_actions()
    except ApprovalStoreUnavailable as exc:
        return (
            "  Held for a human: the queue could not be read, so what is waiting is unknown.\n"
            f"  {exc}"
        )
    except Exception as exc:  # noqa: BLE001
        return f"  Held for a human: the queue could not be read ({type(exc).__name__}: {exc})."
    if not actions:
        return "  Held for a human: nothing is waiting."
    lines = [f"  Held for a human: {len(actions)} waiting"]
    for a in actions:
        # Argument values can carry content or credentials; names are enough to recognise the call.
        names = ", ".join(sorted(a.params)) or "no arguments"
        lines.append(
            f"    {a.action_id}  {a.name}({names})  proposed {relative_age(a.created_at)}, "
            f"{_expires(a.created_at)}"
        )
    lines.append(f"  Answer from any terminal: {cli} approve ok <id>   |   {cli} approve no <id> --reason '...'")
    lines.append(f"  Inspect the arguments first with: {cli} approve list")
    return "\n".join(lines)
