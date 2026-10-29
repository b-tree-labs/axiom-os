# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The write core the mutation verbs share (prd-program R2/R6, ADR-166/167).

The program data file is the authoritative editable source of truth. The
read side answers from it; this module is how ``program person|lane|item|
invite|redeem`` *change* it, under the same guarantees the rest of the
extension keeps:

- **Lossless + validated.** Every edit mutates a deep copy of ``data.raw``
  and goes back through :func:`..model.save_program`, which re-validates on
  the way out — a process that can write a file the next reader refuses is
  the bug this avoids. Unknown fields and binding blocks ride through
  untouched.
- **Logged.** Every edit appends to the append-only change log with a stable
  ``kind`` (:data:`.._changelog.CHANGE_KINDS`), the ``old``→``new`` values,
  a ``ts``, and ``by`` — the acting principal, so an owner change names *who*
  reassigned it and *when*, immutably, even after the old owner leaves the
  roster. The change log is history; ``data.json`` carries only the current
  state.
- **Idempotent against sync.** A commit advances ``snapshot.json`` to the new
  state under the same exclusive lock ``sync`` takes, so a later
  self-reconcile does not re-log an edit the mutation already logged.
- **Identity-gated.** A mutation is deputy-gated in-body (ADR-114 is a
  *transport* gate and cannot read the data file's deputy; the data-driven
  decision lives here). The skills also declare ``surfaces=("cli",)`` so the
  anonymous MCP surface never reaches a writer — the structural floor
  ``sync``/``render`` already use.

Nothing here names a vendor, a domain, or a harness (prd-program R10). A
tracker "system" is whatever the deployment's ``program.tracker.kind`` says;
an account map's keys are the deployment's; GitLab/GitHub/a harness need not
be present for any verb to work.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom.infra.principal import node_posture
from axiom.infra.skills import SkillContext, SkillResult
from axiom.infra.state import LockedJsonFile

from ..model import (
    _PRINCIPAL_RE,  # noqa: PLC2701 — the one principal-shape rule, shared
    ProgramData,
    ProgramError,
    ProgramValidationError,
    load_program,
    save_program,
    validate_program,
)
from . import _changelog as cl
from ._source import ABSENT, BAD_REQUEST, NO_DATA, resolve_data_path

#: A mutation refused because the caller is not allowed to make it.
FORBIDDEN = "forbidden"
#: A mutation refused because it would leave the program inconsistent (a lane
#: that still owns items, a duplicate id, a dangling owner reference).
CONFLICT = "conflict"


def refuse(kind: str, message: str) -> SkillResult:
    """A typed refusal, in the same ``value["refused"]`` shape the reads use."""
    return SkillResult(ok=False, value={"refused": kind}, errors=[message])


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def caller(ctx: SkillContext) -> str:
    """The acting principal handle — stamped by dispatch onto ``ctx.principal``,
    never read from params (which a caller controls)."""
    return ctx.principal.handle


# ---- load for edit --------------------------------------------------------


def load_for_edit(
    params: dict[str, Any], ctx: SkillContext
) -> tuple[ProgramData | None, Path | None, SkillResult | None]:
    """Resolve the data file and load it strictly for mutation.

    Returns ``(data, path, None)`` on success, or ``(None, None, refusal)``.
    The path seam is the reads' own :func:`resolve_data_path`: a ``data``
    param is honoured on the CLI only, so a served caller can never aim a
    writer at an arbitrary file.
    """
    path, msg = resolve_data_path(params, ctx)
    if msg is not None:
        return None, None, refuse(BAD_REQUEST, msg)
    assert path is not None
    if not Path(path).exists():
        return None, None, refuse(
            NO_DATA,
            f"no program data file at {path}; create one before editing it",
        )
    try:
        data = load_program(path)
    except ProgramValidationError as exc:
        return None, None, SkillResult(
            ok=False, value={"refused": NO_DATA}, errors=list(exc.errors)
        )
    except ProgramError as exc:
        return None, None, refuse(NO_DATA, str(exc))
    return data, Path(path), None


