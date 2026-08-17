# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Registry Fabric — Phase 1: the core seam, in-process registry, and a
server.json-aligned connector descriptor (ADR-074).

This is the *formal home* a connector lives in, independent of any vendor.
Design choices, grounded in the prevailing standards (see ADR-074):

- The descriptor is a **profile of MCP's `server.json`** (reverse-DNS name,
  version, `environmentVariables[]{isSecret}`), so Axiom is a conformant
  sub-registry rather than a fork. Axiom-specific data (artifact class,
  trust tier, direction, classification) rides in the reverse-DNS ``_meta``
  escape hatch — never as forked top-level keys.
- Artifacts are **typed by class** (connector / extension / pack /
  inference) over one fabric; ``kind`` is the sub-type within a class
  (e.g. ``channel_adapter``). Per-class install rigor is layered later
  (Phase 3); Phase 1 is registration + resolution + catalog only.
- The seam stays in ``axiom.infra`` with an in-process default; an opt-in
  Connector Fabric *service* supersedes it later (Phase 4 federation).
  Nothing in core depends on an extension.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

_META = "ai.axiom.registry"  # reverse-DNS namespace for Axiom _meta keys
_REVERSE_DNS = re.compile(r"^[a-z0-9]+(\.[a-z0-9_-]+)+(/[a-z0-9._-]+)?$")


class ArtifactClass(str, Enum):
    """Top-level typing — each class plugs in its own installer/risk tier."""

    CONNECTOR = "connector"
    EXTENSION = "extension"
    PACK = "pack"
    INFERENCE_RESOURCE = "inference_resource"


class TrustTier(str, Enum):
    """Graduated trust ladder (Power Platform model), not binary."""

    FIRST_PARTY = "first_party"
    VERIFIED = "verified"      # publisher owns the service
    CERTIFIED = "certified"    # platform-reviewed
    COMMUNITY = "community"    # unreviewed / independent


class Availability(str, Enum):
    """Where a connector is in its rollout — connectors trickle out over time."""

    AVAILABLE = "available"      # ready to install now
    PLANNED = "planned"          # catalogued (with deep links) but not shipped yet
    DEPRECATED = "deprecated"    # being retired


@dataclass(frozen=True)
class SetupSpec:
    """How a human turns a connector on, for the installer/updater to render.

    ``urls`` are clickable, deep-linked vendor console pages (create the app /
    generate a token / register in Entra). ``needs`` is the short list of
    things the user will provide. ``install_kind`` drives the guided flow.
    """

    install_kind: str  # app_manifest | oauth | developer_token | app_registration
    summary: str
    needs: tuple[str, ...] = ()
    urls: dict[str, str] = field(default_factory=dict)
    enabled_by_default: bool = False
    # Vendor-facing setup copy lives here (DATA, not code) so it can change
    # without touching the installer. ``credential_url_label`` names which of
    # ``urls`` to open for the pre-step credential; ``instructions`` are the
    # ordered guidance lines; ``prompt`` is the interactive paste prompt;
    # ``credential_hint`` is the expected token prefix.
    instructions: tuple[str, ...] = ()
    prompt: str | None = None
    credential_url_label: str | None = None
    credential_hint: str | None = None
    # Error→remedy guidance (data): ordered (error-substring, remedy) pairs the
    # installer matches a failure against to tell the user how to fix it. First
    # match wins; unmatched errors escalate to AXI/LLM diagnosis.
    error_remedies: tuple[tuple[str, str], ...] = ()
    # In-place UPDATE copy (data, not code) for the `upgrade` flow — evolving a
    # deployed connector without a teardown. ``app_id_hint`` tells the user
    # where to find the deployed app id; ``update_prompt`` is the credential
    # paste prompt in the update context; ``reconsent_url`` is a ``{app_id}``
    # template for the one re-consent click when an update adds scopes;
    # ``reconsent_note`` is the reassurance copy (tokens persist, no re-paste).
    app_id_hint: str | None = None
    update_prompt: str | None = None
    reconsent_url: str | None = None
    reconsent_note: str | None = None

    def remedy_for(self, error_text: str) -> str | None:
        low = error_text.lower()
        for sub, remedy in self.error_remedies:
            if sub.lower() in low:
                return remedy
        return None

    def to_dict(self) -> dict:
        return {
            "install_kind": self.install_kind,
            "summary": self.summary,
            "needs": list(self.needs),
            "urls": dict(self.urls),
            "enabled_by_default": self.enabled_by_default,
            "instructions": list(self.instructions),
            "prompt": self.prompt,
            "credential_url_label": self.credential_url_label,
            "credential_hint": self.credential_hint,
            "error_remedies": [list(p) for p in self.error_remedies],
            "app_id_hint": self.app_id_hint,
            "update_prompt": self.update_prompt,
            "reconsent_url": self.reconsent_url,
            "reconsent_note": self.reconsent_note,
        }


