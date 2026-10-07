# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Back-compat shim — moved to `axiom.llm.params` (ADR-071).

`llm` is a first-class core primitive; import from `axiom.llm.params`. This
shim aliases the old `axiom.infra.llm_params` path to the new module so existing
importers keep working during the transition.
"""

import sys as _sys

from axiom.llm import params as _moved

_sys.modules[__name__] = _moved
