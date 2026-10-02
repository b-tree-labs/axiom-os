# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Back-compat shim — moved to `axiom.llm.routing_health` (ADR-071).

`llm` is a first-class core primitive; import from `axiom.llm.routing_health`. This
shim aliases the old `axiom.infra.routing_health` path to the new module so existing
importers keep working during the transition.
"""

import sys as _sys

from axiom.llm import routing_health as _moved

_sys.modules[__name__] = _moved