# ---- identity gate --------------------------------------------------------


def program_maintainers(data: ProgramData) -> list[str]:
    """Principals a program grants edit authority beyond the deputy.

    Optional deployment data (``program.maintainers``: a list of
    ``@name:context``). Absent or malformed reads as "none", so a program
    that declares no maintainers is governed by its deputy alone.
    """
    raw = data.program.get("maintainers")
    if not isinstance(raw, list):
        return []
    return [m for m in raw if isinstance(m, str) and _PRINCIPAL_RE.match(m)]


def authorize(data: ProgramData, ctx: SkillContext) -> str | None:
    """``None`` when the caller may edit this program, else a refusal message.

    Reuses the platform's identity primitives rather than inventing a rights
    store (prd-program R6, ADR-114 §4, ADR-026):

    - the program **deputy** (``program.deputy``) may act for others — the one
      authority the PRD grants cross-principal write;
    - a declared **maintainer** (``program.maintainers``) holds the edit right;
    - on an **open-posture** node (the solo/dev/air-gapped default, where
      ``ctx.principal`` is the unproven OS principal and the node sets no
      stricter floor) whoever holds the shell is the operator and may edit —
      exactly the platform's open-mode semantics. The gate bites the moment a
      deployment raises its posture (``AXIOM_IDENTITY_POSTURE``), which is the
      moment "who is acting" stops being self-evident.

    When a program ever carries an ``Ownership`` record, this composes with
    ``axiom.memory.ownership.can_exercise`` (ADR-026) for the right-check; the
    deputy/maintainer handles are the data-driven form of the same idea.
    """
    who = caller(ctx)
    deputy = data.deputy()
    if deputy is not None and who == deputy:
        return None
    if who in program_maintainers(data):
        return None
    if not ctx.principal.assured and node_posture() == "open":
        return None
    named = deputy or "(none declared)"
    return (
        f"{who} may not edit this program: editing is reserved to the deputy "
        f"({named}) or a declared maintainer. This node runs at the "
        f"'{node_posture()}' identity posture, so the open-mode operator "
        "allowance does not apply."
    )


# ---- the commit -----------------------------------------------------------


def commit(
    ctx: SkillContext,
    data_path: Path,
    old_data: ProgramData,
    new_data: ProgramData,
    extra_changes: list[dict[str, Any]],
    *,
    source: str = "edit",
) -> list[dict[str, Any]]:
    """Write ``new_data``, append the change log, advance the snapshot.

    ``extra_changes`` are the records the snapshot diff does not produce —
    people, lane leads/names, item labels, invitations. The item/lane/drift
    deltas the snapshot *does* track are derived here from the diff, so an
    edit that touches a tracked field is logged exactly as ``sync`` would log
    it (``owner_changed``, ``date_changed`` …) and is never double-logged by
    a later ``sync`` (the snapshot is advanced in the same locked section).

    The diff's baseline is the last reconciled snapshot when one exists (so an
    un-synced hand-edit is still caught, exactly as ``sync`` catches it), and
    the *pre-edit* state (``snapshot_of(old_data)``) when none does — otherwise
    the first mutation on a never-synced node would diff against nothing and
    report a baseline of adds instead of the one change it made.

    Raises :class:`..model.ProgramValidationError` if ``new_data`` is invalid;
    callers turn that into a typed refusal and nothing is written.
    """
    errors = validate_program(new_data.raw)
    if errors:
        raise ProgramValidationError(data_path, errors)

    ts = now_iso()
    by = caller(ctx)
    recorded: list[dict[str, Any]] = []
    with LockedJsonFile(cl.snapshot_path(ctx), exclusive=True) as snap:
        prior = snap.read()
        old_snapshot = prior if isinstance(prior, dict) and prior else cl.snapshot_of(old_data)
        new_snapshot = cl.snapshot_of(new_data)
        save_program(new_data, data_path)
        diffs = cl.diff_snapshots(old_snapshot, new_snapshot)
        changelog = cl.changelog_path(ctx)
        base_seq = len(cl.read_changelog(changelog))
        for offset, change in enumerate([*diffs, *extra_changes], start=1):
            record = {"seq": base_seq + offset, "ts": ts, "source": source, "by": by, **change}
            cl.append_change(changelog, record)
            recorded.append(record)
        snap.write(new_snapshot)
    return recorded


