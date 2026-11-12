# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Video's command line, reached as ``<cli> instrument video`` (instrument kind ``video``, ADR-256).

The implementation is ``commands.cli``. The name is resolved lazily so importing
the package does not import the CLI while the CLI module is itself still loading.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["main"]


def __getattr__(name: str) -> Any:
    if name == "main":
        return importlib.import_module("axiom.extensions.builtins.video.commands.cli").main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
