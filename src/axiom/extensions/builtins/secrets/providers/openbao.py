# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``openbao`` SecretStoreProvider — the default OSS backend.

OpenBao is the CNCF Sandbox / MPL-2.0 continuation of HashiCorp Vault;
the HTTP API is wire-compatible. This provider speaks the API directly
over ``urllib`` so we avoid an ``hvac`` runtime dependency for the
common-case install. Heavier features (transit engine, AppRole login,
dynamic DB credentials) live behind the same ``openbao`` kind in
follow-up PRs — SEC-2 ships kv/v2 get/put/delete/list + read-versioning.

Configuration::

    [[secret_store_providers]]
    kind  = "openbao"
    name  = "primary"
    url   = "http://openbao:8200"
    token = "..."                  # or use AXIOM_OPENBAO_TOKEN env
    mount = "kv"                   # kv/v2 mount path

`SecretRef.path` is interpreted as the *full* path past the API root,
typically `kv/data/<key>` for kv/v2. Versioned reads via
`?version=N` work through the kv/v2 endpoint's `version` query.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, ClassVar

from ..providers.protocol import (
    Capabilities,
    Secret,
    SecretRef,
    SecretStore,
    SecretStoreProvider,
)

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HTTP transport (intentionally tiny so tests can swap it)
# ---------------------------------------------------------------------------


class _BaoTransport:
    """Thin urllib wrapper. Tests inject a fake by passing ``http_send``."""

    def __init__(self, base_url: str, token: str, timeout: float = 5.0) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def request(
        self, method: str, path: str, *, body: Any = None,
        query: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base}/v1/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method.upper(),
            headers={
                "X-Vault-Token": self._token,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            # Read body for diagnostics even on error.
            payload = exc.read().decode("utf-8", errors="replace")
            if exc.code == 404:
                raise KeyError(f"openbao 404 at {path}: {payload[:200]}") from exc
            if exc.code in (401, 403):
                raise PermissionError(
                    f"openbao {exc.code} at {path}: {payload[:200]}"
                ) from exc
            raise RuntimeError(
                f"openbao HTTP {exc.code} at {path}: {payload[:200]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(
                f"openbao unreachable at {self._base}: {exc.reason}"
            ) from exc
        if not raw:
            return {}
        return json.loads(raw)


# ---------------------------------------------------------------------------
# kv/v2 SecretStore
# ---------------------------------------------------------------------------


class _OpenBaoKVv2Store:
    """SecretStore over kv/v2. Path shape ``kv/data/<key>``.

    `SecretRef.path` should INCLUDE the mount + ``data/`` segments
    (e.g. ``kv/data/example-host/dp1/db/password``). This keeps the ref
    format identical to the OpenBao API and lets us address other
    engines (transit/db) under the same provider in follow-ups
    without reshaping the URL grammar.
    """

    capabilities = Capabilities(
        read=True,
        write=True,
        delete=True,
        list_paths=True,
        versions=True,
        dynamic_credentials=False,   # transit/DB engines land later
        rotation=False,              # ditto
        audit_stream=True,           # OpenBao writes audit by itself
        encryption_at_rest=True,
    )

    def __init__(self, transport: _BaoTransport) -> None:
        self._t = transport

    # kv/v2 stores secrets under {"data": {<keys>: <values>}} in JSON.
    # We treat the *whole* JSON map as the secret payload (encoded as
    # JSON bytes) so callers can store arbitrary structured material
    # while a flat "value" key is the documented convention.

    def _read(self, ref: SecretRef) -> dict[str, Any]:
        q: dict[str, str] = {}
        if ref.version is not None:
            q["version"] = str(ref.version)
        resp = self._t.request("GET", ref.path, query=q or None)
        return resp.get("data") or {}

    def get(self, ref: SecretRef) -> Secret:
        data = self._read(ref)
        # kv/v2 wraps the payload under "data" inside "data".
        payload = data.get("data") if isinstance(data, dict) else None
        if payload is None:
            raise KeyError(f"openbao: no payload at {ref.path}")
        metadata = data.get("metadata") or {}
        # Convention: a "value" key is the bytes; otherwise we encode
        # the whole payload as JSON.
        if "value" in payload and isinstance(payload["value"], str):
            value = payload["value"].encode("utf-8")
        else:
            value = json.dumps(payload).encode("utf-8")
        version = metadata.get("version")
        return Secret(
            value=value,
            metadata={"backend": "openbao", **metadata},
            lease_id=None,
            version=int(version) if version is not None else None,
        )

    def put(self, ref: SecretRef, value: bytes) -> None:
        # Convention: stash bytes (utf-8) under a "value" key. Callers
        # that want structured layouts can manage the JSON themselves
        # via the underlying transport.
        body = {"data": {"value": value.decode("utf-8")}}
        self._t.request("POST", ref.path, body=body)

    def delete(self, ref: SecretRef) -> None:
        # In kv/v2, soft delete vs destroy is path-different. We soft-delete
        # by default; ``delete`` operates on ``kv/data/<key>``.
        self._t.request("DELETE", ref.path)

    def list_paths(self, prefix: str) -> list[str]:
        # kv/v2 LIST is at ``kv/metadata/<prefix>``.
        # Convert ``kv/data/foo`` → ``kv/metadata/foo`` if the caller
        # passed the data-form prefix; otherwise use as-is.
        list_path = prefix.replace("/data/", "/metadata/", 1) \
            if "/data/" in prefix else prefix
        try:
            resp = self._t.request("LIST", list_path)
        except KeyError:
            return []
        data = resp.get("data") or {}
        return sorted(data.get("keys") or [])

    def lease(self, ref: SecretRef, ttl_seconds: int) -> Secret:  # pragma: no cover
        raise PermissionError(
            "kv/v2 does not issue leased credentials; use the transit or "
            "database engine (SEC-6 follow-up)"
        )

    def rotate(self, ref: SecretRef) -> None:  # pragma: no cover
        raise PermissionError(
            "kv/v2 does not rotate; SEC-6 wires PULSE-driven rotation"
        )


# ---------------------------------------------------------------------------
# Provider (factory)
# ---------------------------------------------------------------------------


class OpenBaoSecretStoreProvider(SecretStoreProvider):
    """Factory for the kv/v2 client."""

    _log_prefix = "secret_store_provider"
    _fingerprint_fields = ("url", "mount")
    _required_config = ("url",)
    kind: ClassVar[str] = "openbao"
    capabilities: ClassVar[Capabilities] = _OpenBaoKVv2Store.capabilities

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._url: str = config["url"]
        token = config.get("token") or os.environ.get("AXIOM_OPENBAO_TOKEN", "")
        if not token:
            raise ValueError(
                "openbao provider requires a token in config or "
                "AXIOM_OPENBAO_TOKEN env"
            )
        self._token = token
        self._mount = config.get("mount", "kv")
        # Allow tests to inject a fake transport via config.
        self._transport: _BaoTransport = (
            config.get("_transport")
            or _BaoTransport(self._url, self._token,
                             timeout=float(config.get("timeout", 5.0)))
        )

    def open(self) -> SecretStore:  # type: ignore[override]
        return _OpenBaoKVv2Store(self._transport)

    def available(self) -> bool:  # type: ignore[override]
        """Probe ``sys/health`` — returns True iff OpenBao is unsealed."""
        try:
            self._transport.request("GET", "sys/health")
            return True
        except Exception:
            return False


__all__ = ["OpenBaoSecretStoreProvider"]
