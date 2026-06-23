# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Extension-registered settings sections.

Implements the settings-side mirror of `[[connections]]`: an extension
declares one or more `[[settings.sections]]` blocks in its manifest;
the entry callable returns a SectionView at runtime; `axi settings`
discovers + lists active sections + dispatches view/edit/wizard ops.

Contract: spec-settings.md §2 + §4.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from axiom.extensions.discovery import discover_extensions

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes (spec §4.1 + §2.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SettingsSectionDef:
    """Manifest declaration of an extension settings section."""

    name: str
    display_name: str
    description: str
    entry: str
    wizard: str | None = None
    schema: str | None = None
    intent_groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class SectionView:
    """Runtime view of one settings section's current state."""

    name: str
    display_name: str
    description: str
    values: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    is_active: bool = True
    wizard: Callable[[], SectionView] | None = None


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def parse_settings_sections(manifest: dict) -> list[SettingsSectionDef]:
    """Extract [[settings.sections]] blocks from a parsed AEOS manifest."""
    raw_sections = (manifest.get("settings") or {}).get("sections") or []
    out: list[SettingsSectionDef] = []
    for raw in raw_sections:
        try:
            out.append(
                SettingsSectionDef(
                    name=raw["name"],
                    display_name=raw.get("display_name", raw["name"]),
                    description=raw.get("description", ""),
                    entry=raw["entry"],
                    wizard=raw.get("wizard"),
                    schema=raw.get("schema"),
                    intent_groups=tuple(raw.get("intent_groups") or ()),
                )
            )
        except KeyError as exc:
            log.warning("Skipping settings section missing required field: %s", exc)
    return out


# ---------------------------------------------------------------------------
# Discovery (mirrors discover_connections in extensions/discovery.py)
# ---------------------------------------------------------------------------


def discover_settings_sections() -> list[SettingsSectionDef]:
    """Enumerate registered settings sections from all enabled extensions.

    Precedence: first definition wins on name conflict — matches the
    project-local > user-global > builtin order discover_extensions
    yields. Sections from disabled extensions are filtered.
    """
    seen: set[str] = set()
    out: list[SettingsSectionDef] = []
    for ext in discover_extensions():
        if not getattr(ext, "enabled", True):
            continue
        for section in getattr(ext, "settings_sections", []) or []:
            if section.name in seen:
                continue
            seen.add(section.name)
            out.append(section)
    return out


# ---------------------------------------------------------------------------
# Entry resolution + view loading
# ---------------------------------------------------------------------------


def _resolve_entry(entry_spec: str) -> Callable[..., Any] | None:
    """Resolve a 'module:function' entry string to a callable."""
    if ":" not in entry_spec:
        log.warning("Invalid entry spec (missing ':'): %s", entry_spec)
        return None
    module_path, func_name = entry_spec.split(":", 1)
    try:
        module = importlib.import_module(module_path)
        return getattr(module, func_name)
    except Exception as exc:
        log.warning("Could not resolve entry %s: %s", entry_spec, exc)
        return None


def load_section_view(section_def: SettingsSectionDef) -> SectionView | None:
    """Invoke section_def.entry and return its SectionView; None on failure."""
    entry = _resolve_entry(section_def.entry)
    if entry is None:
        return None
    try:
        return entry()
    except Exception as exc:
        log.warning("Section %s entry raised: %s", section_def.name, exc)
        return None
