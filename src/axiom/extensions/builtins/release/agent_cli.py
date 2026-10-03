# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Back-compat shim — ``agent_cli`` was the pre-2026-05-30 module name
for what is now ``_legacy_rivet_cli``. Re-exports EVERY attribute
(public + private) so tests reaching for `_auto_sweep_post_merge_stale`
and other private symbols continue to work during the migration."""

from __future__ import annotations

import sys as _sys

from . import _legacy_rivet_cli as _impl

# Without this, `from ._legacy_rivet_cli import *` only imports public
# names per `__all__` / underscore semantics — tests reach for several
# private symbols.
_self = _sys.modules[__name__]
for _name in dir(_impl):
    if _name.startswith("__"):
        continue
    if hasattr(_self, _name):
        continue
    setattr(_self, _name, getattr(_impl, _name))

build_parser = _impl.build_parser
main = _impl.main


if __name__ == "__main__":
    # ``python -m axiom.extensions.builtins.release.agent_cli`` enters here.
    # Without this block the shim is silent — CLI subprocess smokes get
    # empty stdout and treat the call as a no-op (caught the
    # ``test_cli_sync_subprocess_smoke`` regression).
    raise SystemExit(main())
