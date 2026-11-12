# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Compatibility import path for the orchestrator service.

The implementation lives in ``services/orchestrator.py`` (AEOS §5.1, kind ``service``).
Importing this module returns that implementation, so patches against the
historical path still land on the class that runs.
"""

from __future__ import annotations

import importlib
import sys

_IMPL = "axiom.extensions.builtins.data_platform.services.orchestrator"

sys.modules[__name__] = importlib.import_module(_IMPL)
