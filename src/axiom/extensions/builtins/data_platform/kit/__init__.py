# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The tenant data kit: contribute to every tier of a shared medallion.

A provider runs ``kit-init`` in their own site repository and gets a working
example of each contribution kind against bundled sample data: a normalizer
(bronze to silver), silver declarations (derived channels, roles), a gold
object (SQL over their own silver), a verb chat can call, and a chart. They
iterate with ``kit-up`` / ``kit-try`` on a local medallion that enforces the
same tenant boundary the host does, and ``kit-check`` is the gate CI runs
before promotion. See ``docs/prds/prd-tenant-data-kit.md``.
"""

from __future__ import annotations

from .check import check_kit
from .medallion import down, up
from .project import KitError, KitProject, load
from .runner import try_kit
from .scaffold import init

__all__ = ["KitError", "KitProject", "check_kit", "down", "init", "load", "try_kit", "up"]
