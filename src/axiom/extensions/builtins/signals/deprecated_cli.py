# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The old CLI noun, kept one release: it runs the same command under the new noun (ADR-265 D9).

"Signal" is retired; the noun is ``observation``. Every old command still works and prints
one line on stderr naming its new spelling and the release that removes the old one, so
scripts and habits can move first. ``AXI_NO_DEPRECATION_WARNING=1`` hides the line.
"""

from __future__ import annotations

import os
import sys

from . import cli
from .cli import get_parser  # noqa: F401 - shell completion builds the old noun's tree from it

QUIET_ENV = "AXI_NO_DEPRECATION_WARNING"
REMOVED_IN = "v0.72.0"


def _brand_cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001 - the warning must never stop the command it wraps
        return "axi"


def main() -> None:
    if not os.environ.get(QUIET_ENV):
        rest = " ".join(sys.argv[1:])
        name = _brand_cli()
        old, new = f"{name} signal {rest}".rstrip(), f"{name} observation {rest}".rstrip()
        print(f"{old}: deprecated, use `{new}` (removed in {REMOVED_IN})", file=sys.stderr)
    cli.main()