@dataclass(frozen=True)
class EnvVar:
    """A config/secret input the connector needs — mirrors server.json's
    ``environmentVariables[]``. A secret var declares that it *is* secret;
    it never carries the value (the value lives in the keystore)."""

    name: str
    description: str = ""
    is_required: bool = False
    is_secret: bool = False
    default: str | None = None
    # Navigation aids (data, not code) so the installer can guide the user to
    # where this value lives. ``url`` may contain ``{app_id}`` for a precise
    # per-app deep link; ``where`` is the human page/path.
    where: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if self.is_secret and self.default is not None:
            raise ValueError(
                f"env var {self.name!r} is secret and must not carry a value; "
                "secrets resolve from the keystore, not the descriptor"
            )

    def to_server_json(self) -> dict:
        d = {
            "name": self.name,
            "description": self.description,
            "isRequired": self.is_required,
            "isSecret": self.is_secret,
        }
        if self.default is not None:
            d["default"] = self.default
        return d


@dataclass
class ConnectorDescriptor:
    """A registry entry — a profile of MCP ``server.json``.

    ``name`` is reverse-DNS (``ai.axiom.connector.slack``). Axiom-specific
    fields ride in ``meta`` under the ``ai.axiom.registry/*`` namespace.
    """

    name: str
    version: str
    title: str
    description: str
    artifact_class: ArtifactClass
    kind: str  # sub-type within the class, e.g. "channel_adapter"
    transport: str | None = None  # stdio | streamable-http | sse | socket-mode
    env: list[EnvVar] = field(default_factory=list)
    connection_ref: str | None = None  # links to a Connection (creds), ADR-074
    provider_entry: str | None = None  # "module:provider" for the runtime adapter
    setup: SetupSpec | None = None      # how a human turns it on (deep links etc.)
    availability: Availability = Availability.AVAILABLE
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _REVERSE_DNS.match(self.name):
            raise ValueError(
                f"connector name {self.name!r} must be reverse-DNS "
                "(e.g. 'ai.axiom.connector.slack')"
            )

    @property
    def trust_tier(self) -> TrustTier:
        return TrustTier(self.meta.get(f"{_META}/trust_tier", TrustTier.COMMUNITY.value))

    def to_server_json(self) -> dict:
        """Serialize to the server.json shape, Axiom data in ``_meta``."""
        meta = dict(self.meta)
        meta[f"{_META}/artifact_class"] = self.artifact_class.value
        meta[f"{_META}/kind"] = self.kind
        if self.connection_ref:
            meta[f"{_META}/connection_ref"] = self.connection_ref
        out: dict = {
            "name": self.name,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "environmentVariables": [e.to_server_json() for e in self.env],
            "_meta": meta,
        }
        if self.transport:
            out["_meta"][f"{_META}/transport"] = self.transport
        out["_meta"][f"{_META}/availability"] = self.availability.value
        if self.setup is not None:
            out["_meta"][f"{_META}/setup"] = self.setup.to_dict()
        return out


@runtime_checkable
class ConnectorFabric(Protocol):
    """The seam. The in-process default ships in core; a service supersedes
    it later through this same interface (ADR-074)."""

    def register(self, descriptor: ConnectorDescriptor, *, replace: bool = False) -> None: ...
    def get(self, name: str) -> ConnectorDescriptor | None: ...
    def catalog(
        self, *, artifact_class: ArtifactClass | None = None, kind: str | None = None
    ) -> list[ConnectorDescriptor]: ...


class InProcessConnectorFabric:
    """Local, in-process default registry — the Phase-1 floor."""

    def __init__(self) -> None:
        self._by_name: dict[str, ConnectorDescriptor] = {}

    def register(self, descriptor: ConnectorDescriptor, *, replace: bool = False) -> None:
        if descriptor.name in self._by_name and not replace:
            raise ValueError(
                f"connector {descriptor.name!r} already registered; "
                "pass replace=True to override"
            )
        self._by_name[descriptor.name] = descriptor

    def get(self, name: str) -> ConnectorDescriptor | None:
        return self._by_name.get(name)

    def catalog(
        self, *, artifact_class: ArtifactClass | None = None, kind: str | None = None
    ) -> list[ConnectorDescriptor]:
        out: Iterable[ConnectorDescriptor] = self._by_name.values()
        if artifact_class is not None:
            out = [d for d in out if d.artifact_class is artifact_class]
        if kind is not None:
            out = [d for d in out if d.kind == kind]
        return list(out)


