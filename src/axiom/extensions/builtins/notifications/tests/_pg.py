# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""One honest probe for "can the Postgres-backed path actually run here?".

The Postgres tests used to gate on ``psycopg2.connect(AXIOM_DB_URL)``, which
answers *is some Postgres listening*. What they exercise needs a narrower thing
to be true: that ``engine_for("notifications")`` resolves and the extension can
create its table in its own schema. Any machine with an unrelated Postgres on
5432 satisfied the first and failed the second, so the tests failed instead of
skipping — and the pre-push hook blocked every push on that machine.

Probe the path the code takes, so the guard and the thing it guards agree by
construction.
"""

from __future__ import annotations

import functools


@functools.lru_cache(maxsize=1)
def postgres_available() -> bool:
    """True when the notifications extension can really open its store."""
    try:
        from axiom.extensions.builtins.notifications.preferences import (
            _try_build_postgres_default,
        )

        return _try_build_postgres_default() is not None
    except Exception:
        return False


SKIP_REASON = (
    "notifications Postgres path unavailable "
    "(engine_for('notifications') or its table bootstrap did not succeed)"
)
