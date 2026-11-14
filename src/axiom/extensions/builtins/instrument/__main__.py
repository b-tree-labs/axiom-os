# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``python -m axiom.extensions.builtins.instrument`` is ``<cli> instrument``."""

from __future__ import annotations

from .commands.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
