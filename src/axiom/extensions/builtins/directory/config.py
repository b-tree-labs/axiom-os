# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Environment → the directory seam's moving parts, in one place.

The same names the HTTP edge reads (``AXIOM_DIRECTORY_*``), so a node
configures the provider once and both the per-request resolver and the
cadenced sync see it::

    AXIOM_DIRECTORY_PROVIDER=local|entra|oidc_claims|none   # default oidc_claims
    AXIOM_DIRECTORY_LOCAL_FILE=~/.config/axiom/directory.json
    AXIOM_DIRECTORY_ROLE_MAP=~/.config/axiom/roles.json
    AXIOM_DIRECTORY_SYNC_GROUPS=grp-a,grp-b      # default: the role map's literal groups
    AXIOM_DIRECTORY_TUPLE_STORE=json:<path>|openfga
    AXIOM_DIRECTORY_REVOKED_FILE=<state>/directory/revoked.json
    AXIOM_DIRECTORY_ENTRA_TENANT / _CLIENT_ID / _CLIENT_SECRET(_FILE)   # entra only

A misconfiguration is *reported* on the config object, not raised on load, so
``axi directory status`` can show it and ``sync`` can refuse with a sentence.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.directory.mapping import GroupRoleMap
from axiom.extensions.builtins.directory.protocol import DirectoryCapability, GroupRef
from axiom.extensions.builtins.directory.reconcile import TupleStore
from axiom.extensions.builtins.directory.revoked import JsonFileRevokedSet
from axiom.extensions.builtins.directory.stores import resolve_tuple_store

PROVIDER_ENV = "AXIOM_DIRECTORY_PROVIDER"
LOCAL_FILE_ENV = "AXIOM_DIRECTORY_LOCAL_FILE"
ROLE_MAP_ENV = "AXIOM_DIRECTORY_ROLE_MAP"
SYNC_GROUPS_ENV = "AXIOM_DIRECTORY_SYNC_GROUPS"
REVOKED_FILE_ENV = "AXIOM_DIRECTORY_REVOKED_FILE"
ENTRA_TENANT_ENV = "AXIOM_DIRECTORY_ENTRA_TENANT"
ENTRA_CLIENT_ID_ENV = "AXIOM_DIRECTORY_ENTRA_CLIENT_ID"
ENTRA_CLIENT_SECRET_ENV = "AXIOM_DIRECTORY_ENTRA_CLIENT_SECRET"
ENTRA_CLIENT_SECRET_FILE_ENV = "AXIOM_DIRECTORY_ENTRA_CLIENT_SECRET_FILE"

DEFAULT_PROVIDER = "oidc_claims"
_WILDCARDS = set("*?[")


@dataclass
class DirectoryConfig:
    provider_name: str
    provider: Any | None
    role_map: GroupRoleMap
    sync_groups: tuple[GroupRef, ...]
    revoked_path: Path
    tuple_store: TupleStore | None
    errors: list[str] = field(default_factory=list)

    @property
    def can_enumerate(self) -> bool:
        caps = getattr(self.provider, "capabilities", frozenset())
        return self.provider is not None and DirectoryCapability.ENUMERATE in caps

    def revoked_set(self) -> JsonFileRevokedSet:
        return JsonFileRevokedSet(self.revoked_path)

    def summary(self) -> dict[str, Any]:
        from axiom.extensions.builtins.directory.stores import describe_store

        return {
            "provider": self.provider_name,
            "can_enumerate": self.can_enumerate,
            "role_map_rules": len(self.role_map.rules),
            "sync_groups": [g.id for g in self.sync_groups],
            "tuple_store": describe_store(self.tuple_store) if self.tuple_store else None,
            "revoked_file": str(self.revoked_path),
            "errors": list(self.errors),
        }


def _secret(env: Mapping[str, str], value_key: str, file_key: str) -> str | None:
    value = (env.get(value_key) or "").strip()
    if value:
        return value
    path = (env.get(file_key) or "").strip()
    if path:
        return Path(os.path.expanduser(path)).read_text(encoding="utf-8").strip() or None
    return None


