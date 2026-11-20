# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Platform-aware base directory resolution for TIDY scratch space.

Resolution order:
1. AXIOM_HYGIENE_SCRATCH_DIR env var (explicit override)
2. Platform default:
   - macOS: ~/Library/Caches/<cli>/tidy/
   - Linux: $XDG_RUNTIME_DIR/<cli>/tidy/ or /tmp/<cli>-{uid}/tidy/
   - Windows: %TEMP%/<cli>/tidy/
3. Fallback: tempfile.gettempdir()/<cli>-tidy

``<cli>`` is the active branding's CLI name, so a product built on the platform
gets its own scratch tree.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from axiom.infra.brand_migration import getenv
from axiom.infra.branding import get_branding as _get_branding


def resolve_base_dir() -> Path:
    """Resolve the scratch base directory for TIDY.

    Returns a Path that may or may not exist yet — the caller is responsible
    for creating it and handling permission errors.
    """
    # 1. Explicit override
    env_dir = getenv("AXIOM_HYGIENE_SCRATCH_DIR")
    if env_dir:
        return Path(env_dir)

    # 2. Platform default
    _cli = _get_branding().cli_name
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / _cli / "tidy"

    if sys.platform == "win32":
        temp = os.environ.get("TEMP", tempfile.gettempdir())
        return Path(temp) / _cli / "tidy"

    # Linux / other Unix
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return Path(xdg) / _cli / "tidy"

    return Path(tempfile.gettempdir()) / f"{_cli}-{os.getuid()}" / "tidy"