def log_only(
    ctx: SkillContext,
    changes: list[dict[str, Any]],
    *,
    source: str = "edit",
) -> list[dict[str, Any]]:
    """Append change-log entries that do not touch ``data.json`` (invitations).

    Shares the snapshot lock so the seq numbering never races a concurrent
    ``sync`` or ``commit``; the snapshot itself is unchanged.
    """
    ts = now_iso()
    by = caller(ctx)
    recorded: list[dict[str, Any]] = []
    with LockedJsonFile(cl.snapshot_path(ctx), exclusive=True):
        changelog = cl.changelog_path(ctx)
        base_seq = len(cl.read_changelog(changelog))
        for offset, change in enumerate(changes, start=1):
            record = {"seq": base_seq + offset, "ts": ts, "source": source, "by": by, **change}
            cl.append_change(changelog, record)
            recorded.append(record)
    return recorded


# ---- shared value helpers -------------------------------------------------


def valid_principal(value: Any) -> bool:
    return isinstance(value, str) and bool(_PRINCIPAL_RE.match(value))


def parse_accounts(raw: Any) -> tuple[dict[str, str | None] | None, str | None]:
    """Parse ``["gitlab=foo", "github="]`` into ``{"gitlab": "foo", "github": None}``.

    The keys are the deployment's account systems, uninterpreted. An empty
    value (``gitlab=`` or ``gitlab=none``) records an explicit ``null`` — "no
    account on that system", which the proxy-assignee rule reads. Returns
    ``(accounts, None)`` or ``(None, error)``.
    """
    if raw is None:
        return None, None
    if isinstance(raw, dict):
        return {str(k): (str(v) if v is not None else None) for k, v in raw.items()}, None
    if not isinstance(raw, (list, tuple)):
        return None, "accounts must be a list of system=username pairs"
    out: dict[str, str | None] = {}
    for item in raw:
        if not isinstance(item, str) or "=" not in item:
            return None, f"account {item!r} must be of the form system=username (username may be empty)"
        system, _, username = item.partition("=")
        system = system.strip()
        username = username.strip()
        if not system:
            return None, f"account {item!r} names no system"
        out[system] = username or None
    return out, None


def tracker_system(data: ProgramData) -> str | None:
    """The account system an item's assignment is computed against — the
    program's declared tracker ``kind`` (deployment data: ``gitlab``,
    ``github``, or anything else), or ``None`` when no tracker is declared."""
    kind = data.tracker().get("kind")
    return kind if isinstance(kind, str) and kind else None


def proxy_assignee(data: ProgramData, item: dict[str, Any]) -> dict[str, Any] | None:
    """Compute ``item['assignment']`` per the ADR-166 proxy-assignee rule.

    For an item whose real owner has no account on the program's tracker
    system, the *intended* assignee — what a later posting phase would set on
    the tracker — is the lane lead (``lanes[].lead``) when the lead has an
    account, else the deputy (``program.deputy``) when the deputy has one,
    else none. The **real owner is named in every case**; the proxy is only
    who a tracker item would point at. Nothing here writes to any tracker.

    Returns the assignment dict to record, or ``None`` when no assignment is
    warranted (no tracker system declared, or the owner has an account and so
    needs no proxy). Returning ``None`` is the caller's signal to clear a
    stale ``assignment``.
    """
    owner = item.get("owner")
    system = tracker_system(data)
    if not owner or system is None:
        return None
    if data.account(owner, system):
        # The owner can be assigned directly; no proxy, no assignment record.
        return None

    proxy: str | None = None
    via = "none"
    lead = data.lane_lead(item.get("lane"))
    if lead and data.account(lead, system):
        proxy, via = lead, "lane_lead"
    elif data.deputy() and data.account(data.deputy(), system):
        proxy, via = data.deputy(), "deputy"

    return {
        "owner": owner,  # the real owner, named regardless
        "system": system,
        "proxy": proxy,
        "via": via,
        "reason": f"owner {owner} has no {system} account; "
        + (f"tracker item would point at {proxy} ({via})" if proxy else "no proxy in the chain has one"),
    }


