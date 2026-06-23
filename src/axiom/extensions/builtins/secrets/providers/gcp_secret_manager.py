# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``gcp_secret_manager`` SecretStoreProvider — Google Cloud Secret Manager.

GCP-native operational secret store. Authenticates via Application
Default Credentials (ADC) — the canonical GCP pattern: workload
identity in GKE, Compute Engine / Cloud Run / Cloud Functions
metadata-server tokens elsewhere, and a service-account JSON key as
the local-dev fallback (``GOOGLE_APPLICATION_CREDENTIALS``).

``SecretRef`` shape::

    gcp://<project>/<secret-name>            # latest version
    gcp://<project>/<secret-name>?version=N  # specific version
    gcp://<project>/<secret-name>?version=latest

Maps onto Secret Manager's resource grammar
``projects/<project>/secrets/<name>/versions/<version>``.

Capability advertisement (per the registry's contract):
  - read / write / delete       True
  - list_paths                  True
  - versions                    True  (first-class in Secret Manager)
  - encryption_at_rest          True  (Google-managed by default;
                                       CMEK supported by config but
                                       transparent to this provider)
  - audit_stream                True  (Cloud Audit Logs)
  - dynamic_credentials         False (no native equivalent;
                                       short-lived creds come from
                                       Workload Identity, not Secret
                                       Manager itself)
  - rotation                    False (Secret Manager has rotation
                                       schedules but they call out to
                                       user-supplied Pub/Sub-driven
                                       functions; we don't drive that
                                       here — SEC-6 follow-up under
                                       PULSE integration)

The ``google-cloud-secret-manager`` package is imported lazily so the
wheel stays light for installs that don't use this provider.
"""

from __future__ import annotations

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


class _GCPSecretManagerStore:
    """Runtime client. Wraps the GCP SecretManagerServiceClient."""

    capabilities = Capabilities(
        read=True,
        write=True,
        delete=True,
        list_paths=True,
        versions=True,
        dynamic_credentials=False,
        rotation=False,
        audit_stream=True,
        encryption_at_rest=True,
    )

    def __init__(self, *, default_project: str | None, client: Any) -> None:
        self._default_project = default_project
        self._client = client

    # ---- helpers --------------------------------------------------------

    def _resource(self, ref: SecretRef, *, with_version: bool = True) -> str:
        """``gcp://<project>/<name>`` → ``projects/<project>/secrets/<name>``.

        When ``with_version`` is True, append ``/versions/<v>`` using
        ``ref.version`` (default ``latest``).
        """
        parts = ref.path.split("/", 1)
        if len(parts) == 2:
            project, name = parts
        else:
            if not self._default_project:
                raise ValueError(
                    f"gcp SecretRef missing project (got {ref.path!r}); "
                    "either provide project in the URL or set "
                    "project= in provider config"
                )
            project, name = self._default_project, parts[0]
        if not project or not name:
            raise ValueError(f"gcp SecretRef malformed: {ref.path!r}")

        base = f"projects/{project}/secrets/{name}"
        if not with_version:
            return base
        v = ref.version if ref.version is not None else "latest"
        return f"{base}/versions/{v}"

    def _parent_for_list(self, prefix: str) -> str:
        """``<project>`` or ``<project>/<name-prefix>`` → list parent."""
        parts = prefix.split("/", 1)
        project = parts[0] or self._default_project
        if not project:
            raise ValueError(
                "list_paths needs a project (either in the prefix or "
                "via the provider config's project=...)"
            )
        return f"projects/{project}"

    # ---- SecretStore Protocol ------------------------------------------

    def get(self, ref: SecretRef) -> Secret:
        name = self._resource(ref, with_version=True)
        try:
            resp = self._client.access_secret_version(name=name)
        except Exception as exc:  # noqa: BLE001 — translate cloud SDK errors
            code = _sdk_status(exc)
            if code == 404:
                raise KeyError(f"gcp: no secret version at {name}") from exc
            if code in (401, 403):
                raise PermissionError(f"gcp denied {name}: {exc}") from exc
            raise RuntimeError(f"gcp access_secret_version failed: {exc}") from exc

        payload = resp.payload
        value: bytes = payload.data or b""
        # ``resp.name`` is the fully-qualified version resource; parse the
        # tail for the integer version when present.
        version_str = (resp.name or "").rsplit("/", 1)[-1]
        try:
            version = int(version_str)
        except (TypeError, ValueError):
            version = None
        return Secret(
            value=value,
            metadata={
                "backend": "gcp_secret_manager",
                "name": resp.name,
            },
            lease_id=None,
            version=version,
        )

    def put(self, ref: SecretRef, value: bytes) -> None:
        parent = self._resource(ref, with_version=False)
        # add_secret_version will fail if the secret does not exist yet
        # — we eagerly create it if missing to make put() idempotent.
        try:
            self._client.get_secret(name=parent)
        except Exception as exc:  # noqa: BLE001
            if _sdk_status(exc) == 404:
                project_path = parent.rsplit("/secrets/", 1)[0]
                secret_id = parent.rsplit("/", 1)[-1]
                self._client.create_secret(
                    parent=project_path,
                    secret_id=secret_id,
                    secret={"replication": {"automatic": {}}},
                )
            else:
                raise RuntimeError(f"gcp get_secret failed: {exc}") from exc

        self._client.add_secret_version(
            parent=parent,
            payload={"data": value},
        )

    def delete(self, ref: SecretRef) -> None:
        # Two delete semantics in Secret Manager:
        #   - delete_secret: drops the whole secret + every version
        #   - destroy_secret_version: drops just one version's payload
        # When the ref names a specific version, destroy that; otherwise
        # delete the secret.
        if ref.version is not None:
            name = self._resource(ref, with_version=True)
            self._client.destroy_secret_version(name=name)
        else:
            name = self._resource(ref, with_version=False)
            self._client.delete_secret(name=name)

    def list_paths(self, prefix: str) -> list[str]:
        parent = self._parent_for_list(prefix)
        name_prefix = ""
        if "/" in prefix:
            _, name_prefix = prefix.split("/", 1)
        try:
            secrets = list(self._client.list_secrets(parent=parent))
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"gcp list_secrets failed: {exc}") from exc
        out: list[str] = []
        project_segment = parent.rsplit("/", 1)[-1]
        for s in secrets:
            short = (s.name or "").rsplit("/", 1)[-1]
            if name_prefix and not short.startswith(name_prefix):
                continue
            out.append(f"{project_segment}/{short}")
        return sorted(out)

    def lease(self, ref: SecretRef, ttl_seconds: int) -> Secret:  # pragma: no cover
        raise PermissionError(
            "gcp Secret Manager does not issue leased credentials; use "
            "Workload Identity / short-lived service-account tokens"
        )

    def rotate(self, ref: SecretRef) -> None:  # pragma: no cover
        raise PermissionError(
            "gcp Secret Manager rotation is Pub/Sub-driven and outside "
            "this provider's scope; SEC-6 wires PULSE-driven rotation"
        )


def _sdk_status(exc: Exception) -> int | None:
    """Best-effort extraction of an HTTP-like status from a google-cloud
    SDK error so callers can branch on 404 / 403 / 401 uniformly."""
    # google-api-core raises ``google.api_core.exceptions.NotFound`` etc.;
    # we look for any of the conventional attributes without importing
    # the SDK (keeps it optional at module-load).
    for attr in ("code", "status_code", "grpc_status_code"):
        v = getattr(exc, attr, None)
        if isinstance(v, int):
            return v
    name = type(exc).__name__
    if name in ("NotFound", "NotFoundError"):
        return 404
    if name in ("PermissionDenied", "Unauthenticated", "Forbidden"):
        return 403
    return None


class GCPSecretManagerProvider(SecretStoreProvider):
    """Factory."""

    _log_prefix = "secret_store_provider"
    _fingerprint_fields = ("project",)
    kind: ClassVar[str] = "gcp"
    capabilities: ClassVar[Capabilities] = _GCPSecretManagerStore.capabilities

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._project: str | None = config.get("project") or None
        # Test seam: tests inject a fake client; production builds one
        # from ADC at first use.
        self._client: Any = config.get("_client")

    def _build_client(self) -> Any:
        """Lazily build the GCP SDK client. Imports SDK on first call."""
        if self._client is not None:
            return self._client
        try:
            from google.cloud import secretmanager_v1
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "gcp_secret_manager provider requires the "
                "'google-cloud-secret-manager' package; "
                "pip install axiom-os-lm[secrets-gcp]"
            ) from exc
        self._client = secretmanager_v1.SecretManagerServiceClient()
        return self._client

    def open(self) -> SecretStore:  # type: ignore[override]
        return _GCPSecretManagerStore(
            default_project=self._project,
            client=self._build_client(),
        )

    def available(self) -> bool:  # type: ignore[override]
        """True iff the SDK is importable. We don't probe the API here
        (every ``available()`` call would cost a round-trip + cloud
        quota). The first ``get()`` validates auth + reachability."""
        try:
            import importlib
            importlib.import_module("google.cloud.secretmanager_v1")
            return True
        except Exception:
            return False


__all__ = ["GCPSecretManagerProvider"]
