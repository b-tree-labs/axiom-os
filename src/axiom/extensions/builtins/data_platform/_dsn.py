# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One answer to "which database?", for every data-platform skill.

There were nine. Each skill resolved a DSN by hand, and they did not agree:
some read ``DP1_RAG_DSN`` then ``DATABASE_URL``, one added ``AXIOM_DB_URL``,
one took a configurable env name — and none of them fell back to the URL the
platform is already connected to via :func:`axiom.infra.db.platform_db_url`.

On a node whose environment carries none of those names, that is not a
configuration problem. It is nine answers to one question:

    $ axi db migrate upgrade head
    ✅ Upgrade complete                      <- reached the database
    $ axi data ensure-schema
    ERROR: no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL / AXIOM_DB_URL
    $ axi data backup
    ERROR: no DSN: pass dsn= or set DP1_RAG_DSN / DATABASE_URL

The third one is the one that matters. `data.backup` is what a scheduled
nightly cadence invokes, so arming the schedule against it would have produced
a PULSE row that fires at 2am, fails, and leaves a policy reading
``enabled = true`` with no backup behind it.

Order: an explicit ``dsn`` param wins, then the env names in use, then the
platform's own URL. The last step is the point — a skill must never fail to
find the database the process it runs in is already talking to.
"""

from __future__ import annotations

import os
from typing import Any

#: Read in order. Kept because deployments set them (the Helm chart exports
#: DP1_RAG_DSN); not extended — new code uses the platform's URL.
DSN_ENV_NAMES = ("DP1_RAG_DSN", "DATABASE_URL", "AXIOM_DB_URL")


def resolve_dsn(
    params: dict[str, Any] | None = None,
    *,
    key: str = "dsn",
    env_name: str | None = None,
) -> str:
    """The database this skill should use. Never empty.

    ``env_name`` names an extra variable to consult first, for the skills whose
    connector config carries one (``rag_dsn_env``).
    """
    params = params or {}
    explicit = params.get(key)
    if explicit:
        return str(explicit)
    names = (env_name, *DSN_ENV_NAMES) if env_name else DSN_ENV_NAMES
    for name in names:
        if name and os.environ.get(name):
            return os.environ[name]

    from axiom.infra.db import platform_db_url

    return platform_db_url()


def dsn_source() -> str:
    """Where :func:`resolve_dsn` would get its answer, as a loggable label.

    Never the DSN itself — it carries a password. A nightly backup that ran
    against the platform default because nothing was configured is a different
    situation from one that ran against a DSN somebody set, and the log should
    say which without printing a credential.
    """
    for name in DSN_ENV_NAMES:
        if os.environ.get(name):
            return name
    return "platform default (axiom.infra.db.platform_db_url)"


__all__ = ["DSN_ENV_NAMES", "dsn_source", "resolve_dsn"]
