# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Platform-aware base directory resolution for TIDY scratch space.

Resolution order:
1. NEUT_SCRATCH_DIR env var (explicit override)
2. Platform default:
   - macOS: ~/Library/Caches/neut/tidy/
   - Linux: $XDG_RUNTIME_DIR/neut/tidy/ or /tmp/neut-{uid}/tidy/
   - Windows: %TEMP%/neut/tidy/
3. Fallback: tempfile.gettempdir()/neut-tidy
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from axiom.infra.branding import get_branding as _get_branding


def resolve_base_dir() -> Path:
    """Resolve the scratch base directory for TIDY.

    Returns a Path that may or may not exist yet — the caller is responsible
    for creating it and handling permission errors.
    """
    # 1. Explicit override
    env_dir = os.environ.get("NEUT_SCRATCH_DIR")
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
