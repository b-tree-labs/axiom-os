# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``dev`` — run a checkout's node locally with one command.

``axi dev up`` takes this checkout's lane for a port, makes the person running
it the first account (password held in the vault, never printed), offers the
sign-in their vault already holds, starts the node from this checkout's source,
waits until the app answers, and prints one URL.
"""

from __future__ import annotations

__all__ = ["node", "signin"]