def apply_owner(data: ProgramData, item: dict[str, Any], new_owner: str | None) -> None:
    """Set an item's ``owner`` and recompute its ``assignment`` in place.

    The proxy-assignee rule (ADR-166) is a function of the *current* owner and
    the program's leads/accounts, so any owner change recomputes it:
    ``assignment`` is set when a proxy is warranted and cleared otherwise.
    ``data`` must be the post-edit document so the lookups see the new owner.
    """
    if new_owner is None:
        item.pop("owner", None)
    else:
        item["owner"] = new_owner
    assignment = proxy_assignee(data, item)
    if assignment is None:
        item.pop("assignment", None)
    else:
        item["assignment"] = assignment


def resolve_member_identity(handle: str) -> dict[str, Any]:
    """Resolve a ``@name:context`` through the directory seam when configured.

    DRY (ADR-103 / ADR-167): the program keeps no parallel people registry.
    A member is a principal (the grammar is the platform's one rule,
    :data:`.._PRINCIPAL_RE`) layered with program lane/role. When a directory
    provider is configured (``AXIOM_DIRECTORY_PROVIDER`` ≠ ``none``) the handle
    is resolved through :class:`directory.MembershipResolver` for advisory
    org roles/groups; when none is, the supplied handle is accepted as given.

    Never blocks membership and never reaches the network hard: a provider
    error, a missing claim, or an unimportable directory extension all degrade
    to "accepted" (ADR-166: feeders never gate membership on an account, and
    the seam degrades rather than raising). Returns an advisory dict; it is NOT
    written into the data file.
    """
    advisory: dict[str, Any] = {"handle": handle, "resolved_via": "as-given"}
    try:
        from axiom.extensions.builtins.directory import (  # local import: optional dependency
            MembershipResolver,
            PrincipalRef,
            load_directory_config,
        )

        cfg = load_directory_config()
        if getattr(cfg, "provider", None) is None:
            return advisory
        name = handle.lstrip("@").split(":", 1)[0]
        ref = PrincipalRef(subject=name, provider=getattr(cfg, "provider_name", "") or "program")
        membership = MembershipResolver(provider=cfg.provider, role_map=cfg.role_map).resolve(ref)
        advisory.update(
            resolved_via="directory",
            roles=list(getattr(membership, "roles", ()) or ()),
            stale=bool(getattr(membership, "stale", False)),
            source=getattr(membership, "source", "none"),
        )
    except Exception:  # noqa: BLE001 — the directory is optional and degrades, never blocks
        advisory["resolved_via"] = "as-given"
    return advisory


def mutable_copy(data: ProgramData) -> ProgramData:
    """A deep copy of the data file a verb may edit without touching the
    loaded original (which the caller still reads for pre-checks)."""
    return ProgramData(raw=copy.deepcopy(data.raw))


__all__ = [
    "ABSENT",
    "BAD_REQUEST",
    "CONFLICT",
    "FORBIDDEN",
    "NO_DATA",
    "apply_owner",
    "authorize",
    "caller",
    "commit",
    "load_for_edit",
    "log_only",
    "mutable_copy",
    "now_iso",
    "parse_accounts",
    "program_maintainers",
    "proxy_assignee",
    "refuse",
    "resolve_member_identity",
    "tracker_system",
    "valid_principal",
]
