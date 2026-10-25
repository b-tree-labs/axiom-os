# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``env`` SecretStoreProvider — reads from environment variables.

**Dev-only.** Plaintext at rest in the host process env; no rotation,
no audit, no encryption. The provider advertises that explicitly
(``encryption_at_rest=False``) so SEC-3 wiring code can refuse to
wire dynamic-credential paths onto this backend in production.

Emits a one-time loud warning when used outside ``AXIOM_MODE=dev``.
"""

from __future__ import annotations

import logging
import os
from typing import ClassVar

from ..providers.protocol import (
    Capabilities,
    Secret,
    SecretRef,
    SecretStore,
    SecretStoreProvider,
)

_log = logging.getLogger(__name__)
_NON_DEV_WARNED: set[str] = set()


class _EnvSecretStore:
    """Runtime client. SecretRefs look like ``env://VAR_NAME``."""

    capabilities = Capabilities(
        read=True,
        write=False,
        delete=False,
        list_paths=True,
        versions=False,
        dynamic_credentials=False,
        rotation=False,
        audit_stream=False,
        encryption_at_rest=False,
    )

    def __init__(self, *, prefix: str = "") -> None:
        self._prefix = prefix

    def _env_key(self, ref: SecretRef) -> str:
        return f"{self._prefix}{ref.path}" if self._prefix else ref.path

    def get(self, ref: SecretRef) -> Secret:
        key = self._env_key(ref)
        raw = os.environ.get(key)
        if raw is None:
            raise KeyError(f"env var {key!r} is not set")
        return Secret(
            value=raw.encode("utf-8"),
            metadata={"backend": "env", "env_key": key},
            lease_id=None,
            version=None,
        )

    def put(self, ref: SecretRef, value: bytes) -> None:  # pragma: no cover
        raise PermissionError(
            "env SecretStore is read-only; set the env var out-of-band"
        )

    def delete(self, ref: SecretRef) -> None:  # pragma: no cover
        raise PermissionError(
            "env SecretStore is read-only; unset the env var out-of-band"
        )

    def list_paths(self, prefix: str) -> list[str]:
        scoped = f"{self._prefix}{prefix}" if self._prefix else prefix
        return sorted(k for k in os.environ if k.startswith(scoped))

    def lease(self, ref: SecretRef, ttl_seconds: int) -> Secret:  # pragma: no cover
        raise PermissionError(
            "env SecretStore does not support dynamic credentials"
        )

    def rotate(self, ref: SecretRef) -> None:  # pragma: no cover
        raise PermissionError("env SecretStore does not support rotation")


class EnvSecretStoreProvider(SecretStoreProvider):
    """Factory. Use only in ``AXIOM_MODE=dev``."""

    _log_prefix = "secret_store_provider"
    _fingerprint_fields = ("prefix",)
    kind: ClassVar[str] = "env"
    capabilities: ClassVar[Capabilities] = _EnvSecretStore.capabilities

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._prefix: str = config.get("prefix", "")
        self._warn_if_non_dev()

    def _warn_if_non_dev(self) -> None:
        # Try axiom.governance.mode first (AUTHZ-5+); fall back to a
        # direct AXIOM_MODE read so this SEC-2 PR doesn't hard-depend
        # on the governance.mode module landing first.
        mode = "dev"
        try:
            from axiom.governance.mode import current_mode
            mode = current_mode()
        except Exception:
            raw = (os.environ.get("AXIOM_MODE") or "dev").strip().lower()
            if raw in ("dev", "staging", "production"):
                mode = raw
        if mode == "dev":
            return
        if self.uid in _NON_DEV_WARNED:
            return
        _NON_DEV_WARNED.add(self.uid)
        self._logger.warning(
            "env SecretStoreProvider %r constructed in AXIOM_MODE=%r — "
            "plaintext at rest, no rotation, no audit. This provider is "
            "intended for dev use only. Switch to `openbao` in staging+.",
            self.name, mode,
        )

    def open(self) -> SecretStore:  # type: ignore[override]
        return _EnvSecretStore(prefix=self._prefix)

    def available(self) -> bool:  # type: ignore[override]
        # The env is always reachable; the provider is "available" the
        # moment the process is up. Specific keys may still be missing.
        return True


__all__ = ["EnvSecretStoreProvider"]
