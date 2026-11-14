# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``memory.port`` skill — one-command cross-harness onboarding (ADR-087).

The single verb a colleague runs after installing the platform on a node
where one or more harnesses already live. Composes the existing
primitives, in order:

1. **MCP registration** — ``axi mcp install``'s installer registers the
   aggregation server into every detected client (additive, idempotent;
   never repoints a client's model).
2. **Absorb** — ``memory.absorb`` per harness whose native store is
   present (or an explicit list), landing the harness's memory through
   the D2 import primitive. Re-absorb is echo-suppressed.
3. **Bundle import** — optional: a signed export bundle (machine move /
   account port) lands via ``memory.import_bundle``, assumed as the
   porting principal.
4. **Recall reindex** — ``refresh`` mode only: rebuild the principal's
   recall projection to repair index drift. Initial ports don't need it
   (write-time indexing covers absorbed fragments).

Initial install and forced refresh are the same verb: every step is
idempotent, so ``port`` is always safe to re-run; ``refresh`` adds the
reindex repair.

Per-step failures are collected, not fatal — a harness with an
unreadable store must not block the others — but any failure makes the
overall result non-ok so callers never mistake a partial port for a
complete one.

Params:

- ``composition`` (required), ``principal`` (required, already resolved).
- ``harnesses`` — explicit list; default = detected among
  :data:`DETECTABLE_HARNESSES` by store presence under ``home``.
- ``account``, ``home``, ``roots`` — passed through to absorb.
- ``bundle`` — optional path to a signed bundle; ``sessions_dir``
  passes through to the import.
- ``refresh`` — force the recall reindex after absorb/import.
- ``dry_run`` — propagated to every step; the reindex (a repair write
  with no meaningful dry form) is skipped.
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

# Harness → store directory (relative to home) whose presence turns
# detection on. Only harnesses with a home-anchored store belong here;
# path-parameterized adapters (goose, letta) need explicit ``harnesses``.
DETECTABLE_HARNESSES: dict[str, str] = {
    "claude-code": ".claude",
    "codex": ".codex",
    "gemini-cli": ".gemini",
    "hermes": ".hermes",
}


def _detect_harnesses(home: Path) -> list[str]:
    return [
        name
        for name, store in DETECTABLE_HARNESSES.items()
        if (home / store).is_dir()
    ]


def port(params: dict[str, Any], ctx: SkillContext | None) -> SkillResult:
    composition = params.get("composition")
    principal = params.get("principal")
    if composition is None or not principal:
        return SkillResult(
            ok=False, errors=["port requires composition and principal"],
        )

    home_param = params.get("home")
    home = Path(home_param) if home_param else Path.home()
    dry_run = bool(params.get("dry_run"))
    refresh = bool(params.get("refresh"))
    harnesses = list(params.get("harnesses") or _detect_harnesses(home))

    errors: list[str] = []
    actions: list[str] = []
    report: dict[str, Any] = {
        "principal": principal,
        "refresh": refresh,
        "dry_run": dry_run,
        "mcp": None,
        "harnesses": {},
        "bundle": None,
        "reindex": None,
    }

    # -- 1. MCP registration (additive, idempotent) --------------------------
    try:
        from axiom.extensions.builtins.mcp import install as mcp_install

        report["mcp"] = mcp_install.install(dry_run=dry_run)
        actions.append("mcp-install")
    except Exception as exc:  # noqa: BLE001 — collected, not fatal
        report["mcp"] = {"error": str(exc)}
        errors.append(f"mcp install failed: {exc}")

    # -- 2. Absorb each harness's native memory ------------------------------
    # import_module (not ``from . import``): the package __init__ re-exports
    # same-named functions, which shadow the submodules on attribute access.
    absorb_mod = import_module("axiom.extensions.builtins.memory.skills.absorb")

    for harness in harnesses:
        result = absorb_mod.absorb({
            "composition": composition,
            "harness": harness,
            "account": params.get("account"),
            "principal": principal,
            "home": home_param,
            "roots": params.get("roots"),
            "dry_run": dry_run,
        }, ctx)
        if result.ok:
            report["harnesses"][harness] = result.value
            actions.append(f"absorb:{harness}")
        else:
            report["harnesses"][harness] = {"errors": result.errors}
            errors.extend(f"absorb {harness}: {e}" for e in result.errors)

    # -- 3. Optional signed-bundle import (machine move) ---------------------
    bundle = params.get("bundle")
    if bundle:
        import_mod = import_module(
            "axiom.extensions.builtins.memory.skills.import_bundle"
        )
        result = import_mod.import_bundle({
            "composition": composition,
            "bundle": bundle,
            "assume_principal": principal,
            "dry_run": dry_run,
            "sessions_dir": params.get("sessions_dir"),
            "home": home_param,
        }, ctx)
        if result.ok:
            report["bundle"] = result.value
            actions.append("import-bundle")
        else:
            report["bundle"] = {"errors": result.errors}
            errors.extend(f"import: {e}" for e in result.errors)

    # -- 4. Refresh: rebuild the recall projection ---------------------------
    if refresh:
        if dry_run:
            report["reindex"] = {"action": "skipped-dry-run"}
        else:
            reindex_mod = import_module(
                "axiom.extensions.builtins.memory.skills.reindex_recall"
            )
            result = reindex_mod.reindex_recall({
                "composition": composition,
                "principal": principal,
                "all": False,
            }, ctx)
            if result.ok:
                report["reindex"] = result.value
                actions.append("reindex-recall")
            else:
                report["reindex"] = {"errors": result.errors}
                errors.extend(f"reindex: {e}" for e in result.errors)

    return SkillResult(
        ok=not errors, value=report, errors=errors, actions_taken=actions,
    )
