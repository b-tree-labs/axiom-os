# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``python -m axiom.extensions.builtins.release.agreement hook`` is the hook's fast path.

It skips the ``axi`` command registry, which a prompt hook cannot afford on every prompt.
"""

import sys

from .cli import main

raise SystemExit(main(sys.argv[1:]))
