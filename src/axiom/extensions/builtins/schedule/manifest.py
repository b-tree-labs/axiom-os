# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Manifest-declared cadence ingestion.

Per prd-axiom-schedule §5.2: extensions declare ``[[extension.schedule]]``
blocks in their ``axiom-extension.toml``; PULSE discovers them at
extension install via the AEOS manifest discovery, registers them with
PULSE's runtime, and removes them at extension uninstall.

The discovery hook is wired into AEOS extension install in a follow-up
PR — the function below is the pure transform from a parsed manifest
block to a ``register()`` call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from axiom.extensions.builtins.schedule.api import Cadence


@dataclass(frozen=True)
class ManifestSchedule:
    """A parsed ``[[extension.schedule]]`` block."""

    name: str
    description: str
    action: str
    cadence: Cadence
    classification_ceiling: Optional[str]
    raci_default: str
    retry_policy: dict[str, Any]


def parse_manifest_block(block: dict[str, Any]) -> ManifestSchedule:
    """Parse one ``[[extension.schedule]]`` TOML block.

    Validates the cadence shape per spec-axiom-schedule §6 (seconds-precision
    cron rejected) and returns a typed ManifestSchedule.
    """
    name = _required(block, "name")
    action = _required(block, "action")
    description = block.get("description", "")
    cadence_block = _required(block, "cadence")

    cadence = _build_cadence(cadence_block)
    retry = block.get("retry", {})
    if not isinstance(retry, dict):
        raise ValueError(f"schedule {name!r}: retry must be a table")

    return ManifestSchedule(
        name=name,
        description=description,
        action=action,
        cadence=cadence,
        classification_ceiling=block.get("classification_ceiling"),
        raci_default=block.get("raci_default", "autonomous"),
        retry_policy=retry,
    )


def _build_cadence(block: dict[str, Any]) -> Cadence:
    kind = _required(block, "kind")
    if kind not in {"one_shot", "interval", "cron", "trigger"}:
        raise ValueError(f"unknown cadence kind: {kind!r}")

    interval = None
    cron = None
    if kind == "interval":
        seconds = _required(block, "interval_seconds")
        if not isinstance(seconds, int) or seconds <= 0:
            raise ValueError("interval_seconds must be a positive integer")
        interval = timedelta(seconds=seconds)
    elif kind == "cron":
        cron = _required(block, "cron")
        _reject_seconds_precision_cron(cron)

    jitter = block.get("jitter_seconds", 0)
    if not isinstance(jitter, int) or jitter < 0:
        raise ValueError("jitter_seconds must be a non-negative integer")
    randomized_delay = timedelta(seconds=jitter) if jitter > 0 else None

    return Cadence(
        kind=kind,
        interval=interval,
        cron=cron,
        tz=block.get("tz", "UTC"),
        randomized_delay=randomized_delay,
    )


def _reject_seconds_precision_cron(expr: str) -> None:
    """Per spec-axiom-schedule §6.2: only 5-field POSIX form accepted."""
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(
            f"cron expression {expr!r} must be 5-field POSIX form; "
            "seconds-precision cron is rejected (spec-axiom-schedule §6.2)."
        )


def _required(block: dict[str, Any], key: str) -> Any:
    if key not in block:
        raise ValueError(f"manifest schedule block missing required key {key!r}")
    return block[key]


__all__ = ["ManifestSchedule", "parse_manifest_block"]
