# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``azure_key_vault`` SecretStoreProvider — Microsoft Azure Key Vault.

Azure-native operational secret store. Authenticates via
``DefaultAzureCredential`` — the canonical Azure pattern: managed identity
in AKS / App Service / VM, ``az login`` for local dev, and a service
principal via ``AZURE_CLIENT_ID`` / ``AZURE_TENANT_ID`` /
``AZURE_CLIENT_SECRET`` env vars as the CI fallback.

``SecretRef`` shape::

    azure://<vault>/<name>              # latest version
    azure://<vault>/<name>?version=<id> # specific version id

``<vault>`` is the Key Vault *name*; the client targets
``https://<vault>.vault.azure.net`` (overridable via
``vault_url_template`` in provider config for sovereign / Government
clouds). A default vault may be set in provider config so refs can name
just ``<name>``.

Capability advertisement (per the registry's contract):
  - read / write / delete       True
  - list_paths                  True
  - versions                    True  (Key Vault versions every secret;
                                       ids are opaque hex, not integers,
                                       so they ride in ``Secret.metadata``
                                       rather than the ``version`` int)
  - encryption_at_rest          True  (Azure-managed, HSM-backed)
  - audit_stream                True  (Azure Monitor diagnostic logs)
  - dynamic_credentials         False (short-lived creds come from managed
                                       identity, not Key Vault itself)
  - rotation                    False (Key Vault rotation policies fire
                                       Event Grid → Function; we don't
                                       drive that here — SEC-6 wires
                                       PULSE-driven rotation)

The ``azure-keyvault-secrets`` + ``azure-identity`` packages import lazily
so the wheel stays light for installs that don't use this provider.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, ClassVar

from ..providers.protocol import (
    Capabilities,
    Secret,
    SecretRef,
    SecretStore,
    SecretStoreProvider,
)

_log = logging.getLogger(__name__)


class _AzureKeyVaultStore:
    """Runtime client. Wraps a per-vault Azure SecretClient."""

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

    def __init__(
        self,
        *,
        default_vault: str | None,
        client_factory: Callable[[str], Any],
    ) -> None:
        self._default_vault = default_vault
        # Key Vault clients are scoped to one vault, so we build lazily
        # per vault name rather than holding a single client.
        self._client_factory = client_factory

    # ---- helpers --------------------------------------------------------

    def _split(self, ref: SecretRef) -> tuple[str, str]:
        """``azure://<vault>/<name>`` → ``(vault, name)``.

        Falls back to the provider-configured default vault when the ref
        names only ``<name>``.
        """
        parts = ref.path.split("/", 1)
        if len(parts) == 2:
            vault, name = parts
        else:
            if not self._default_vault:
                raise ValueError(
                    f"azure SecretRef missing vault (got {ref.path!r}); "
                    "either provide vault in the URL or set vault= in "
                    "provider config"
                )
            vault, name = self._default_vault, parts[0]
        if not vault or not name:
            raise ValueError(f"azure SecretRef malformed: {ref.path!r}")
        return vault, name

    # ---- SecretStore Protocol ------------------------------------------

    def get(self, ref: SecretRef) -> Secret:
        vault, name = self._split(ref)
        client = self._client_factory(vault)
        # Key Vault versions are opaque strings; "latest" means "no version".
        version = ref.version if ref.version not in (None, "latest") else None
        try:
            kv = client.get_secret(name, version)
        except Exception as exc:  # noqa: BLE001 — translate cloud SDK errors
            code = _azure_status(exc)
            if code == 404:
                raise KeyError(
                    f"azure: no secret {name!r} in vault {vault!r}"
                ) from exc
            if code in (401, 403):
                raise PermissionError(
                    f"azure denied {name} in {vault}: {exc}"
                ) from exc
            raise RuntimeError(f"azure get_secret failed: {exc}") from exc

        raw = getattr(kv, "value", None)
        data: bytes = raw.encode("utf-8") if isinstance(raw, str) else (raw or b"")
        version_id = getattr(getattr(kv, "properties", None), "version", None)
        return Secret(
            value=data,
            metadata={
                "backend": "azure_key_vault",
                "vault": vault,
                "name": name,
                # Azure version ids are opaque hex, not integers — expose
                # the id here so callers can pin a version via ?version=.
                "version_id": version_id,
            },
            lease_id=None,
            version=None,
        )

    def put(self, ref: SecretRef, value: bytes) -> None:
        vault, name = self._split(ref)
        client = self._client_factory(vault)
        # Key Vault stores text; our transport is bytes. utf-8 is the
        # documented contract for secret payloads on this provider.
        text = value.decode("utf-8") if isinstance(value, (bytes, bytearray)) else str(value)
        try:
            client.set_secret(name, text)
        except Exception as exc:  # noqa: BLE001
            code = _azure_status(exc)
            if code in (401, 403):
                raise PermissionError(
                    f"azure denied write {name} in {vault}: {exc}"
                ) from exc
            raise RuntimeError(f"azure set_secret failed: {exc}") from exc

    def delete(self, ref: SecretRef) -> None:
        # Key Vault deletes the whole secret (soft-delete, recoverable
        # during the vault's retention window). There is no per-version
        # destroy in the data-plane API, so a version-qualified ref still
        # removes the secret — documented divergence from GCP.
        vault, name = self._split(ref)
        client = self._client_factory(vault)
        try:
            poller = client.begin_delete_secret(name)
        except Exception as exc:  # noqa: BLE001
            code = _azure_status(exc)
            if code == 404:
                raise KeyError(
                    f"azure: no secret {name!r} in vault {vault!r}"
                ) from exc
            if code in (401, 403):
                raise PermissionError(
                    f"azure denied delete {name} in {vault}: {exc}"
                ) from exc
            raise RuntimeError(f"azure begin_delete_secret failed: {exc}") from exc
        # Block until the soft-delete lands so delete() is synchronous like
        # its peers; older SDKs return a poller, newer a direct result.
        result = getattr(poller, "result", None)
        if callable(result):
            result()

    def list_paths(self, prefix: str) -> list[str]:
        parts = prefix.split("/", 1)
        vault = parts[0] or self._default_vault
        if not vault:
            raise ValueError(
                "list_paths needs a vault (either in the prefix or via the "
                "provider config's vault=...)"
            )
        name_prefix = parts[1] if len(parts) == 2 else ""
        client = self._client_factory(vault)
        try:
            props = list(client.list_properties_of_secrets())
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"azure list_properties_of_secrets failed: {exc}"
            ) from exc
        out: list[str] = []
        for p in props:
            short = getattr(p, "name", "") or ""
            if name_prefix and not short.startswith(name_prefix):
                continue
            out.append(f"{vault}/{short}")
        return sorted(out)

    def lease(self, ref: SecretRef, ttl_seconds: int) -> Secret:  # pragma: no cover
        raise PermissionError(
            "azure Key Vault does not issue leased credentials; use a "
            "managed identity for short-lived tokens"
        )

    def rotate(self, ref: SecretRef) -> None:  # pragma: no cover
        raise PermissionError(
            "azure Key Vault rotation is Event-Grid/Function-driven and "
            "outside this provider's scope; SEC-6 wires PULSE-driven rotation"
        )


