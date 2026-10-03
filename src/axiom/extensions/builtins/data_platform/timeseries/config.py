# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Which time-series tables this node manages, read from data-platform config.

**Declared, never discovered.** A partition lifecycle that guessed which tables
to manage would eventually create partitions on, or drop partitions from, a
table nobody asked it to touch. A node that declares nothing gets nothing done
to it, which is the safe default for a job that can drop data.

Config (``<config-dir>/data_platform.toml``, the ADR-065 knob declared in this
extension's ``config.schema.json``)::

    [[data_platform.timeseries]]
    schema = "public"
    table = "measurements"
    granularity = "day"      # day | week | month
    ahead = 7                # keep this many future partitions created
    retain_periods = 90      # omit to never drop

Resolution mirrors ``skills/activity.py`` exactly: the config registry first so
watcher- or API-written values are honored, then the installed file. Never
raises — an unreadable config yields no managed tables rather than an exception
inside a heartbeat.
"""

from __future__ import annotations

import logging
from typing import Any

from .partitions import PartitionSpec

log = logging.getLogger(__name__)

CONFIG_KEY = "data_platform.timeseries"
CONFIG_FILENAME = "data_platform.toml"


def _raw_entries() -> Any:
    try:
        from axiom.infra.config import get_value

        entries = get_value(CONFIG_KEY)
        if entries:
            return entries
    except Exception:  # noqa: BLE001 - config must never break a caller
        pass
    try:
        from axiom.infra.config import default_config_dir, load_config_file

        return load_config_file(default_config_dir() / CONFIG_FILENAME).get(CONFIG_KEY)
    except Exception:  # noqa: BLE001
        return None


def specs_from_entries(entries: Any) -> list[PartitionSpec]:
    """Parse declared tables. A malformed entry is skipped and logged, never
    fatal: one bad stanza must not stop the other tables being managed."""
    if isinstance(entries, dict):
        entries = [entries]
    out: list[PartitionSpec] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            log.warning("%s entry is not a table: %r", CONFIG_KEY, entry)
            continue
        try:
            retain = entry.get("retain_periods")
            out.append(
                PartitionSpec(
                    schema=str(entry["schema"]),
                    table=str(entry["table"]),
                    granularity=str(entry.get("granularity", "day")),
                    ahead=int(entry.get("ahead", 7)),
                    retain_periods=int(retain) if retain is not None else None,
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            log.warning("skipping malformed %s entry %r: %s", CONFIG_KEY, entry, exc)
    return out


def configured_specs() -> list[PartitionSpec]:
    """The time-series tables this node manages, or none."""
    return specs_from_entries(_raw_entries())


__all__ = ["CONFIG_FILENAME", "CONFIG_KEY", "configured_specs", "specs_from_entries"]
