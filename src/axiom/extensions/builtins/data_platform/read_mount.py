# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Compatibility import path for the data-read service mount.

The implementation lives in ``services/read_mount.py`` (AEOS §5.1, kind ``service``).
Importing this module returns that implementation.
"""

from __future__ import annotations

import importlib
import sys

_IMPL = "axiom.extensions.builtins.data_platform.services.read_mount"

sys.modules[__name__] = importlib.import_module(_IMPL)
