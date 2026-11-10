# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Compatibility import path for the ``axi data`` command.

The implementation lives in ``commands/cli.py`` (AEOS §5.1, kind ``cmd``).
Importing this module returns that implementation, so patches against the
historical path still land on the functions that run.
"""

from __future__ import annotations

import importlib
import sys

_IMPL = "axiom.extensions.builtins.data_platform.commands.cli"


def _load():
    return importlib.import_module(_IMPL)


if __name__ == "__main__":
    raise SystemExit(_load().main())

sys.modules[__name__] = _load()
