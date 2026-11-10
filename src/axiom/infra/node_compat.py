# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The node tells the laptop what it needs (ADR-207 rule 7, generalised from the intake).

The intake already says so to a sender (:mod:`axiom.extensions.builtins.data_platform.compat`:
an out-of-date collector is answered 426 and keeps its readings). Every other client call had
no such sentence: a laptop older than its node met the difference as an HTTP 403 or a 400 for a
verb it had spelt the old way, with nothing to say that an upgrade was the fix.

**The node advertises.** ``GET /api/v1/version`` (public, no secret) carries ``min_client``, the
oldest platform release that speaks this node's doors (``AXIOM_API_MIN_CLIENT``, else
:data:`MIN_CLIENT_DEFAULT`), and ``capabilities``, what this node actually serves, read off the
routes it mounted (:func:`advertised_capabilities`).

**The client checks once and says it once.** :func:`check_once` runs the first time a command
reaches the bound node. It reuses the answer for :data:`TTL_S` (cached per binding under the
user's state directory), so at most one extra request a day reaches the node, and it prints the
warning at most once in that window::

    node.example.org needs axiom 0.70.9 or later; this computer has 0.70.4. Run: neut update

It never blocks and never raises: a node that does not answer, a cache that cannot be read or
written, and a node that advertises nothing all leave the command to run exactly as before.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

#: The oldest platform release that speaks this node's doors when the node does not say
#: otherwise. 0.70.9 is the first whose kit submission uses ``/data/submit``; an older client
#: sends it to ``/data/query`` and is answered "not a data read".
MIN_CLIENT_DEFAULT = "0.70.9"
#: A node operator's override (a later floor after a breaking change, or ``0`` to say none).
MIN_CLIENT_ENV = "AXIOM_API_MIN_CLIENT"

#: Each capability a node may serve, and the route that proves it is mounted. A capability is
#: advertised only when its route is on the composed app, so the list cannot drift from what runs.
CAPABILITY_ROUTES: dict[str, str] = {
    "data_reads": "/data/query",
    "kit_promotion": "/data/submit",
    "kit_review": "/data/review",
    "retrieval": "/api/v1/rag/search",
    "library": "/api/v1/library/items",
    "chat": "/v1/chat/completions",
    "self_service_keys": "/gate/keys",
    "memory_recall": "/v1/memory/recall",
}
#: Capabilities that answer only where this install holds the medallion (the data door 404s
#: anywhere else, so advertising it there would be a promise the node does not keep).
_MEDALLION_ONLY = frozenset({"data_reads", "kit_promotion", "kit_review"})

#: How long one answer from a node is reused, and how long one warning stays said.
TTL_S = 24 * 3600.0
TTL_ENV = "AXIOM_NODE_COMPAT_TTL"
#: Seconds the check waits for the node. Short: it rides in front of the user's own request.
PROBE_TIMEOUT_S = 2.0
#: Set to ``1`` to switch the check off (an air-gapped demo, a test harness).
DISABLE_ENV = "AXIOM_NO_NODE_COMPAT"


# ------------------------------------------------------------------------------------- server


def min_client() -> str:
    """The minimum client this node advertises. ``0`` or empty in the variable means none."""
    raw = os.environ.get(MIN_CLIENT_ENV)
    if raw is None:
        return MIN_CLIENT_DEFAULT
    raw = raw.strip()
    return "" if raw in ("", "0") else raw


def advertised_capabilities(route_paths: Iterable[str], *, holds_medallion: bool | None = None) -> list[str]:
    """The capabilities whose route is among ``route_paths``, sorted. Never raises."""
    paths = {str(p) for p in route_paths}
    if holds_medallion is None:
        try:
            from axiom.extensions.builtins.data_platform.read_target import holds_medallion as held

            holds_medallion = bool(held())
        except Exception:  # noqa: BLE001 - unknown is "does not hold"
            holds_medallion = False
    out = []
    for name, route in CAPABILITY_ROUTES.items():
        if route in paths and (holds_medallion or name not in _MEDALLION_ONLY):
            out.append(name)
    return sorted(out)


# ------------------------------------------------------------------------------------- client


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in str(text).strip().lstrip("v").split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def older(mine: str, needed: str) -> bool:
    """Whether release ``mine`` is older than ``needed``. Unparseable either way: not older."""
    a, b = _version_tuple(mine), _version_tuple(needed)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) < b + (0,) * (n - len(b))


def node_label(face_url: str) -> str:
    """How a message names the bound node: its host, from the binding. Never a constant."""
    host = urlparse(face_url).hostname or face_url
    return host or "the bound node"


