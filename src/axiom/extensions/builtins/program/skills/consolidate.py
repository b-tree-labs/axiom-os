# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.consolidate`` and ``program.digest`` — the entropy pass (prd-program R27).

``consolidate`` runs one consolidation pass over the node's program and turns
it into **one digest of proposals**: likely duplicates, stale items to close or
refresh, items missing a lane or an owner, items whose work sits in another
lane, unlinked repository work a ``program.repo_map`` rule attributes to an
epic, broken cross-links, and the rest of the taxonomy (see ``..entropy``).

It writes nothing to any tracker and changes nothing in the data file. Every
proposal is consent-gated: it carries its evidence and, where the change is
the data file's to make, the exact program verb that applies it; a person
decides. The digest is persisted on the node (``<state_dir>/program/digests``)
and, when asked to deliver (the weekly schedule does), sent once through
HERALD to the program's operator (``AXI_FOLLOWUP_OPERATOR``, else the deputy).
An unchanged digest is not sent again, an empty one is not sent at all, and an
install that is not the program's writer (ADR-248) never delivers.

``digest`` is the read: the latest persisted digest, for the CLI and the
composed MCP.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult
from axiom.infra.state import atomic_write

from .. import entropy
from ..model import ProgramError, load_program
from ._source import ABSENT, BAD_REQUEST, NO_DATA, resolve_data_path
from ._writer import declared_writer, read_only_reason

#: Test seam for the clock.
_now: Callable[[], datetime] = lambda: datetime.now(UTC)  # noqa: E731
#: Test seam for delivery: (recipient, subject, body, dedup_key) -> Sent-like.
_sender: Callable[[str, str, str, str], Any] | None = None

#: How many digests the node keeps (one a week is about three months).
HISTORY = 12
#: How many proposals the delivered message lists before pointing at the full digest.
MESSAGE_LINES = 25


def _refuse(kind: str, message: str) -> SkillResult:
    return SkillResult(ok=False, value={"refused": kind}, errors=[message])


def digests_dir(ctx: SkillContext) -> Path:
    return Path(ctx.state_dir) / "program" / "digests"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_digest(data: Any, *, now: datetime) -> dict[str, Any]:
    items = entropy.proposals(data, now=now)
    return {
        "schema": "axiom.program.digest/0.1",
        "generated_at": _iso(now),
        "program": {"id": data.program.get("id"), "name": data.program.get("name")},
        "digest_id": entropy.digest_id(items),
        "count": len(items),
        "by_kind": dict(Counter(p["kind"] for p in items)),
        "proposals": items,
        "consent": "proposals only: nothing is applied until a person agrees",
    }


def _persist(ctx: SkillContext, digest: dict[str, Any]) -> Path:
    root = digests_dir(ctx)
    root.mkdir(parents=True, exist_ok=True)
    name = digest["generated_at"].replace(":", "").replace("-", "") + ".json"
    path = root / name
    atomic_write(path, digest)
    atomic_write(root / "latest.json", digest)
    history = sorted(p for p in root.glob("*.json") if p.name not in ("latest.json", "delivered.json"))
    for old in history[:-HISTORY]:
        try:
            old.unlink()
        except OSError:
            pass
    return path


