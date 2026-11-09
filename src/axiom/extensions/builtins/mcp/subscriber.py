# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Compatibility import path for the extension-change hook.

The implementation lives in ``hooks/subscriber.py`` (AEOS §5.1, kind ``hook``).
Importing this module returns that implementation.
"""

from __future__ import annotations

import importlib
import sys

_IMPL = "axiom.extensions.builtins.mcp.hooks.subscriber"

sys.modules[__name__] = importlib.import_module(_IMPL)
