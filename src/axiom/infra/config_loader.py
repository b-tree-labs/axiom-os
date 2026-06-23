# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Safe config file loading — YAML, JSON, TOML with consistent error handling.

Usage:
    from axiom.infra.config_loader import load_yaml, load_json, load_toml

    cfg = load_yaml(path)           # returns {} on any failure
    cfg = load_json(path)           # returns {} on any failure
    cfg = load_toml(path)           # re-exported from toml_compat
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .toml_compat import load_toml  # noqa: F401 — re-export

logger = logging.getLogger(__name__)


def load_yaml(path: Path | str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load a YAML file safely. Returns *default* (empty dict) on any failure."""
    if default is None:
        default = {}
    try:
        import yaml
    except ImportError:
        logger.debug("PyYAML not installed; returning default for %s", path)
        return default
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else default
    except (FileNotFoundError, yaml.YAMLError, OSError) as exc:
        logger.debug("Failed to load YAML %s: %s", path, exc)
        return default


def load_json(path: Path | str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load a JSON file safely. Returns *default* (empty dict) on any failure."""
    if default is None:
        default = {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to load JSON %s: %s", path, exc)
        return default


__all__ = ["load_yaml", "load_json", "load_toml"]
