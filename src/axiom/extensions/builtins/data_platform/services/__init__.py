# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Data-platform services (AEOS kind ``service``).

Submodules stay lazy so loading the data-read mount does not import the
orchestrator, and the other way around.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["OrchestratorService", "mount_spec"]


def __getattr__(name: str) -> Any:
    if name == "OrchestratorService":
        module = importlib.import_module(
            "axiom.extensions.builtins.data_platform.services.orchestrator"
        )
        return module.OrchestratorService
    if name == "mount_spec":
        module = importlib.import_module(
            "axiom.extensions.builtins.data_platform.services.read_mount"
        )
        return module.mount_spec
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
