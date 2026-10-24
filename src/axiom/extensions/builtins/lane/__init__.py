# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``lane`` — isolated development lanes on a shared machine.

Several checkouts of one project, running side by side, each with its own
database, its own ports and a record of what it depends on. They share a
Postgres server and nothing else.

The extension knows how to GIVE you an isolated lane. It deliberately does
not know what runs in it — that boundary is what lets the same tool serve
this platform and anything else with the same problem.
"""

from __future__ import annotations

__all__ = ["naming", "ports", "registry", "doctor", "explain"]
