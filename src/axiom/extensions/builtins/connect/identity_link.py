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


def _read(p: Path) -> dict:
    """The bindings at ``p``. Damage raises :class:`StateDamaged` (file untouched)."""
    from axiom.infra.state_integrity import load_json_state

    return load_json_state(
        p, expect=dict, default={}, fail_closed=True, what="channel identity links",
        fix=f"restore a good copy of {p}, or move it aside with `mv '{p}' '{p}.bad'` and re-link "
        "each channel login",
    )


def _write(p: Path, data: dict) -> None:
    from axiom.infra.state_integrity import atomic_write_text

    atomic_write_text(p, json.dumps(data, indent=2))


def _load() -> dict:
    """For lookups: damage means NOBODY is bound (fail closed), said loudly."""
    import logging

    from axiom.infra.state_integrity import StateDamaged

    try:
        return _read(_path())
    except StateDamaged as exc:
        logging.getLogger(__name__).error(
            "channel identity links are unreadable, so no channel login resolves to a principal: %s", exc
        )
        return {}


def link_identity(connector: str, external_id: str, principal: str, *, path: Path | None = None) -> None:
    """Assert that ``external_id`` on ``connector`` is the Axiom ``principal``."""
    p = path or _path()
    data = _read(p)
    data[_key(connector, external_id)] = principal
    _write(p, data)


def resolve_principal(connector: str, external_id: str, *, path: Path | None = None) -> str | None:
    """The Axiom principal bound to a channel login, or None if unbound."""
    if path is not None:
        try:
            data = _read(path) if path.exists() else {}
        except Exception:  # noqa: BLE001 - lookups fail closed
            data = {}
    else:
        data = _load()
    return data.get(_key(connector, external_id))


def unlink_identity(connector: str, external_id: str, *, path: Path | None = None) -> bool:
    p = path or _path()
    data = _read(p)
    if _key(connector, external_id) in data:
        del data[_key(connector, external_id)]
        _write(p, data)
        return True
    return False
