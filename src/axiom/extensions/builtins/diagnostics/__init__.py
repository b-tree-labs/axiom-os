# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Diagnostics + system-health extension.

Houses **TRIAGE**, the agent responsible for platform health, security
scanning, configuration audit, and federation trust verification. See
``agents/triage/persona.md`` for TRIAGE's role definition.

This extension exposes ``axi doctor`` for interactive diagnosis and the
``DoctorAgent`` class for autonomous multi-turn diagnosis/patch loops
driven by the chat pipeline.

ADR-065 PR-1: dogfood adopter of ``register_schema_from_jsonschema``.
The ``config.schema.json`` next to this module declares three operator-
facing knobs (severity threshold, auto-fix permission, parallel-check
cap) for the doctor agent. Registration is best-effort: a failure here
does not block the extension from loading the rest of its surface.
"""

from __future__ import annotations

from pathlib import Path


def _register_config_schema() -> None:
    try:
        from axiom.infra.config import register_schema_from_jsonschema

        schema_path = Path(__file__).parent / "config.schema.json"
        if schema_path.exists():
            register_schema_from_jsonschema("diagnostics", schema_path)
    except Exception:
        # Best-effort — the diagnostics extension stays loadable even
        # if the config primitive is unavailable in this context.
        pass


_register_config_schema()
