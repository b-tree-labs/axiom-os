# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Enable ``python -m axiom <args>`` as an equivalent entry to the ``axi``
console script. Used by CLI subprocess smoke tests (ADR-063 §lint, and
per the project ``feedback_cli_subprocess_smoke_required`` rule)."""

from __future__ import annotations

from axiom.axiom_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
