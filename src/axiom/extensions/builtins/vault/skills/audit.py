# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``vault.audit`` — the vault's records, in two sections. Metadata only, never a value.

There were two ``audit`` verbs. ``secrets audit`` listed every credential's
expiry and always passed; ``vault audit`` listed only the flagged ones and
failed on any. ADR-218 makes them one verb with two sections:

* **expiry** (``expiry=True``): every credential's health from the one
  credential index, and the flagged ones (``expired``, ``expiring``,
  ``no_expiry``, ``unparseable``). ``no_expiry`` is a finding, not health: a
  credential with no recorded expiry cannot be audited by anything, and the
  first symptom is a 401, which is how a live token was lost.
* **access** (``access=True``): who reached the vault's verbs, over which
  surface, from the capability ledger. It is PARTIAL by construction (it sees
  what passed through ``invoke_capability``), and says so, because "no rows"
  must not read as "never called".

Both run by default. Expiry findings make the result not-ok, unless
``report_only`` is set; that is how ``axi vault audit`` keeps its old exit
code through the deprecation window. Access never makes it red.

The top-level keys of the old ``vault.audit`` (``findings``, ``flagged``,
``by_level``, ``credential_name``...) are kept when the expiry section runs, so
callers that read them keep working.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult, ensure_context

#: Levels that mean a person should look. ``no_expiry`` is among them by design.
_FINDINGS = ("expired", "expiring", "no_expiry", "unparseable")

#: The capability namespaces whose calls are the vault's access record.
#: ``secrets`` is the deprecated alias namespace; its calls are the vault's too.
ACCESS_NAMESPACES = ("vault", "secrets")


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


def _sections(params: dict[str, Any]) -> tuple[bool, bool]:
    """Which sections run. Neither named means both; naming one means that one."""
    expiry, access = params.get("expiry"), params.get("access")
    if expiry is None and access is None:
        return True, True
    return _flag(expiry, False), _flag(access, False)


def _expiry(params: dict[str, Any], ctx: SkillContext) -> tuple[dict[str, Any], list[str], list[str]]:
    from axiom.extensions.builtins.secrets.skills import audit as expiry_core

    horizon = int(params.get("within_days") or expiry_core.DEFAULT_WITHIN_DAYS)
    inner = expiry_core.run({**params, "within_days": horizon}, ctx)
    if not inner.ok:
        raise RuntimeError("; ".join(inner.errors) or "expiry audit failed")
    every = list((inner.value or {}).get("findings", []))
    flagged = [f for f in every if f.get("level") in _FINDINGS]
    by_level: dict[str, list[str]] = {}
    for finding in flagged:
        by_level.setdefault(str(finding.get("level")), []).append(str(finding.get("name")))
    section = {
        "within_days": horizon,
        "total": len(every),
        "flagged": len(flagged),
        # A ref (ADR-185): the first flagged credential, so the next step can be
        # handed a name instead of having to read the findings.
        "credential_name": str(flagged[0].get("name") or "") if flagged else "",
        "by_level": by_level,
        "counts": dict((inner.value or {}).get("counts", {})),
        "findings": flagged,
        "all": every,
    }
    errors = [
        f"{f['name']}: {f['level']}" + (f" (expires {f['expires_at']})" if f.get("expires_at") else "")
        for f in flagged
    ]
    return section, errors, [f"audited {len(every)} credential(s) over {horizon} days"]


def _access(params: dict[str, Any], ctx: SkillContext) -> tuple[dict[str, Any], list[str]]:
    from axiom.extensions.builtins.vault.skills import canonical_skill_name
    from axiom.infra.capability_telemetry import _read

    since = params.get("since")
    since_ts = float(since) if isinstance(since, (int, float)) else None
    verbs: dict[str, dict[str, Any]] = {}
    for row in _read(ctx.state_dir):
        name = str(row.get("tool_name") or "")
        if name.split(".", 1)[0] not in ACCESS_NAMESPACES:
            continue
        ts = row.get("ts")
        if not isinstance(ts, (int, float)) or (since_ts is not None and ts < since_ts):
            continue
        verb = canonical_skill_name(name)
        entry = verbs.setdefault(verb, {
            "verb": verb, "calls": 0, "failures": 0, "surfaces": {},
            "principals": set(), "last_ts": 0.0, "via_deprecated_name": 0,
        })
        entry["calls"] += 1
        entry["last_ts"] = max(entry["last_ts"], float(ts))
        surface = str(row.get("surface") or "?")
        entry["surfaces"][surface] = entry["surfaces"].get(surface, 0) + 1
        if row.get("principal"):
            entry["principals"].add(str(row["principal"]))
        if row.get("ok") is False:
            entry["failures"] += 1
        if name != verb:
            entry["via_deprecated_name"] += 1
    rows = sorted(verbs.values(), key=lambda e: -e["last_ts"])
    for entry in rows:
        entry["principals"] = sorted(entry["principals"])
        entry["last_at"] = datetime.fromtimestamp(entry["last_ts"], UTC).isoformat(timespec="seconds")
    section = {
        "verbs": rows,
        "calls": sum(e["calls"] for e in rows),
        "partial": True,
        "note": (
            "the access record holds the calls that went through the capability dispatcher "
            "(CLI, MCP, chat, agents); a process that read the store directly is not in it"
        ),
    }
    return section, [f"read {section['calls']} vault call(s) from the access record"]


def run(params: dict[str, Any], ctx: SkillContext | None = None) -> SkillResult:
    """The vault's records: expiry health, access, or both."""
    ctx = ensure_context(ctx)
    params = dict(params or {})
    want_expiry, want_access = _sections(params)
    value: dict[str, Any] = {"sections": []}
    errors: list[str] = []
    actions: list[str] = []
    flagged = 0

    if want_expiry:
        try:
            section, expiry_errors, expiry_actions = _expiry(params, ctx)
        except Exception as exc:  # noqa: BLE001
            return SkillResult(ok=False, errors=[f"expiry audit failed: {exc}"])
        value["sections"].append("expiry")
        value["expiry"] = section
        # The old vault.audit's top-level shape, kept for its callers.
        for key in ("within_days", "total", "flagged", "credential_name", "by_level", "findings"):
            value[key] = section[key]
        flagged = section["flagged"]
        errors += expiry_errors
        actions += expiry_actions

    if want_access:
        try:
            section, access_actions = _access(params, ctx)
        except Exception as exc:  # noqa: BLE001 - the record is advisory; say why it is missing
            section, access_actions = {"verbs": [], "calls": 0, "partial": True,
                                       "error": f"access record unreadable: {exc}"}, []
        value["sections"].append("access")
        value["access"] = section
        actions += access_actions

    report_only = _flag(params.get("report_only"), False)
    return SkillResult(
        # Findings make this red. A green audit alongside a credential nobody
        # can renew is the state that let one die.
        ok=report_only or not flagged,
        value=value,
        actions_taken=actions,
        errors=[] if report_only else errors,
    )


__all__ = ["ACCESS_NAMESPACES", "run"]
