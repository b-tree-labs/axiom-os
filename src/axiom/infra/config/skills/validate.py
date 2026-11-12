# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi config validate <ext> [--config <path>]`` — ADR-065 PR-1.

Runs ``jsonschema.validate`` against the extension's declared schema.
The Pydantic ceiling check (PR-2) is gated on a declared model and
no-ops until the ceiling lands.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from axiom.infra.config.jsonschema_loader import load_jsonschema
from axiom.infra.skills import SkillContext, SkillResult


def validate_config(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``(params, ctx) -> SkillResult``.

    Required params:
      - ``extension``: extension name (str).
      - ``schema_path``: path to the JSON Schema file (Path | str).

    Optional params:
      - ``config_path``: path to the config JSON to validate (Path | str).
        If omitted, the skill only loads + structurally validates the schema.
    """
    extension = params.get("extension")
    schema_path = params.get("schema_path")
    config_path = params.get("config_path")

    if not extension or not schema_path:
        return SkillResult(
            ok=False,
            errors=["validate: 'extension' and 'schema_path' are required"],
        )

    try:
        schema = load_jsonschema(Path(schema_path))
    except Exception as exc:
        return SkillResult(
            ok=False,
            errors=[f"validate: schema load failed: {exc}"],
        )

    if config_path is None:
        return SkillResult(
            ok=True,
            value={"extension": extension, "schema_loaded": True},
            actions_taken=[f"loaded schema {schema_path}"],
        )

    try:
        config_text = Path(config_path).read_text(encoding="utf-8")
        config = json.loads(config_text)
    except (OSError, json.JSONDecodeError) as exc:
        return SkillResult(
            ok=False,
            errors=[f"validate: cannot read config {config_path}: {exc}"],
        )

    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return SkillResult(
            ok=False,
            errors=[
                "validate: the 'jsonschema' package is required "
                "(pip install 'jsonschema>=4.20')"
            ],
        )

    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(config), key=lambda e: e.path)
    if errors:
        diagnostics = []
        for err in errors:
            pointer = "/" + "/".join(str(p) for p in err.absolute_path)
            diagnostics.append(f"{pointer}: {err.message}")
        return SkillResult(
            ok=False,
            errors=diagnostics,
            value={"extension": extension, "config_path": str(config_path)},
        )

    return SkillResult(
        ok=True,
        value={
            "extension": extension,
            "config_path": str(config_path),
            "validated_keys": list(config.keys()) if isinstance(config, dict) else [],
        },
        actions_taken=[f"validated {config_path} against {schema_path}"],
    )


__all__ = ["validate_config"]
