# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``SkillExecutor`` — the production :class:`~.engine.Executor`.

The PULSE-1 harden audit left the engine's ``Executor`` as a protocol:
tests drove doubles, and nothing in production turned a stored cadence's
``action`` string into real work. This module closes that gap with the
obvious, domain-agnostic mapping: **action string == qualified skill
name** (ADR-056), dispatched through :class:`SkillRegistry.invoke`.

Envelope contract: the schedule row's ``capability_envelope`` is a dict
whose optional ``"params"`` key carries the skill's params. PULSE stores
it verbatim and never interprets it; only this executor does.

Failure contract: a skill returning ``ok=False`` (or raising — the
registry wraps that into ``ok=False``) raises :class:`SkillDispatchError`
so the engine's retry / dead-letter machinery engages. Swallowing a
failed SkillResult would record the fire as ``success`` and defeat the
alerting chain.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillRegistry


class SkillDispatchError(RuntimeError):
    """A scheduled skill dispatch failed (missing skill or ok=False).

    The message stays the flat string the logs and the fire log have always
    carried. The structure the failed ``SkillResult`` had travels with it, so
    the dead-letter hook can hand the alert desk the cause and the fix
    (ADR-185 ``unmet``, ADR-249) instead of an exception string:

    - ``error_class``: the underlying error's class when the skill raised
      (``"KeyError: ..."`` -> ``"KeyError"``), ``SkillFailed`` for a plain
      ``ok=False``, ``SkillNotRegistered`` when nothing answers the action.
    - ``unmet``: the result's structured ``{id, why, fix}`` entries.
    - ``errors``: the result's error lines.
    """

    def __init__(
        self,
        message: str,
        *,
        error_class: str = "SkillFailed",
        unmet: list[dict[str, Any]] | None = None,
        errors: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_class = error_class
        self.unmet = list(unmet or [])
        self.errors = list(errors or [])


#: A CamelCase class name opening an error line, as ``SkillRegistry`` writes a
#: raised skill (``"KeyError: ..."``). One capitalised word (``"Note: ..."``)
#: is prose, not a class.
_RAISED = re.compile(r"^((?:[A-Z][a-z0-9_]*){2,}): ")


def _error_class(errors: list[str]) -> str:
    """The class named by a raised skill's first error line, else ``SkillFailed``."""
    for line in errors:
        m = _RAISED.match(line or "")
        if m:
            return m.group(1)
    return "SkillFailed"


class SkillExecutor:
    """Maps ``action`` strings onto SkillRegistry invocations.

    Satisfies the engine's ``Executor`` protocol
    (``run(action, envelope) -> receipt``). The returned receipt is the
    skill's ``value["receipt"]`` when present (e.g. the data-platform
    skills return their authz receipt-id) — the engine writes it to the
    fire log for the audit trail.
    """

    def __init__(
        self,
        registry: SkillRegistry,
        *,
        state_dir: Path | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._registry = registry
        self._state_dir = state_dir
        self._logger = logger or logging.getLogger("axiom.schedule.executor")

    def run(self, action: str, envelope: Any) -> str | None:
        if not self._registry.has(action):
            raise SkillDispatchError(
                f"no skill registered as {action!r} — is the providing "
                "extension bound into this registry?",
                error_class="SkillNotRegistered",
            )

        params: dict[str, Any] = {}
        if isinstance(envelope, dict) and isinstance(envelope.get("params"), dict):
            params = dict(envelope["params"])

        state_dir = self._state_dir
        if state_dir is None:
            from axiom.infra.paths import get_user_state_dir

            state_dir = get_user_state_dir()

        ctx = SkillContext(
            registry=self._registry,
            state_dir=state_dir,
            logger=self._logger,
            user_prompt=None,  # headless: skills must not block on input
        )
        result = self._registry.invoke(action, params, ctx)
        if not result.ok:
            raise SkillDispatchError(
                f"{action} failed: " + ("; ".join(result.errors) or "ok=False"),
                error_class=_error_class(result.errors),
                unmet=[u for u in (result.unmet or []) if isinstance(u, dict)],
                errors=list(result.errors),
            )
        self._logger.info("dispatched %s: %s", action, "; ".join(result.actions_taken) or "ok")
        if isinstance(result.value, dict):
            receipt = result.value.get("receipt")
            return str(receipt) if receipt is not None else None
        return None


__all__ = ["SkillDispatchError", "SkillExecutor"]
