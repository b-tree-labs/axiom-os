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
        bronze_root=bronze_root,
        rag_dsn_env=params.get("rag_dsn_env", "DP1_RAG_DSN"),
        provenance_rules_file=params.get("provenance_rules_file"),
        default_disposition=params.get("default_disposition", "quarantine"),
        default_tier=params.get("default_tier", "rag-community"),
        params=params.get("kind_params") or {},
    )

    errors = provider.validate(config)
    if errors:
        return SkillResult(ok=False, errors=errors)

    # Idempotent on identical config.
    try:
        existing = load_connector(name, state_dir=ctx.state_dir)
    except FileNotFoundError:
        existing = None

    if existing == config:
        return SkillResult(
            ok=True,
            value={"name": name, "kind": kind, "changed": False,
                   "path": str(Path(ctx.state_dir) / "plinth" / "connectors" / f"{name}.toml")},
            actions_taken=[f"no-op (already registered): {name}"],
        )

    if existing is not None and not params.get("force"):
        return SkillResult(
            ok=False,
            errors=[f"connector {name!r} already exists with different fields; "
                    "pass --force to overwrite"],
        )

    actor = params.get("actor")
    with _authz.action(
        verb="register",
        resource=f"data-platform://connector/{name}",
        classification=Classification.INTERNAL,
        actor=actor,
    ) as act:
        path = save_connector(config, state_dir=ctx.state_dir)
    return SkillResult(
        ok=True,
        value={"name": name, "kind": kind, "changed": True, "path": str(path)},
        actions_taken=[
            f"registered {kind} connector {name!r} → {path}",
            f"audit-receipt: {act.receipt_id}",
        ],
    )
