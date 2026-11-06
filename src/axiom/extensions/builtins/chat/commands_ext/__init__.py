# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One module per slash command (or small family), discovered and registered on import (ADR-168).

A module here registers its commands on the shared registry with the ``@command`` decorator (or
``REGISTRY.register``). Nothing else needs editing: ``load_all()`` imports every module in this package,
and help, completion and dispatch derive from what registered. Keep the handler thin: it parses arguments
and calls a function that is testable without a chat session.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil

log = logging.getLogger(__name__)
_loaded = False


def load_all() -> None:
    """Import every command module once. A module that fails to import is reported, not fatal: one broken
    command must not take the rest of the chat's commands with it."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{info.name}")
        except Exception:  # noqa: BLE001
            log.exception("slash command module %s failed to load", info.name)
