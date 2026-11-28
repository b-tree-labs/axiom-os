# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One affinity key per session and entry, sent where a hop keeps the prompt cache (#2211, RS-P9).

OpenAI spreads requests that share a prefix across machines unless ``prompt_cache_key`` is
sent; OpenRouter pins a provider only after a cache hit unless ``session_id`` is sent. The
key is a keyed hash of the session id and the entry: it names no person, carries no
content, and changes with the session. The salt is this node's, kept in its state directory,
so the key cannot be recomputed from a session id elsewhere.

Which field a hop reads is known for the hosted APIs below; any other entry (a site proxy in
front of a hosted model) declares it with ``affinity_field``. An entry with no field gets
no key. A server that refuses the field is asked again without it.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any
from urllib.parse import urlparse

#: The field each known hosted API reads, by host.
_KNOWN = {
    "api.openai.com": "prompt_cache_key",
    "openrouter.ai": "session_id",
}
FIELDS = frozenset({"prompt_cache_key", "session_id", "user"})
_SALT: bytes | None = None


def _salt() -> bytes:
    global _SALT
    if _SALT is not None:
        return _SALT
    try:
        from axiom.infra.paths import get_platform_state_dir

        path = get_platform_state_dir() / "routing" / "affinity.salt"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(secrets.token_bytes(32))
        _SALT = path.read_bytes()
    except (OSError, FileExistsError):
        # No writable state: a salt for this process. Keys stay stable within it.
        _SALT = secrets.token_bytes(32)
    return _SALT


def affinity_key(session: str, entry_uid: str) -> str:
    """32 hex characters from the node's salt, the session and the entry."""
    return hmac.new(_salt(), f"{session}\x00{entry_uid}".encode(), hashlib.sha256).hexdigest()[:32]


def field_for(provider: Any) -> str | None:
    declared = str(getattr(provider, "affinity_field", "") or "").strip()
    if declared:
        return declared if declared in FIELDS else None  # "none" or anything unknown: no key
    host = urlparse(str(getattr(provider, "endpoint", "") or "")).hostname or ""
    return next(
        (field for known, field in _KNOWN.items() if host == known or host.endswith("." + known)),
        None,
    )


def apply(payload: dict[str, Any], provider: Any, session: str) -> str | None:
    """Write the key into ``payload`` where the entry reads it; return the field used, if any."""
    field = field_for(provider)
    if not session or not field:
        return None
    uid = str(getattr(provider, "uid", "") or getattr(provider, "name", ""))
    payload[field] = affinity_key(session, uid)
    return field
