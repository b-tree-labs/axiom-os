# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``kubernetes`` SecretStoreProvider — reads Kubernetes Secrets.

In production the recommended K8s topology is OpenBao backing the
in-cluster secrets via the CSI Secret Store driver (ADR-002): OpenBao
holds the source of truth, the CSI driver projects values into
SecretProviderClass-bound Kubernetes Secret objects, and this provider
reads from those Secret objects. The provider itself is intentionally
*just* a K8s API client — it doesn't know about OpenBao, so the same
shape works for any backend the CSI driver supports (AWS Secrets
Manager, Azure KV, GCP Secret Manager).

``SecretRef`` shape::

    kubernetes://<namespace>/<name>[/<key>]

Without ``<key>`` the whole Secret is returned as a JSON object
(base64-decoded values); with ``<key>``, only that one entry is
returned. SEC-3 ships read-only; ``put``/``delete`` land alongside the
CSI auto-template work in SEC-3b.

This module imports ``kubernetes`` lazily so the dep is required only
on hosts that actually configure this provider — keeps the wheel size
flat for CLI-only callers.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, ClassVar

from ..providers.protocol import (
    Capabilities,
    Secret,
    SecretRef,
    SecretStore,
    SecretStoreProvider,
)

_log = logging.getLogger(__name__)


class _KubernetesSecretStore:
    """Read Kubernetes Secret objects via the API."""

    capabilities = Capabilities(
        read=True,
        write=False,
        delete=False,
        list_paths=True,
        versions=False,
        dynamic_credentials=False,
        rotation=False,
        audit_stream=False,  # K8s audit is cluster-level, not per-secret
        encryption_at_rest=True,  # if etcd encryption-at-rest is configured
    )

    def __init__(self, *, kube_context: str | None, in_cluster: bool) -> None:
        self._kube_context = kube_context
        self._in_cluster = in_cluster
        self._api: Any = None  # late-bound

    def _ensure_api(self) -> Any:
        if self._api is not None:
            return self._api
        from kubernetes import client, config

        if self._in_cluster:
            config.load_incluster_config()
        else:
            config.load_kube_config(context=self._kube_context)
        self._api = client.CoreV1Api()
        return self._api

    def _split(self, ref: SecretRef) -> tuple[str, str, str | None]:
        """Parse ``kubernetes://<ns>/<name>[/<key>]`` → (ns, name, key|None)."""
        parts = ref.path.split("/", 2)
        if len(parts) < 2:
            raise ValueError(
                f"kubernetes SecretRef must be ns/name[/key], got {ref.path!r}"
            )
        ns, name = parts[0], parts[1]
        key = parts[2] if len(parts) == 3 else None
        return ns, name, key

    def get(self, ref: SecretRef) -> Secret:
        ns, name, key = self._split(ref)
        api = self._ensure_api()
        try:
            sec = api.read_namespaced_secret(name=name, namespace=ns)
        except Exception as exc:  # noqa: BLE001 — kubernetes.ApiException etc.
            # 404 → KeyError so callers get a consistent miss-signal.
            status = getattr(exc, "status", None)
            if status == 404:
                raise KeyError(f"no Secret {ns}/{name}") from exc
            if status in (401, 403):
                raise PermissionError(
                    f"K8s denied access to {ns}/{name}: {exc}"
                ) from exc
            raise RuntimeError(f"K8s read failed for {ns}/{name}: {exc}") from exc

        data: dict[str, str] = sec.data or {}
        # Each value is base64-encoded per K8s convention.
        decoded = {k: base64.b64decode(v) for k, v in data.items()}

        if key is not None:
            if key not in decoded:
                raise KeyError(f"Secret {ns}/{name} has no key {key!r}")
            value = decoded[key]
            return Secret(
                value=value,
                metadata={
                    "backend": "kubernetes",
                    "namespace": ns,
                    "name": name,
                    "key": key,
                    "resource_version": getattr(
                        sec.metadata, "resource_version", None
                    ),
                },
                lease_id=None,
                version=None,
            )

        # No key requested → JSON object of all keys (string-decoded).
        payload = {k: v.decode("utf-8", errors="replace") for k, v in decoded.items()}
        return Secret(
            value=json.dumps(payload).encode("utf-8"),
            metadata={
                "backend": "kubernetes",
                "namespace": ns,
                "name": name,
                "resource_version": getattr(
                    sec.metadata, "resource_version", None
                ),
            },
            lease_id=None,
            version=None,
        )

    def put(self, ref: SecretRef, value: bytes) -> None:  # pragma: no cover
        raise PermissionError(
            "kubernetes SecretStore is read-only in SEC-3; "
            "CSI-templated writes land in SEC-3b"
        )

    def delete(self, ref: SecretRef) -> None:  # pragma: no cover
        raise PermissionError("kubernetes SecretStore is read-only in SEC-3")

    def list_paths(self, prefix: str) -> list[str]:
        # ``prefix`` shape is ``<ns>`` or ``<ns>/<name-prefix>``.
        parts = prefix.split("/", 1)
        ns = parts[0]
        name_prefix = parts[1] if len(parts) == 2 else ""
        api = self._ensure_api()
        try:
            resp = api.list_namespaced_secret(namespace=ns)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"K8s list failed in ns={ns}: {exc}") from exc
        names = sorted(
            f"{ns}/{item.metadata.name}"
            for item in (resp.items or [])
            if not name_prefix or item.metadata.name.startswith(name_prefix)
        )
        return names

    def lease(self, ref: SecretRef, ttl_seconds: int) -> Secret:  # pragma: no cover
        raise PermissionError(
            "kubernetes SecretStore does not issue leased credentials; "
            "use the CSI driver + an OpenBao DB engine instead"
        )

    def rotate(self, ref: SecretRef) -> None:  # pragma: no cover
        raise PermissionError(
            "kubernetes SecretStore does not rotate; OpenBao + PULSE does"
        )


class KubernetesSecretStoreProvider(SecretStoreProvider):
    """Factory. Honors ``kube_context`` or runs in-cluster."""

    _log_prefix = "secret_store_provider"
    _fingerprint_fields = ("kube_context", "in_cluster")
    kind: ClassVar[str] = "kubernetes"
    capabilities: ClassVar[Capabilities] = _KubernetesSecretStore.capabilities

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._kube_context: str | None = config.get("kube_context")
        self._in_cluster: bool = bool(config.get("in_cluster", False))

    def open(self) -> SecretStore:  # type: ignore[override]
        return _KubernetesSecretStore(
            kube_context=self._kube_context,
            in_cluster=self._in_cluster,
        )

    def available(self) -> bool:  # type: ignore[override]
        """True iff the ``kubernetes`` package is importable.

        We don't probe the API server here — that costs a round-trip
        per call to ``available()`` and the chief use of this method is
        ``axi doctor`` listing per-provider state. Reachability is
        verified the first time a Secret is actually read.
        """
        try:
            import importlib
            importlib.import_module("kubernetes")
            return True
        except Exception:
            return False


__all__ = ["KubernetesSecretStoreProvider"]
