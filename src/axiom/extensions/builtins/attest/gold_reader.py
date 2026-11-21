# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The gold door a cross-check or predicate source reads through (#1816, ADR-128).

A source an extension binds (``Inputs.crosscheck_source``, ``Inputs.predicate_source``) answers
from gold, never from silver or bronze and never from the log's own tables. It is handed a
:class:`GoldReader`, which reaches only the platform's gold verbs and only through
:func:`axiom.infra.skill_dispatch.invoke_capability`, the one chokepoint every surface calls
(the GUARD consult, the site rules and the serving guard apply). A door-only gold object (the
log's own ``log_records``) is refused by the verb exactly as for any other caller.
"""

from __future__ import annotations

from typing import Any

#: The gold verbs a source may read through: each answers from the ``gold`` schema only.
GOLD_VERBS = ("data.series", "data.aggregate", "data.latest", "data.show", "data.table")


class GoldReadRefused(RuntimeError):
    """A source asked for something the gold door does not serve, or the verb refused it."""


class GoldReader:
    """Reads gold for one source call. ``ctx`` is a skill context; None builds the default one,
    lazily, at the first read (a source that reads nothing costs nothing)."""

    def __init__(self, ctx: Any = None) -> None:
        self._ctx = ctx

    def _context(self) -> Any:
        if self._ctx is None:
            from axiom.infra.skills import ensure_context

            self._ctx = ensure_context(None)
        return self._ctx

    def read(self, verb: str, **params: Any) -> dict[str, Any]:
        """The value a gold verb answers with; :class:`GoldReadRefused` for anything else."""
        from axiom.infra.skill_dispatch import invoke_capability

        if verb not in GOLD_VERBS:
            raise GoldReadRefused(
                f"{verb!r} is not a gold verb; a source reads gold through one of {list(GOLD_VERBS)}"
            )
        tier = params.get("tier")
        if tier is not None and str(tier).strip().lower() != "gold":
            raise GoldReadRefused(f"a source reads gold only (ADR-128), not {tier!r}")
        ctx = self._context()
        result = invoke_capability(ctx.registry, verb, dict(params), ctx, surface="cli")
        if not getattr(result, "ok", False):
            raise GoldReadRefused("; ".join(getattr(result, "errors", None) or [f"{verb} refused"]))
        return dict(result.value or {})


__all__ = ["GOLD_VERBS", "GoldReadRefused", "GoldReader"]
