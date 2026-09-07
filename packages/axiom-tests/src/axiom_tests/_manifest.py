# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""AEOS manifest loading and validation helpers.

These helpers are the foundation for the ``ExtensionStandardTests`` base
class and for the ``manifest_validator`` pytest fixture. They intentionally
stay dependency-light — ``tomllib`` from the stdlib on Python >= 3.11 and
``jsonschema`` for validation.

The schema file lives at ``axiom_tests/schemas/aeos-manifest-0.1.json`` and
is shipped as package data.
"""

from __future__ import annotations

import json
import tomllib
from importlib import resources
from pathlib import Path
from typing import Any

try:
    import jsonschema
    from jsonschema import Draft202012Validator
except ImportError as exc:  # pragma: no cover - hard requirement
    raise ImportError("axiom-tests requires jsonschema>=4 to validate AEOS manifests") from exc

AEOS_SCHEMA_VERSION = "0.1.0"
_SCHEMA_FILENAME = "aeos-manifest-0.1.json"


class ManifestError(ValueError):
    """Raised when an AEOS manifest is malformed or cannot be loaded."""


def load_schema() -> dict[str, Any]:
    """Load the AEOS JSON Schema bundled with ``axiom-tests``.

    Returns:
        The parsed JSON Schema as a dict.
    """
    files = resources.files("axiom_tests.schemas")
    raw = (files / _SCHEMA_FILENAME).read_text(encoding="utf-8")
    return json.loads(raw)


def build_validator() -> Draft202012Validator:
    """Construct a Draft 2020-12 validator bound to the AEOS schema."""
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def load_manifest(path: str | Path) -> dict[str, Any]:
    """Load an ``axiom-extension.toml`` manifest from disk.

    Args:
        path: Absolute or relative path to the manifest file.

    Returns:
        The parsed TOML content as a dict.

    Raises:
        ManifestError: If the manifest file is missing or malformed.
    """
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise ManifestError(f"manifest not found: {manifest_path}")
    try:
        with manifest_path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"invalid TOML in {manifest_path}: {exc}") from exc


def validate_manifest(
    manifest: dict[str, Any],
    *,
    validator: Draft202012Validator | None = None,
) -> list[str]:
    """Validate a manifest against the AEOS schema.

    Args:
        manifest: Parsed manifest contents (as returned by :func:`load_manifest`).
        validator: Optional pre-built validator (e.g. from the
            ``manifest_validator`` pytest fixture). If omitted, a fresh
            validator is built.

    Returns:
        A list of human-readable validation error strings. An empty list
        means the manifest is valid.
    """
    v = validator or build_validator()
    errors = sorted(v.iter_errors(manifest), key=lambda e: list(e.absolute_path))
    return [_format_error(err) for err in errors]


def _format_error(err: jsonschema.ValidationError) -> str:
    path = ".".join(str(p) for p in err.absolute_path)
    location = f"$.{path}" if path else "$"
    return f"{location}: {err.message}"


__all__ = [
    "AEOS_SCHEMA_VERSION",
    "ManifestError",
    "build_validator",
    "load_manifest",
    "load_schema",
    "validate_manifest",
]
