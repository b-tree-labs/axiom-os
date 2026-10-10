# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``data.register`` — persist a connector config (kind-aware).

The skill is source-kind agnostic: it dispatches to the kind's
:class:`SourceKindProvider` for validation, then saves a generic
:class:`ConnectorConfig`. CLI maps as ``axi data register <name> <kind>
[kind-specific flags] [platform-generic flags]`` — adding a new
source kind never touches this file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.governance.classification import Classification
from axiom.infra.skills import SkillContext, SkillResult

from .. import _authz
from ..agents.plinth.connectors import (
    ConnectorConfig,
    load_connector,
    save_connector,
)
from ..sources import default_source_kind_registry


def _validate_promotion_map(path: str) -> list[str]:
    """Load + validate a declared promotion map at register time so a bad map is
    a register error, not a runtime crash (ADR-001 D4)."""
    from ..promote import load_promotion_map

    try:
        pmap = load_promotion_map(path)
    except FileNotFoundError:
        return [f"promotion map not found: {path}"]
    except Exception as exc:  # noqa: BLE001 — bad TOML → a register error, not a crash
        return [f"promotion map {path} is not valid TOML: {exc}"]
    return [f"promotion map {path}: {e}" for e in pmap.validate()]


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """Persist a connector config.

    Required params: ``name``, ``kind``, ``bronze_root`` + the kind's
    own required params (in ``kind_params``).
    """
    name = params.get("name")
    kind = params.get("kind")
    bronze_root = params.get("bronze_root")
    if not name:
        return SkillResult(ok=False, errors=["missing required param: name"])
    if not kind:
        return SkillResult(ok=False, errors=["missing required param: kind"])
    if not bronze_root:
        return SkillResult(ok=False, errors=["missing required param: bronze_root"])

    registry = default_source_kind_registry()
    try:
        provider = registry.get(kind)
    except KeyError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    config = ConnectorConfig(
        name=name,
        kind=kind,
        site=params.get("site") or None,
        bronze_root=bronze_root,
        rag_dsn_env=params.get("rag_dsn_env", "DP1_RAG_DSN"),
        provenance_rules_file=params.get("provenance_rules_file"),
        default_disposition=params.get("default_disposition", "quarantine"),
        default_tier=params.get("default_tier", "rag-community"),
        credential_ref=params.get("credential_ref"),
        promotion_map_file=params.get("promotion_map_file"),
        params=params.get("kind_params") or {},
    )

    errors = provider.validate(config)
    if config.promotion_map_file:
        errors = errors + _validate_promotion_map(config.promotion_map_file)
    if errors:
        return SkillResult(ok=False, errors=errors)

    # A connector that can ACCEPT rows must be able to LAND them. A push
    # connector without a site takes a partner's rows with 2xx forever while
    # conformance silently skips it (unmapped_connectors) — found live on a
    # node where this was true of every push connector. Registration is the
    # moment the operator is present, so refuse here; --allow-unmapped is the
    # explicit escape hatch for a deliberate unattributed sink. Pull kinds
    # keep the warning below: their rows arrive by our own hand.
    if kind == "push" and not config.site and not params.get("allow_unmapped"):
        return SkillResult(
            ok=False,
            errors=[
                f"push connector {name!r} has no --site: it would ACCEPT rows "
                "(2xx) while conformance silently skips them forever "
                "(unmapped_connectors, rows never reach silver). Pass "
                "--site <site> to attribute it, or --allow-unmapped if an "
                "unattributed sink is genuinely intended."
            ],
        )

    # Idempotent on identical config.
    try:
        existing = load_connector(name, state_dir=ctx.state_dir)
    except FileNotFoundError:
        existing = None

    if existing == config:
        return SkillResult(
            ok=True,
            value={
                "name": name,
                "kind": kind,
                "changed": False,
                "path": str(Path(ctx.state_dir) / "plinth" / "connectors" / f"{name}.toml"),
            },
            actions_taken=[f"no-op (already registered): {name}"],
        )

    if existing is not None and not params.get("force"):
        return SkillResult(
            ok=False,
            errors=[
                f"connector {name!r} already exists with different fields; "
                "pass --force to overwrite"
            ],
        )

    actor = params.get("actor")
    with _authz.action(
        verb="register",
        resource=f"data-platform://connector/{name}",
        classification=Classification.INTERNAL,
        actor=actor,
    ) as act:
        path = save_connector(config, state_dir=ctx.state_dir)
    actions = [
        f"registered {kind} connector {name!r} → {path}",
        f"audit-receipt: {act.receipt_id}",
    ]
    if not config.site:
        actions.append(
            f"⚠️  connector {name!r} registered WITHOUT a site: conformance cannot "
            f"attribute its rows, so they land in bronze but never reach "
            f"silver.signals. Re-run with --site <site> to attribute it."
        )
    return SkillResult(
        ok=True,
        value={
            "name": name,
            "kind": kind,
            "changed": True,
            "site": config.site,
            "path": str(path),
        },
        actions_taken=actions,
    )
