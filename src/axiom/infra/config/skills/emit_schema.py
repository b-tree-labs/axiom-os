# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi config emit-schema [--ext <name>] [--check]`` — ADR-065 PR-1.

Lints the JSON Schema file's structural well-formedness. The full
generator (Pydantic model → JSON Schema, with ``--check`` diffing the
on-disk file against the generated one) lands in PR-2; PR-1 ships only
the structural floor so consumers can wire the verb into CI today.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axiom.infra.config.jsonschema_loader import load_jsonschema
from axiom.infra.skills import SkillContext, SkillResult


def emit_schema(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``(params, ctx) -> SkillResult``.

    Required params:
      - ``schema_path``: path to the JSON Schema file (Path | str).

    Optional params:
      - ``check``: bool — when True, verifies the on-disk file is a
        well-formed Draft 2020-12 schema with the expected top-level
        keys. PR-2 extends this to a model-vs-schema diff.
      - ``extension``: optional extension name (carried into the
        result for receipts).
    """
    schema_path = params.get("schema_path")
    if not schema_path:
        return SkillResult(
            ok=False,
            errors=["emit-schema: 'schema_path' is required"],
        )
    check = bool(params.get("check", False))
    extension = params.get("extension")

    path = Path(schema_path)
    try:
        schema = load_jsonschema(path)
    except Exception as exc:
        return SkillResult(
            ok=False,
            errors=[f"emit-schema: {exc}"],
        )

    if check:
        problems: list[str] = []
        if not isinstance(schema, dict):
            problems.append("top-level must be an object")
        else:
            if "$schema" not in schema:
                problems.append(
                    "missing $schema (expected JSON Schema Draft 2020-12 URI)"
                )
            if schema.get("type") != "object":
                problems.append("top-level type must be 'object'")
            if not isinstance(schema.get("properties"), dict):
                problems.append("'properties' must be a non-empty object")
            elif not schema["properties"]:
                problems.append("'properties' is empty")
        if problems:
            return SkillResult(
                ok=False,
                errors=[f"emit-schema --check {path}: {p}" for p in problems],
                value={"extension": extension, "schema_path": str(path)},
            )

    return SkillResult(
        ok=True,
        value={
            "extension": extension,
            "schema_path": str(path),
            "properties": sorted(schema.get("properties", {}).keys()),
            "schema_json": json.dumps(schema, indent=2, sort_keys=True),
        },
        actions_taken=[f"linted {path}"],
    )


__all__ = ["emit_schema"]
