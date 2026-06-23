# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axiom.governance.mode`` — runtime mode awareness.

Resolves the runtime mode from ``AXIOM_MODE`` env. Three values:

    ``dev``         — local development; ``dev_mode=True`` permitted.
    ``staging``     — production-like; dev_mode is suspicious.
    ``production``  — real workload; dev_mode is REJECTED at setup time.

The default is ``dev`` when the env var is absent. Operators flip
``AXIOM_MODE`` in production deploys; the easy-onramp consults this
before stitching authn defaults so a dev-mode permit-all rule cannot
silently leak into prod.
"""

from __future__ import annotations

import os
from typing import Literal

Mode = Literal["dev", "staging", "production"]

_VALID: frozenset[str] = frozenset({"dev", "staging", "production"})


def current_mode() -> Mode:
    """Return the current runtime mode from ``AXIOM_MODE``.

    Unknown values fall back to ``dev`` with a deliberate noise so an
    operator who fat-fingers the env doesn't accidentally end up in a
    permissive mode without warning.
    """
    raw = (os.environ.get("AXIOM_MODE") or "dev").strip().lower()
    if raw in _VALID:
        return raw  # type: ignore[return-value]
    # Unknown → dev, but surface the issue.
    import logging
    logging.getLogger("axiom.governance.mode").warning(
        "AXIOM_MODE=%r is not one of %s; falling back to 'dev'",
        raw, sorted(_VALID),
    )
    return "dev"


def is_dev() -> bool:
    return current_mode() == "dev"


def is_production() -> bool:
    return current_mode() == "production"


__all__ = ["Mode", "current_mode", "is_dev", "is_production"]
