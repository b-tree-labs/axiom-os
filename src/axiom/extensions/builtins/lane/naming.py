# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where a lane's name, database and ports come from.

A lane is one checkout's isolated slice of a shared development machine: its
own database on the shared Postgres, its own ports, its own editable installs.
Several checkouts of the same project run side by side and none of them can
see the others' migrations.

Everything here is **derived from the checkout directory**, deterministically
and with no I/O beyond asking git where it is. That is the whole point. A
registry that records what someone remembered to claim will always be missing
the session that did not ask, and the session that did not ask is exactly the
one that corrupts a database. Derivation happens whether or not anyone
remembers; the registry then records what derivation produced, so the two
cannot disagree.

The rules are deliberately dull and must stay so: two tools computing a lane's
database name have to arrive at the same string, forever, or the isolation is
worse than none.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse, urlunparse

#: Postgres truncates identifiers at 63 bytes. A name that silently loses its
#: tail collides with its neighbours, which is the one failure this must not
#: have.
MAX_IDENTIFIER = 63

#: Ports below this are somebody's standing service. A lane never takes one.
FIRST_LANE_PORT = 8800
LAST_LANE_PORT = 8999


def repo_root(start: Path | str) -> Path | None:
    """The checkout `start` is inside, or None when it is not in one."""
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return None
    return Path(out).resolve() if out else None


def is_linked_worktree(root: Path) -> bool:
    """True for a linked worktree, where ``.git`` is a FILE, not a directory.

    This is the whole detection. A primary clone keeps the shared database
    because that is what a person expects from the checkout they think of as
    "the repo"; every extra checkout is presumed to be parallel work.
    """
    return (root / ".git").is_file()


def slug(folder_name: str) -> str:
    """A Postgres-safe, filesystem-safe token for a checkout directory.

    Lowercased, runs of non-alphanumerics collapsed to one underscore, ends
    trimmed. Deterministic and boring on purpose.
    """
    s = re.sub(r"[^a-z0-9]+", "_", folder_name.lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "worktree"


def database_name(folder_name: str, *, prefix: str) -> str:
    """``<prefix>_<slug>``, truncated so Postgres cannot do it for us.

    Truncation trims a trailing underscore so the name never ends in one, and
    is applied to the WHOLE name rather than the slug, because the prefix is
    the part a person recognises at a `\\l` prompt.
    """
    name = f"{prefix}_{slug(folder_name)}"
    if len(name) > MAX_IDENTIFIER:
        name = name[:MAX_IDENTIFIER].rstrip("_")
    return name


def preferred_port(
    name: str, *, first: int = FIRST_LANE_PORT, last: int = LAST_LANE_PORT, stride: int = 2
) -> int:
    """The port this lane would like, derived from its name.

    Stable across machines and runs, so a checkout tends to come back on the
    ports its owner already has open in a browser tab. It is a PREFERENCE, not
    a claim: two names can hash to the same slot and the allocator probes on
    from here. Determinism is a convenience; the registry is what prevents
    collisions.
    """
    span = (last - first + 1) // stride
    digest = hashlib.sha256(name.encode()).digest()
    return first + (int.from_bytes(digest[:4], "big") % span) * stride


def _database_of(parsed) -> str:
    path = (parsed.path or "").lstrip("/")
    return unquote(path.split("/")[0]) if path else ""


def is_rewritable(
    url: str,
    *,
    shared_database: str,
    local_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "::1"),
) -> bool:
    """Whether substituting a database name into `url` is safe.

    THE load-bearing check, and the reason isolation can default to on.

    Only a URL that points at LOCAL Postgres and names the SHARED development
    database is rewritten. Anything else — a remote host, a non-default port,
    a database somebody chose deliberately — is left exactly as it is. Without
    this, a developer pointed at staging would find their worktree quietly
    redirected to a database that does not exist, or worse, created.

    A URL is not rewritable merely because it is unfamiliar. The test is
    positive: it must look like the thing we hand out by default.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme not in ("postgresql", "postgres"):
        return False
    if (parsed.hostname or "").lower() not in local_hosts:
        return False
    if parsed.port not in (None, 5432):
        return False
    return _database_of(parsed) == shared_database


def rewrite_database(url: str, database: str) -> str:
    """Return `url` with its database replaced. Caller checks is_rewritable."""
    parsed = urlparse(url)
    rest = (parsed.path or "/").lstrip("/").split("/")[1:]
    path = "/" + "/".join([database, *rest]) if rest else f"/{database}"
    return urlunparse(parsed._replace(path=path))


def maintenance_url(url: str) -> str:
    """The same server, pointed at ``postgres``.

    A health check must not probe the lane's own database: a lane that has not
    been created yet is the normal state on a first run, and a probe against it
    reports "Postgres is down" when Postgres is fine. Ask the maintenance
    database whether the SERVER is up, and ask separately whether the lane's
    database exists.
    """
    return rewrite_database(url, "postgres")


__all__ = [
    "FIRST_LANE_PORT",
    "LAST_LANE_PORT",
    "MAX_IDENTIFIER",
    "database_name",
    "is_linked_worktree",
    "is_rewritable",
    "maintenance_url",
    "preferred_port",
    "repo_root",
    "rewrite_database",
    "slug",
]
