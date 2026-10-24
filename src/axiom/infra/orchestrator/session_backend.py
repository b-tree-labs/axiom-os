# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where chat sessions are stored, and whether a second surface can reach it.

The session plane is the one that has to become shared for "one chat, many
surfaces" to mean anything: continuing a conversation on a phone, or in a web
harness, requires both to be clients of a single store. JSON files under
``runtime/sessions/`` cannot be that — every machine has its own copy.

So the backend is chosen by whether a database is configured, not by a flag
somebody remembers to set. Files remain, explicitly marked local-only, for a
laptop with nothing wired.

A distinction worth keeping sharp: a database on ``localhost`` is the right
BACKEND but is still one machine's database. It does not make the session
plane ``served``, because no second surface can reach it. Reporting otherwise
would be the same class of false green this codebase has been clearing out —
a check accurate about what it measured and wrong about what it claimed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

#: Hosts that mean "this machine" — configured, but not reachable by anyone else.
_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "0.0.0.0"})

BackendKind = Literal["database", "files"]
PlaneKind = Literal["local", "served", "unconfigured"]


@dataclass(frozen=True)
class BackendChoice:
    """Which store answers, and how honestly it can be described."""

    kind: BackendKind
    #: True when the store is a file tree — local testing only, per the rule
    #: that SQLite and files are not a deployment backend.
    local_only: bool
    #: True when a second surface could reach this store. A localhost database
    #: is not shared.
    shared: bool
    #: What ``/planes`` should report for the session plane.
    plane_kind: PlaneKind
    #: A redacted description of the store — safe for logs and the plane report.
    source: str
    #: Why this backend was chosen, in one line.
    reason: str


def _redacted(url: str) -> str:
    """``postgresql://user@host:port/db`` — never the password."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "(unparseable url)"
    user = parts.username or ""
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    path = parts.path or ""
    at = f"{user}@" if user else ""
    return f"{parts.scheme}://{at}{host}{port}{path}"


def choose_session_backend(default_url: str | None = None) -> BackendChoice:
    """Pick the session store from configuration.

    ``default_url`` overrides the platform default, and an empty string means
    "no default" — which is how a test asks for the unconfigured case without
    depending on what this machine happens to have.
    """
    url = os.environ.get("AXIOM_DB_URL") or os.environ.get("DATABASE_URL") or ""
    if not url:
        if default_url is None:
            from axiom.infra.db import DEFAULT_DB_URL

            url = DEFAULT_DB_URL
        else:
            url = default_url

    if not url:
        return BackendChoice(
            kind="files",
            local_only=True,
            shared=False,
            plane_kind="local",
            source="runtime/sessions",
            reason=(
                "no database configured; using the file store, which is for "
                "local testing only and cannot be reached by another surface"
            ),
        )

    host = (urlsplit(url).hostname or "").lower()
    shared = host not in _LOCAL_HOSTS
    return BackendChoice(
        kind="database",
        local_only=False,
        shared=shared,
        plane_kind="served" if shared else "local",
        source=_redacted(url),
        reason=(
            "database configured and reachable by other surfaces"
            if shared
            else "database configured on this machine; no second surface can reach it"
        ),
    )


__all__ = ["BackendChoice", "BackendKind", "PlaneKind", "choose_session_backend"]
