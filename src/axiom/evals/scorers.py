# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Core scorers. Each returns a float in [0.0, 1.0]."""

from __future__ import annotations

import json
from typing import Any


def exact_match(output: Any, expected: Any, **_: Any) -> float:
    return 1.0 if str(output).strip() == str(expected).strip() else 0.0


def contains(output: Any, expected: Any, **_: Any) -> float:
    return 1.0 if str(expected).lower() in str(output).lower() else 0.0


def json_schema_valid(output: Any, expected: dict[str, Any], **_: Any) -> float:
    """Expected: {'required_keys': [...]}. Returns 1.0 iff output parses as JSON
    and contains every required key."""
    try:
        parsed = json.loads(output)
    except (ValueError, TypeError):
        return 0.0
    if not isinstance(parsed, dict):
        return 0.0
    required = expected.get("required_keys", [])
    return 1.0 if all(k in parsed for k in required) else 0.0