def warning(face_url: str, needed: str, mine: str, *, cli: str | None = None) -> str:
    """The sentence an out-of-date client is told, with the one command that fixes it."""
    cli = cli or _cli_name()
    return (f"{node_label(face_url)} needs axiom {needed} or later; this computer has {mine}. "
            f"Run: {cli} update")


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip() or "axi"
    except Exception:  # noqa: BLE001 - a warning must never be the reason a command fails
        return "axi"


def _ttl() -> float:
    try:
        value = float((os.environ.get(TTL_ENV) or "").strip() or TTL_S)
    except ValueError:
        return TTL_S
    return value if value > 0 else TTL_S


def cache_path() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return Path(get_user_state_dir()) / "node-compat.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: Path, data: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # an unwritable cache means asking again next time, never a failed command


def _probe(face_url: str, timeout: float) -> dict[str, Any] | None:
    """The node's advertisement, or ``None`` when it did not give one. Never raises."""
    from axiom.infra import serving_mode

    try:
        status, body = serving_mode._http_get(f"{face_url.rstrip('/')}/api/v1/version",
                                              {"accept": "application/json"}, timeout)
    except Exception:  # noqa: BLE001 - unreachable is silence, not an error
        return None
    if status >= 400 or not isinstance(body, dict):
        return None
    caps = body.get("capabilities")
    return {
        "min_client": str(body.get("min_client") or ""),
        "version": str(body.get("platform") or body.get("version") or ""),
        "capabilities": [str(c) for c in caps] if isinstance(caps, list) else None,
    }


def check_once(
    face_url: str,
    *,
    mine: str | None = None,
    emit: Callable[[str], None] | None = None,
    now: float | None = None,
    timeout: float = PROBE_TIMEOUT_S,
) -> str | None:
    """Warn once when this client is older than the node at ``face_url`` asks for.

    Returns the warning when it was emitted this call, else ``None``. Reuses the node's answer
    for the TTL and emits a given warning at most once in it. Never raises, never blocks longer
    than ``timeout`` and only when the cached answer has expired.
    """
    try:
        return _check_once(face_url, mine=mine, emit=emit, now=now, timeout=timeout)
    except Exception:  # noqa: BLE001 - the check may never be why a command fails
        return None


def _check_once(face_url, *, mine, emit, now, timeout):
    if not face_url or (os.environ.get(DISABLE_ENV) or "").strip().lower() in ("1", "true", "yes"):
        return None
    if mine is None:
        from axiom.infra.serving_mode import client_version

        mine = client_version()
    now = time.time() if now is None else now
    ttl = _ttl()
    path = cache_path()
    cache = _load(path)
    entry = cache.get(face_url) if isinstance(cache.get(face_url), dict) else {}
    if not entry or now - float(entry.get("checked_at") or 0) >= ttl:
        advert = _probe(face_url, timeout)
        if advert is None:
            # Unreachable or silent. Remember that we asked, so an offline laptop does not pay
            # the timeout on every command, and keep whatever the node said last time.
            entry = {**entry, "checked_at": now}
        else:
            entry = {**entry, **advert, "checked_at": now}
        cache[face_url] = entry
        _save(path, cache)
    needed = str(entry.get("min_client") or "")
    if not needed or not mine or not older(mine, needed):
        return None
    said = f"{mine}<{needed}"
    if entry.get("warned") == said and now - float(entry.get("warned_at") or 0) < ttl:
        return None
    text = warning(face_url, needed, mine)
    (emit or _stderr)(text)
    entry.update(warned=said, warned_at=now)
    cache[face_url] = entry
    _save(path, cache)
    return text


def _stderr(text: str) -> None:
    print(text, file=sys.stderr)


def known_older(face_url: str, *, mine: str | None = None) -> str:
    """The warning, from the cache only, when this client is known to be too old; else empty.

    For a refusal that already happened (an HTTP 403 or 400 from the node): it adds the likely
    cause to the error without another request.
    """
    try:
        entry = _load(cache_path()).get(face_url) or {}
        needed = str(entry.get("min_client") or "")
        if mine is None:
            from axiom.infra.serving_mode import client_version

            mine = client_version()
        if needed and mine and older(mine, needed):
            return warning(face_url, needed, mine)
    except Exception:  # noqa: BLE001
        pass
    return ""


def capabilities(face_url: str) -> list[str] | None:
    """What the node at ``face_url`` said it serves, from the cache; ``None`` if it never said."""
    entry = _load(cache_path()).get(face_url) or {}
    caps = entry.get("capabilities") if isinstance(entry, dict) else None
    return list(caps) if isinstance(caps, list) else None


__all__ = [
    "CAPABILITY_ROUTES",
    "DISABLE_ENV",
    "MIN_CLIENT_DEFAULT",
    "MIN_CLIENT_ENV",
    "TTL_ENV",
    "TTL_S",
    "advertised_capabilities",
    "cache_path",
    "capabilities",
    "check_once",
    "known_older",
    "min_client",
    "node_label",
    "older",
    "warning",
]
