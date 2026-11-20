# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi data`` command capability (AEOS kind ``cmd``).

The implementation is ``commands.cli``. The name is resolved lazily so
importing the package does not import the CLI while the CLI module is
itself still loading.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["main"]


def __getattr__(name: str) -> Any:
    if name == "main":
        module = importlib.import_module("axiom.extensions.builtins.data_platform.commands.cli")
        return module.main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