def _azure_status(exc: Exception) -> int | None:
    """Best-effort extraction of an HTTP-like status from an azure-sdk
    error so callers can branch on 404 / 403 / 401 uniformly, without
    importing the SDK at module-load."""
    v = getattr(exc, "status_code", None)
    if isinstance(v, int):
        return v
    name = type(exc).__name__
    if name in ("ResourceNotFoundError", "NotFound"):
        return 404
    if name in ("ClientAuthenticationError", "Unauthenticated"):
        return 401
    return None


class AzureKeyVaultProvider(SecretStoreProvider):
    """Factory."""

    _log_prefix = "secret_store_provider"
    _fingerprint_fields = ("vault",)
    kind: ClassVar[str] = "azure"
    capabilities: ClassVar[Capabilities] = _AzureKeyVaultStore.capabilities

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._vault: str | None = config.get("vault") or None
        self._vault_url_template: str = config.get(
            "vault_url_template", "https://{vault}.vault.azure.net"
        )
        # Test seam: tests inject a fake client (one client serves every
        # vault name in-process); production builds one per vault from
        # DefaultAzureCredential at first use.
        self._client: Any = config.get("_client")
        self._credential: Any = config.get("_credential")

    def _client_factory(self, vault: str) -> Any:
        """Build (or return the injected) SecretClient for one vault."""
        if self._client is not None:
            return self._client
        try:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "azure_key_vault provider requires the "
                "'azure-keyvault-secrets' and 'azure-identity' packages; "
                "pip install axiom-os-lm[secrets-azure]"
            ) from exc
        credential = self._credential or DefaultAzureCredential()
        url = self._vault_url_template.format(vault=vault)
        return SecretClient(vault_url=url, credential=credential)

    def open(self) -> SecretStore:  # type: ignore[override]
        return _AzureKeyVaultStore(
            default_vault=self._vault,
            client_factory=self._client_factory,
        )

    def available(self) -> bool:  # type: ignore[override]
        """True iff both SDKs are importable. We don't probe the vault here
        (every ``available()`` call would cost a round-trip + AAD token);
        the first ``get()`` validates auth + reachability."""
        try:
            import importlib

            importlib.import_module("azure.keyvault.secrets")
            importlib.import_module("azure.identity")
            return True
        except Exception:
            return False


__all__ = ["AzureKeyVaultProvider"]
