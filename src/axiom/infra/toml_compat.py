# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TOML loading — single import point for tomllib.

Usage:
    from axiom.infra.toml_compat import load_toml, tomllib

    data = load_toml(path)                    # safe load from Path
    with open(path, "rb") as f:               # manual usage
        data = tomllib.load(f)
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


def load_toml(path: Path | str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load a TOML file, returning *default* (empty dict) on any failure."""
    if default is None:
        default = {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (FileNotFoundError, tomllib.TOMLDecodeError, OSError):
        return default


__all__ = ["tomllib", "load_toml"]
