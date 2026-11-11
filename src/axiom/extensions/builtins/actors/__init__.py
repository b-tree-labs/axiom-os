# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``actors`` — Team: the people, agents and nodes that can act at a site (MVP-23).

A read across where each actor's facts already live: the gate's accounts, keys
and invitations, the directory's groups, the agent roster and the node itself.
It keeps no store of its own (MVP-23-A4); every write Team offers is a gate verb
(``gate.invite``, ``gate.role``, ``gate.suspend``, ``gate.reinstate``).
"""

from __future__ import annotations

from .directory import build_rows, find
from .visibility import FIELDS, viewer_class

__all__ = ["FIELDS", "build_rows", "find", "viewer_class"]
