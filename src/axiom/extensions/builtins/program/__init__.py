# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Program tracking — the ``program`` extension (prd-program, ADR-161).

Phase 1: the one-data-file-per-program contract (``axiom.program/0.1``),
the parameterized ``status`` read, the static-page ``render``, and the
``validate`` check, as skills any surface invokes through the registry
(ADR-056) with ``axi program`` as the thin CLI face.

Phase 2: ``status`` and ``validate`` project to the composed MCP as
read-only tools (``axiom_program__status``, ``axiom_program__validate``),
``status`` is served at ``GET /program/status`` behind the node's authz
(``mount.py``), and ``status`` gains the data-file-only ``drift`` scope.

The coordinator agent, capture watchers (ADR-162), and the write verbs
arrive in later phases; nothing here depends on them.
"""

from __future__ import annotations

from .model import (
    PROGRAM_SCHEMA,
    STATUS_VALUES,
    ProgramData,
    ProgramError,
    ProgramValidationError,
    load_program,
    save_program,
    validate_program,
)

__all__ = [
    "PROGRAM_SCHEMA",
    "STATUS_VALUES",
    "ProgramData",
    "ProgramError",
    "ProgramValidationError",
    "load_program",
    "save_program",
    "validate_program",
]
