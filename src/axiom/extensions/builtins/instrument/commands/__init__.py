# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``<cli> instrument`` command capability (AEOS kind ``cmd``).

The implementation is ``commands.cli``. Names are resolved lazily, so importing the
package imports no kind, and ``SKILL_FOR_VERB`` is computed from the kinds
registered when it is read.
"""

from __future__ import annotations

import importlib
from typing import Any

_FROM_CLI = ("main", "get_parser", "SKILL_FOR_VERB", "parser_sources")

__all__ = list(_FROM_CLI)


def __getattr__(name: str) -> Any:
    if name in _FROM_CLI:
        return getattr(importlib.import_module("axiom.extensions.builtins.instrument.commands.cli"), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