def _last_delivered(ctx: SkillContext) -> str | None:
    try:
        record = json.loads((digests_dir(ctx) / "delivered.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record.get("digest_id") if isinstance(record, dict) else None


def render_message(digest: dict[str, Any]) -> tuple[str, str]:
    """``(subject, body)`` for the one message a cycle sends."""
    program = digest["program"].get("name") or digest["program"].get("id") or "program"
    kinds = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(digest["by_kind"].items()))
    subject = f"{program}: {digest['count']} proposal(s) to keep the tracker coherent"
    lines = [
        f"CLERK's consolidation pass for {program} ({digest['generated_at']}): {kinds}.",
        "Each is a proposal with its evidence; nothing has been changed.",
        "",
    ]
    for p in digest["proposals"][:MESSAGE_LINES]:
        line = f"- [{p['kind']}] {p['detail']}"
        if p.get("apply"):
            line += f"  (apply: {p['apply']})"
        lines.append(line)
    if digest["count"] > MESSAGE_LINES:
        lines.append(f"... and {digest['count'] - MESSAGE_LINES} more.")
    lines.append("")
    lines.append("The full digest: `program digest` (CLI) or the program digest tool (MCP).")
    return subject, "\n".join(lines)


def _deliver(ctx: SkillContext, digest: dict[str, Any], params: dict[str, Any], target: Path) -> dict[str, Any]:
    if digest["count"] == 0:
        return {"sent": False, "reason": "nothing to propose"}
    read_only = read_only_reason(declared_writer(target), params)
    if read_only is not None:
        return {"sent": False, "reason": "this install is not the program's writer", "read_only": read_only}
    if _last_delivered(ctx) == digest["digest_id"]:
        return {"sent": False, "reason": "unchanged since the last delivered digest"}
    from .. import followup_engine as engine

    recipient = engine.operator_for(ctx.state_dir)
    if recipient is None:
        return {"sent": False, "reason": "no recipient: set AXI_FOLLOWUP_OPERATOR or program.deputy"}
    subject, body = render_message(digest)
    dedup = f"program.digest:{digest['program'].get('id')}:{digest['digest_id']}"
    try:
        if _sender is not None:
            sent = _sender(recipient, subject, body, dedup)
        else:
            sent = engine.herald_send(recipient, subject, body, dedup, "normal", intent="program.digest")
    except Exception as exc:  # noqa: BLE001 - a delivery failure is reported, the digest is already kept
        return {"sent": False, "recipient": recipient, "reason": f"delivery failed ({type(exc).__name__})"}
    ok = bool(getattr(sent, "ok", False))
    if ok:
        atomic_write(digests_dir(ctx) / "delivered.json",
                     {"digest_id": digest["digest_id"], "at": digest["generated_at"], "recipient": recipient})
    return {"sent": ok, "recipient": recipient, "reach": getattr(sent, "reach", None),
            "reason": None if ok else (getattr(sent, "error", None) or "HERALD did not accept it")}


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """One consolidation pass: persist the digest; deliver it when asked."""
    target, refusal = resolve_data_path(params, ctx)
    if refusal is not None:
        return _refuse(BAD_REQUEST, refusal)
    assert target is not None
    if params.get("scheduled") and not Path(target).exists():
        return SkillResult(ok=True, value={"skipped_run": "no_program", "note": "no program data file on this node"})
    try:
        data = load_program(target, require_listed_owners=False)
    except ProgramError as exc:
        return _refuse(NO_DATA, str(exc))
    digest = build_digest(data, now=_now())
    path = _persist(ctx, digest)
    value: dict[str, Any] = {**digest, "path": str(path)}
    actions = [f"wrote a digest of {digest['count']} proposal(s)"]
    if params.get("deliver"):
        delivery = _deliver(ctx, digest, params, Path(target))
        value["delivery"] = delivery
        if delivery.get("sent"):
            actions.append(f"delivered to {delivery['recipient']}")
    return SkillResult(ok=True, value=value, actions_taken=actions)


def digest(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """The latest persisted digest (read-only)."""
    path = digests_dir(ctx) / "latest.json"
    try:
        latest = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return _refuse(ABSENT, "no consolidation digest yet; run `program consolidate`")
    except ValueError as exc:
        return _refuse(NO_DATA, f"the latest digest is unreadable: {exc}")
    kind = params.get("kind")
    if kind:
        if kind not in entropy.PROPOSAL_KINDS:
            return _refuse(BAD_REQUEST, f"unknown kind {kind!r}; one of {', '.join(entropy.PROPOSAL_KINDS)}")
        latest["proposals"] = [p for p in latest.get("proposals") or [] if p.get("kind") == kind]
        latest["count"] = len(latest["proposals"])
    return SkillResult(ok=True, value=latest)


__all__ = ["build_digest", "digest", "digests_dir", "render_message", "run"]