def _entra_provider(env: Mapping[str, str], errors: list[str], graph_client: Any | None):
    """The Entra directory over an injected Graph client, or app-only MSAL.

    Enumerating members needs the *application* permission
    ``GroupMember.Read.All`` on the registration; without it every
    ``members_of`` 403s and the sync reports the group as failed (never as empty).
    """
    from axiom.extensions.builtins.directory.providers import get_provider

    if graph_client is None:
        tenant = (env.get(ENTRA_TENANT_ENV) or "").strip()
        client_id = (env.get(ENTRA_CLIENT_ID_ENV) or "").strip()
        secret = _secret(env, ENTRA_CLIENT_SECRET_ENV, ENTRA_CLIENT_SECRET_FILE_ENV)
        if not (tenant and client_id and secret):
            errors.append(
                f"provider=entra needs {ENTRA_TENANT_ENV}, {ENTRA_CLIENT_ID_ENV} and "
                f"{ENTRA_CLIENT_SECRET_ENV}(_FILE)"
            )
            return None
        try:
            from axiom.extensions.builtins.schedule.calendar.vendors.m365 import _build_client

            raw = _build_client(
                {"tenant_id": tenant, "client_id": client_id, "client_secret": secret}
            )
        except Exception as exc:  # noqa: BLE001 — surfaced on the config, not raised
            errors.append(f"entra Graph client unavailable: {exc}")
            return None
        graph_client = _GraphGet(raw)
    return get_provider("entra", client=graph_client)


class _GraphGet:
    """Adapt the calendar vendors' ``request(method, path)`` client to the
    ``get(path) -> dict`` shape the directory provider expects."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def get(self, path: str) -> dict:
        # ``@odata.nextLink`` comes back absolute; the vendor client prefixes
        # its base, so hand it the relative part or the URL doubles up.
        base = getattr(self._client, "base", "") or ""
        if base and path.startswith(base):
            path = path[len(base) :]
        return self._client.request("GET", path) or {}


def load_directory_config(
    env: Mapping[str, str] | None = None,
    *,
    state_dir: Path | None = None,
    graph_client: Any | None = None,
) -> DirectoryConfig:
    env = os.environ if env is None else env
    errors: list[str] = []
    provider_name = (env.get(PROVIDER_ENV) or DEFAULT_PROVIDER).strip().lower()
    provider = None

    try:
        from axiom.extensions.builtins.directory.providers import get_provider

        if provider_name == "local":
            path = (env.get(LOCAL_FILE_ENV) or "").strip()
            if not path:
                errors.append(f"provider=local needs {LOCAL_FILE_ENV}")
            else:
                provider = get_provider("local", path=os.path.expanduser(path))
        elif provider_name == "entra":
            provider = _entra_provider(env, errors, graph_client)
        elif provider_name == "oidc_claims":
            provider = get_provider("oidc_claims")
        elif provider_name == "none":
            provider = None
        else:
            errors.append(f"unknown {PROVIDER_ENV} {provider_name!r}")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"provider {provider_name!r} failed to load: {exc}")

    role_map = GroupRoleMap()
    role_map_path = (env.get(ROLE_MAP_ENV) or "").strip()
    if role_map_path:
        try:
            role_map = GroupRoleMap.from_file(os.path.expanduser(role_map_path))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"role map unreadable: {exc}")

    raw_groups = [g.strip() for g in (env.get(SYNC_GROUPS_ENV) or "").split(",") if g.strip()]
    if not raw_groups:
        # Default: every literal (non-wildcard) group the role map names —
        # the groups a deployment has actually decided mean something.
        raw_groups = [
            str(rule.get("group"))
            for rule in role_map.rules
            if rule.get("group") and not (_WILDCARDS & set(str(rule.get("group"))))
        ]
        if not raw_groups and provider_name == "local" and provider is not None:
            raw_groups = sorted(getattr(provider, "_groups", {}).keys())
    sync_groups = tuple(GroupRef(id=g, provider=provider_name) for g in raw_groups)

    base = state_dir if state_dir is not None else _default_state_dir()
    revoked_raw = (env.get(REVOKED_FILE_ENV) or "").strip()
    revoked_path = (
        Path(os.path.expanduser(revoked_raw))
        if revoked_raw
        else base / "directory" / "revoked.json"
    )

    tuple_store = None
    try:
        tuple_store = resolve_tuple_store(env)
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))

    return DirectoryConfig(
        provider_name=provider_name,
        provider=provider,
        role_map=role_map,
        sync_groups=sync_groups,
        revoked_path=revoked_path,
        tuple_store=tuple_store,
        errors=errors,
    )


def _default_state_dir() -> Path:
    try:
        from axiom.infra.paths import get_user_state_dir

        return get_user_state_dir()
    except Exception:  # noqa: BLE001 — branding not initialised (tests)
        return Path(os.path.expanduser("~/.axi"))


__all__ = [
    "DEFAULT_PROVIDER",
    "ENTRA_CLIENT_ID_ENV",
    "ENTRA_CLIENT_SECRET_ENV",
    "ENTRA_CLIENT_SECRET_FILE_ENV",
    "ENTRA_TENANT_ENV",
    "LOCAL_FILE_ENV",
    "PROVIDER_ENV",
    "REVOKED_FILE_ENV",
    "ROLE_MAP_ENV",
    "SYNC_GROUPS_ENV",
    "DirectoryConfig",
    "load_directory_config",
]
