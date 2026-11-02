# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Generic manifest validation utilities.

Domain-agnostic tools for validating YAML/dict manifests against JSON Schema,
parsing semantic versions, and enforcing status transition graphs.
"""

from __future__ import annotations

import re


def validate_yaml_schema(data: dict, schema: dict) -> list[str]:
    """Validate a dict against a JSON Schema. Returns list of error strings."""
    try:
        import jsonschema
    except ImportError:
        return ["jsonschema package not installed"]

    validator = jsonschema.Draft7Validator(schema)
    return [e.message for e in sorted(validator.iter_errors(data), key=lambda e: list(e.path))]


_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_semver(version: str) -> tuple[int, int, int]:
    """Parse a strict semver string (major.minor.patch). Raises ValueError."""
    m = _SEMVER_RE.match(version)
    if not m:
        raise ValueError(f"Invalid semver: {version!r} (expected X.Y.Z)")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def compare_semver(a: str, b: str) -> int:
    """Compare two semver strings. Returns <0, 0, or >0."""
    av = parse_semver(a)
    bv = parse_semver(b)
    if av < bv:
        return -1
    if av > bv:
        return 1
    return 0


_DEFAULT_TRANSITIONS: dict[str, list[str]] = {
    "draft": ["review"],
    "review": ["production", "draft"],
    "production": ["deprecated"],
    "deprecated": ["archived"],
    "archived": [],
}


class StatusMachine:
    """Simple directed-graph state machine for status transitions."""

    def __init__(self, transitions: dict[str, list[str]] | None = None):
        self._transitions = transitions or _DEFAULT_TRANSITIONS

    def can_transition(self, from_status: str, to_status: str) -> bool:
        allowed = self._transitions.get(from_status, [])
        return to_status in allowed
