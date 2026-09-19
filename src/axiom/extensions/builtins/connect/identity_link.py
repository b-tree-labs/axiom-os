# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""External-identity binding — a channel login as a proxy for an Axiom principal.

A Slack/Teams/SMS message arrives with a *vendor* user id; ACLs need an Axiom
principal. This is the owner-asserted link:

    (connector, external_id)  ->  @principal:context

Established at connector setup by the authorized owner (who is already the
verified installer), so an inbound channel message resolves to an authenticated
principal that ``Ownership.can_exercise`` can gate on. The binding is as strong
as the channel's auth of that user plus the one-time assertion; sensitive ops
still require step-up (see agent_settings). Stronger link methods (verified DM,
SSO/OIDC) plug in behind the same ``resolve_principal`` seam later.
"""

from __future__ import annotations

import json
from pathlib import Path


def _path() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "connectors" / "identity-links.json"


def _key(connector: str, external_id: str) -> str:
    return f"{connector}:{external_id}"


def _load() -> dict:
    p = _path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def link_identity(connector: str, external_id: str, principal: str, *, path: Path | None = None) -> None:
    """Assert that ``external_id`` on ``connector`` is the Axiom ``principal``."""
    p = path or _path()
    data = json.loads(p.read_text()) if p.exists() else {}
    data[_key(connector, external_id)] = principal
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


def resolve_principal(connector: str, external_id: str, *, path: Path | None = None) -> str | None:
    """The Axiom principal bound to a channel login, or None if unbound."""
    data = json.loads(path.read_text()) if (path and path.exists()) else (
        _load() if path is None else {})
    return data.get(_key(connector, external_id))


def unlink_identity(connector: str, external_id: str, *, path: Path | None = None) -> bool:
    p = path or _path()
    data = json.loads(p.read_text()) if p.exists() else {}
    if _key(connector, external_id) in data:
        del data[_key(connector, external_id)]
        p.write_text(json.dumps(data, indent=2))
        return True
    return False


__all__ = ["link_identity", "resolve_principal", "unlink_identity"]
