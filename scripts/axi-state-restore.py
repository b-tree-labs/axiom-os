#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Deprecated name, kept for one release: runs ``axi-restore.py state``.

Standard library only; it imports nothing from Axiom.  Use ``axi-restore.py state ...`` from now on
(for example ``python3 -I axi-restore.py state restore latest``).  This shim needs ``axi-restore.py`` beside
it, as it is in the source tree.  Older snapshot directories hold a complete copy of the old
script, which keeps working on its own.
"""

import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TARGET = os.path.join(_HERE, "axi-restore.py")

if __name__ == "__main__":
    if not os.path.isfile(_TARGET):
        sys.stderr.write("error: axi-restore.py must sit beside this shim (it replaced this script)\n")
        raise SystemExit(1)
    print("note: axi-state-restore.py is now `axi-restore.py state`; this name goes away next release",
          file=sys.stderr)
    sys.argv = [_TARGET, "state", *sys.argv[1:]]
    runpy.run_path(_TARGET, run_name="__main__")  # ends in sys.exit(main())
