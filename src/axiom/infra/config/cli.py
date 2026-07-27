# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi config {validate|show|emit-schema}`` — ADR-065 PR-1.

Thin argparse wrapper over the three skill functions in
``axiom.infra.config.skills.*``. Per ADR-056, this module never holds
business logic — it parses args, builds the skill params + context, and
calls the registered skill.

Dispatched from ``axiom.setup.cli.main`` so that ``axi config`` keeps
its existing wizard behaviour when called bare or with the legacy
flags (``--status`` / ``--set`` / ``--reset`` / ``--model`` /
``--dry-run``) and only diverts to here when the first positional arg
is one of the ADR-065 verbs.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from axiom.infra.config.skills.emit_schema import emit_schema
from axiom.infra.config.skills.show import show_effective
from axiom.infra.config.skills.validate import validate_config
from axiom.infra.skills import SkillContext, SkillRegistry, SkillResult


CONFIG_VERBS = ("validate", "show", "emit-schema")


def _registry_with_verbs() -> SkillRegistry:
    """Build a registry pre-loaded with the three ADR-065 verbs.

    Kept local to PR-1: the global SkillRegistry wiring lands when
    the broader config-namespace registration story (ADR-056 layering)
    is settled.
    """
    reg = SkillRegistry()
    reg.register("config.validate", validate_config)
    reg.register("config.show", show_effective)
    reg.register("config.emit-schema", emit_schema)
    return reg


def _make_ctx() -> SkillContext:
    return SkillContext(
        registry=_registry_with_verbs(),
        state_dir=Path.home() / ".axi",
        logger=logging.getLogger("axiom.infra.config.cli"),
    )


def _print_result(result: SkillResult) -> None:
    if result.ok:
        print(json.dumps(result.value, indent=2, sort_keys=True, default=str))
    else:
        for err in result.errors:
            print(f"error: {err}", file=sys.stderr)


def _bootstrap_extension(extension: str, schema_path: Path | None) -> Path | None:
    """Best-effort schema-path resolution + registration.

    For PR-1 we accept the schema path on the CLI (``--schema``) so the
    verbs work without a manifest discovery layer. PR-2 wires
    ``axiom-extension.toml [config]`` discovery so the bare
    ``axi config validate <ext>`` form resolves automatically.
    """
    if schema_path is None:
        return None
    schema_path = Path(schema_path)
    if not schema_path.exists():
        print(f"error: schema not found: {schema_path}", file=sys.stderr)
        return None
    try:
        from axiom.infra.config import register_schema_from_jsonschema

        register_schema_from_jsonschema(extension, schema_path)
    except Exception as exc:
        # Non-fatal — `validate` still runs against the raw schema.
        print(f"warning: register_schema_from_jsonschema: {exc}", file=sys.stderr)
    return schema_path


def cmd_validate(args: argparse.Namespace) -> int:
    schema_path = _bootstrap_extension(args.extension, args.schema)
    ctx = _make_ctx()
    result = ctx.registry.invoke(
        "config.validate",
        {
            "extension": args.extension,
            "schema_path": str(schema_path) if schema_path else args.schema,
            "config_path": args.config,
        },
        ctx,
    )
    _print_result(result)
    return result.exit_code


def cmd_show(args: argparse.Namespace) -> int:
    _bootstrap_extension(args.extension, args.schema)
    ctx = _make_ctx()
    result = ctx.registry.invoke(
        "config.show",
        {"extension": args.extension},
        ctx,
    )
    _print_result(result)
    return result.exit_code


def cmd_emit_schema(args: argparse.Namespace) -> int:
    ctx = _make_ctx()
    result = ctx.registry.invoke(
        "config.emit-schema",
        {
            "schema_path": args.schema,
            "check": args.check,
            "extension": args.extension,
        },
        ctx,
    )
    _print_result(result)
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi config",
        description="Schema-bilingual config verbs (ADR-065).",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p_val = sub.add_parser("validate", help="Validate a config file against its schema")
    p_val.add_argument("extension")
    p_val.add_argument("--schema", required=True, help="Path to JSON Schema file")
    p_val.add_argument("--config", default=None, help="Path to config JSON file")
    p_val.set_defaults(func=cmd_validate)

    p_show = sub.add_parser("show", help="Show effective config")
    p_show.add_argument("extension")
    p_show.add_argument("--effective", action="store_true", help="Print effective merged values")
    p_show.add_argument("--schema", default=None, help="Schema path for ad-hoc bootstrap")
    p_show.set_defaults(func=cmd_show)

    p_emit = sub.add_parser("emit-schema", help="Lint / emit a JSON Schema")
    p_emit.add_argument("--ext", dest="extension", default=None)
    p_emit.add_argument("--schema", required=True, help="Path to JSON Schema file")
    p_emit.add_argument("--check", action="store_true", help="Lint mode; non-zero on mismatch")
    p_emit.set_defaults(func=cmd_emit_schema)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