# ---------------------------------------------------------------------------
# Connector enablement state (vendor-agnostic; descriptors live in extensions)
# ---------------------------------------------------------------------------


class ConnectorState:
    """Which connectors the operator has switched ON. Disabled by default;
    enabling is an explicit, per-connector opt-in. (In-process floor; a
    persisted backend plugs in behind the same surface.)"""

    def __init__(self, enabled: set[str] | None = None) -> None:
        self._enabled: set[str] = set(enabled or ())

    def enable(self, name: str) -> None:
        self._enabled.add(name)

    def disable(self, name: str) -> None:
        self._enabled.discard(name)

    def is_enabled(self, name: str) -> bool:
        return name in self._enabled

    def enabled(self) -> set[str]:
        return set(self._enabled)


_DEFAULT_STATE: ConnectorState | None = None


def default_state() -> ConnectorState:
    global _DEFAULT_STATE
    if _DEFAULT_STATE is None:
        _DEFAULT_STATE = ConnectorState()
    return _DEFAULT_STATE


class ConnectionStatus(str, Enum):
    PENDING = "pending"    # created, not yet authenticated
    ACTIVE = "active"      # authenticated + verified
    ERROR = "error"        # auth/health failing
    REVOKED = "revoked"    # creds withdrawn


@dataclass
class ConnectionInstance:
    """One authenticated instance of a connector (e.g. the Slack workspace
    install for #ops-sysadmin). Credentials live behind ``secret_ref``
    (KEEP / keystore) — never inline. Mirrors the universal connection-vs-
    connector split (Power Platform / Merge / Pipedream)."""

    name: str
    connector: str             # reverse-DNS connector descriptor name
    owner: str                 # principal who owns/installed it
    secret_ref: str            # keystore pointer for creds (never plaintext)
    status: ConnectionStatus = ConnectionStatus.PENDING
    scopes: list[str] = field(default_factory=list)
    webhook_urls: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.secret_ref:
            raise ValueError(
                f"connection {self.name!r} requires a secret_ref; "
                "credentials resolve from the keystore, never inline"
            )


class ConnectionStore:
    """In-process registry of connection instances (Phase-2 floor)."""

    def __init__(self) -> None:
        self._by_name: dict[str, ConnectionInstance] = {}

    def put(self, conn: ConnectionInstance, *, replace: bool = True) -> None:
        if conn.name in self._by_name and not replace:
            raise ValueError(f"connection {conn.name!r} already exists")
        self._by_name[conn.name] = conn

    def get(self, name: str) -> ConnectionInstance | None:
        return self._by_name.get(name)

    def set_status(
        self, name: str, status: ConnectionStatus, *, scopes: list[str] | None = None
    ) -> ConnectionInstance:
        c = self._by_name[name]
        c.status = status
        if scopes is not None:
            c.scopes = scopes
        return c

    def for_connector(self, connector: str) -> list[ConnectionInstance]:
        return [c for c in self._by_name.values() if c.connector == connector]

    def all(self) -> list[ConnectionInstance]:
        return list(self._by_name.values())

_DEFAULT_CONNECTIONS: ConnectionStore | None = None


def default_connections() -> ConnectionStore:
    global _DEFAULT_CONNECTIONS
    if _DEFAULT_CONNECTIONS is None:
        _DEFAULT_CONNECTIONS = ConnectionStore()
    return _DEFAULT_CONNECTIONS


_DEFAULT: InProcessConnectorFabric | None = None


def default_fabric() -> InProcessConnectorFabric:
    """The platform's default in-process fabric, with built-in connectors
    registered. Lazy so import is cheap and order-independent."""
    global _DEFAULT
    if _DEFAULT is None:
        # Vendor-agnostic: empty by default. Extensions register their
        # connectors into it (e.g. connect.connectors.register_builtin_connectors).
        _DEFAULT = InProcessConnectorFabric()
    return _DEFAULT


__all__ = [
    "ArtifactClass",
    "TrustTier",
    "Availability",
    "SetupSpec",
    "EnvVar",
    "ConnectorDescriptor",
    "ConnectorFabric",
    "InProcessConnectorFabric",
    "ConnectionStatus",
    "ConnectionInstance",
    "ConnectionStore",
    "ConnectorState",
    "default_fabric",
    "default_connections",
    "default_state",
]
