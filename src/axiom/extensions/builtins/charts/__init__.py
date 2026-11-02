# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Charts from answers: which charts a result can be drawn as, and the drawing (ADR-216).

One capability per verb, projected to every surface: ``charts.suggest`` and
``charts.render`` reach the CLI (``axi charts``), MCP, the chat tool loop and
``POST /api/v1/charts/*``. The rules live in
:mod:`axiom.extensions.builtins.scidisplay.suggest`; this package carries them
to the surfaces and bounds what an answer carries.
"""

from . import block

__all__ = ["block"]
