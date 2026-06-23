# Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Test conftest — point axiom imports at the twin-build axiom checkout.

Same pattern as packages/axiom-ext-openmc/conftest.py: the shared workspace
venv has multiple historical axiom-* editable installs whose .pth entries
win sys.path resolution against the twin-build axiom checkout. Prepend
the twin-build axiom/src so the protocol resolves to the lifted location.
"""

from __future__ import annotations

import sys
from pathlib import Path

_TWIN_BUILD_AXIOM_SRC = Path(__file__).resolve().parents[2] / "src"
if str(_TWIN_BUILD_AXIOM_SRC) not in sys.path:
    sys.path.insert(0, str(_TWIN_BUILD_AXIOM_SRC))

# Force-reload any axiom modules that may have been imported from the wrong path.
for _mod in list(sys.modules):
    if _mod == "axiom" or _mod.startswith("axiom."):
        del sys.modules[_mod]
