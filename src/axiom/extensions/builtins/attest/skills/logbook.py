# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.logbook_list`` and ``attest.logbook_validate``: read-only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillResult

from .. import registry
from ..logbooks import LogbookError, load_logbook
from ._common import fail


def _cosign(t, site: str | None) -> dict[str, Any] | None:
    if t.cosign is None:
        return None
    out: dict[str, Any] = {"roles": list(t.cosign.roles), "site_key": t.cosign.roles_key,
                           "party": t.cosign.party}  # fmt: skip
    if site:
        from ..service import cosign_roles

        out["effective_roles"] = list(cosign_roles(site, t.cosign))
    return out


def _filters(logbook) -> list[dict[str, Any]]:
    from ..field_index import describe

    return describe(logbook)


def _summary(logbook, site: str | None = None) -> dict[str, Any]:
    return {
        "id": logbook.id,
        "handle": logbook.handle or logbook.id,
        "version": logbook.version,
        "display": logbook.display,
        "posture": logbook.posture,
        "not_yet_enforced": list(logbook.not_yet_enforced),
        # False for a log the Log page does not list (the approvals log); maintainers still see it.
        "shown_in_log": logbook.shown_in_log,
        # The fields a reader may filter by (ADR-268 D7), in declaration order.
        "filters": _filters(logbook),
        "types": [
            {"id": t.id, "meanings": list(t.meanings), "roles": list(t.roles), "posture": t.posture,
             "cosign": _cosign(t, site)}  # fmt: skip
            for t in logbook.types
        ],
    }


def list_logbooks(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    """Every log on the node, or with ``logbook`` the one log (``<cli> log <log> about``)."""
    try:
        one = (params or {}).get("logbook")
        logbooks = [registry.get(one)] if one else registry.all_logbooks()
    except LogbookError as exc:
        return fail(str(exc))
    site = (params or {}).get("site") or None
    return SkillResult(ok=True, value={"logbooks": [_summary(b, site) for b in logbooks]})


def validate(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    path = params.get("path")
    if not path:
        return fail("give the logbook file to validate")
    try:
        logbook = load_logbook(Path(path))
    except (LogbookError, OSError) as exc:
        return fail(f"{path}: {exc}")
    except ValueError as exc:  # tomllib.TOMLDecodeError
        return fail(f"{path}: not valid TOML: {exc}")
    value = {**_summary(logbook), "bindings": _bindings(logbook)}
    unbound = [b for b in value["bindings"] if not b["bound"]]
    if unbound:
        named = "; ".join(
            f"{b['kind']} {b['name']!r} names source {b['source']!r}, which no extension on this node "
            f"binds (bind it in a logbook_inputs function with Inputs.{b['kind']}_source)"
            for b in unbound
        )
        return SkillResult(ok=False, value=value, errors=[f"{path}: {named}"])
    return SkillResult(ok=True, value=value)


def _bindings(logbook) -> list[dict[str, Any]]:
    """Each cross-check and predicate with the source it names and whether this node binds it
    (#1816): a source nothing binds is a declaration that can never be acted on."""
    from .. import crosscheck, predicates

    return [
        *({"kind": "crosscheck", "name": c.field, "source": c.source, "bound": crosscheck.bound(c.source)}
          for c in logbook.crosschecks),
        *({"kind": "predicate", "name": p.id, "source": p.source, "bound": predicates.bound(p.source)}
          for p in logbook.predicates),
    ]  # fmt: skip
