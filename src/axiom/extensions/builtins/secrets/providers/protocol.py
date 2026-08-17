# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SecretStoreProvider Protocol + SecretRef + Secret wrapper.

Factory/provider split:

    SecretStoreProvider  — *factory*, extends ProviderBase (four-layer
                            identity, ``handles_sensitive_data=True``).
                            ``open(config) -> SecretStore``.
    SecretStore          — *runtime client*. ``get/put/delete``, plus
                            optional ``lease/rotate`` when the provider
                            advertises ``Capabilities.dynamic_credentials``
                            / ``rotation``.

Concrete subclasses live next to this file (``openbao.py``, ``env.py``,
``kubernetes.py``). SEC-1 ships none.

``SecretRef`` is a typed URL: callers/configs use the string form
(``openbao://kv/data/example-host/dp1/db/password``), code paths use the
parsed dataclass. Versioned reads via the ``?version=N`` query.
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any, ClassVar, Mapping, Protocol, runtime_checkable
from urllib.parse import parse_qs, urlsplit

from axiom.infra.provider_base import ProviderBase

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SecretRef — typed URL
# ---------------------------------------------------------------------------


def _coerce_version(raw: str) -> int | str:
    """Version tokens from a ref's ``?version=`` query.

    Backends split on how they identify versions: OpenBao KV v2 and GCP
    Secret Manager use monotonic integers, while Azure Key Vault uses
    opaque hex ids and some backends accept named versions (``latest``).
    Keep numeric tokens as ``int`` (unchanged behaviour for the numeric
    backends) and pass everything else through untouched as ``str``.
    """
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A typed reference to a secret in a SecretStore.

    Parse from string form for CLI/config ergonomics; keep the typed
    form on code paths for safety.

        >>> SecretRef.parse("openbao://kv/data/example-host/dp1/db/password")
        SecretRef(scheme='openbao', path='kv/data/example-host/dp1/db/password', version=None, query={})
        >>> SecretRef.parse("env://NEUT_PG_PASSWORD")
        SecretRef(scheme='env', path='NEUT_PG_PASSWORD', version=None, query={})
        >>> SecretRef.parse("openbao://kv/data/foo?version=3").version
        3
        >>> SecretRef.parse("azure://vault/foo?version=a1b2c3").version
        'a1b2c3'
    """

    scheme: str  # provider kind: "openbao" | "env" | "kubernetes" | ...
    path: str
    # Numeric versions (OpenBao KV v2, GCP Secret Manager) stay ``int``;
    # opaque ids (Azure Key Vault hex ids, named versions like ``latest``)
    # pass through as ``str``. Providers coerce as their backend requires.
    version: int | str | None = None
    query: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, url: str) -> SecretRef:
        parts = urlsplit(url)
        if not parts.scheme:
            raise ValueError(f"SecretRef missing scheme: {url!r}")
        # urlsplit puts the first path segment in netloc when there's no //
        # We always use scheme://netloc/path form, so netloc + path concat.
        path = (parts.netloc + parts.path).lstrip("/")
        if not path:
            raise ValueError(f"SecretRef missing path: {url!r}")
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        version = _coerce_version(q.pop("version")) if "version" in q else None
        return cls(scheme=parts.scheme, path=path, version=version, query=q)

    def __str__(self) -> str:
        url = f"{self.scheme}://{self.path}"
        params = []
        if self.version is not None:
            params.append(f"version={self.version}")
        params.extend(f"{k}={v}" for k, v in self.query.items())
        return url + ("?" + "&".join(params) if params else "")


# ---------------------------------------------------------------------------
# Secret — value + metadata wrapper
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Secret(AbstractContextManager["Secret"]):
    """A secret value plus its provenance.

    Returned by ``SecretStore.get``. The context-manager protocol lets
    callers scope the plaintext lifetime::

        with store.get(ref) as secret:
            connect(password=secret.value)
        # secret.value is zeroed on exit

    ``lease_id`` is set when the backing provider issued dynamic
    credentials (Vault DB engine, OpenBao transit lease, etc.) — the
    consumer is responsible for renewing or letting it expire.
    """

    value: bytes
    metadata: Mapping[str, Any] = field(default_factory=dict)
    lease_id: str | None = None
    # int for numeric backends (OpenBao/GCP); str for opaque-id backends
    # (Azure surfaces its version id in metadata and leaves this None).
    version: int | str | None = None

    def as_str(self, encoding: str = "utf-8") -> str:
        return self.value.decode(encoding)

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        # Best-effort plaintext zeroing — Python doesn't promise this
        # actually scrubs RAM, but it prevents accidental reuse after
        # the context closes.
        self.value = b"\x00" * len(self.value)


# ---------------------------------------------------------------------------
# Capabilities — what does this provider support?
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Capabilities:
    """Per-provider capability advertisement.

    Consumers gate optional API calls on these flags; the registry uses
    them to refuse installs that demand a capability the chosen provider
    doesn't advertise (e.g., refusing to wire dynamic Postgres
    credentials onto the ``env`` provider).
    """

    read: bool = True
    write: bool = False
    delete: bool = False
    list_paths: bool = False
    versions: bool = False
    dynamic_credentials: bool = False  # supports lease()
    rotation: bool = False              # supports rotate()
    audit_stream: bool = False
    encryption_at_rest: bool = True     # False only for dev/env providers


# ---------------------------------------------------------------------------
# SecretStore — runtime client Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class SecretStore(Protocol):
    """Runtime API exposed by a provider's ``open()``.

    Every method is allowed to raise ``PermissionError`` (ACL refusal),
    ``KeyError`` (no such secret), or ``RuntimeError`` (backend
    unreachable). Implementations MUST NOT swallow exceptions silently —
    silent failures here are how plaintext leaks happen.
    """

    capabilities: Capabilities

    def get(self, ref: SecretRef) -> Secret: ...
    def put(self, ref: SecretRef, value: bytes) -> None: ...
    def delete(self, ref: SecretRef) -> None: ...
    def list_paths(self, prefix: str) -> list[str]: ...

    # Optional — only callable when the matching capability is True.
    def lease(self, ref: SecretRef, ttl_seconds: int) -> Secret: ...
    def rotate(self, ref: SecretRef) -> None: ...


# ---------------------------------------------------------------------------
# SecretStoreProvider — factory base
# ---------------------------------------------------------------------------


class SecretStoreProvider(ProviderBase):
    """Factory for ``SecretStore`` clients.

    Subclasses set ``kind`` (registry key), declare ``capabilities`` as a
    class attribute, and implement ``open()``. They inherit four-layer
    identity (uid + config_hash + instance_id) and the
    ``handles_sensitive_data=True`` flag from ``ProviderBase``.
    """

    _log_prefix = "secret_store_provider"
    handles_sensitive_data: ClassVar[bool] = True  # type: ignore[misc]

    # Subclasses override:
    kind: ClassVar[str] = ""
    capabilities: ClassVar[Capabilities] = Capabilities()

    def open(self) -> SecretStore:  # noqa: D401
        """Return a configured ``SecretStore`` client.

        Implementations may cache the client between calls but MUST
        re-validate the backend connection on each ``open()`` to keep
        ``available()`` honest.
        """
        raise NotImplementedError

    def available(self) -> bool:
        """Reachability check — default refuses until the subclass
        implements it. Forces every concrete provider to be explicit
        about what 'reachable' means for its backend (TCP probe vs
        in-process check vs sealed-vault detection)."""
        return False
