# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``file`` SecretStoreProvider — 0600 JSON file. Dev/CI fallback ONLY.

Exists so the foreign-credential surface (issue #667) still works on
hosts without an OS keychain (Linux CI runners, containers). Values are
base64-encoded (obfuscation against casual grep, NOT encryption) in a
single 0600 file. The provider advertises ``encryption_at_rest=False``
and mirrors the ``env`` provider's loud warning outside
``AXIOM_MODE=dev`` — production custody belongs to the ``keychain``
provider (or a real backend like ``openbao``).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import ClassVar

from .protocol import (
    Capabilities,
    Secret,
    SecretRef,
    SecretStore,
    SecretStoreProvider,
)

_log = logging.getLogger(__name__)
_NON_DEV_WARNED: set[str] = set()


class _FileSecretStore:
    """Runtime client. SecretRefs look like ``file://<name>``."""

    capabilities = Capabilities(
        read=True,
        write=True,
        delete=True,
        list_paths=True,
        versions=False,
        dynamic_credentials=False,
        rotation=False,
        audit_stream=False,
        encryption_at_rest=False,
    )

    def __init__(self, *, path: Path) -> None:
        self._path = path

    def _load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Write then chmod: never leave a window with default perms on
        # first creation — create restrictively via os.open.
        fd = os.open(
            self._path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
        )
        try:
            os.write(fd, json.dumps(data, indent=2).encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(self._path, 0o600)

    def get(self, ref: SecretRef) -> Secret:
        data = self._load()
        if ref.path not in data:
            raise KeyError(f"no file-store secret named {ref.path!r}")
        return Secret(
            value=base64.b64decode(data[ref.path]),
            metadata={"backend": "file", "path": str(self._path)},
        )

    def put(self, ref: SecretRef, value: bytes) -> None:
        data = self._load()
        data[ref.path] = base64.b64encode(value).decode("ascii")
        self._save(data)

    def delete(self, ref: SecretRef) -> None:
        data = self._load()
        if ref.path not in data:
            raise KeyError(f"no file-store secret named {ref.path!r}")
        del data[ref.path]
        self._save(data)

    def list_paths(self, prefix: str) -> list[str]:
        return sorted(k for k in self._load() if k.startswith(prefix))

    def lease(self, ref: SecretRef, ttl_seconds: int) -> Secret:  # pragma: no cover
        raise PermissionError("file store does not support dynamic credentials")

    def rotate(self, ref: SecretRef) -> None:  # pragma: no cover
        raise PermissionError(
            "file store does not rotate; use the foreign RotationProvider flow"
        )


class FileSecretStoreProvider(SecretStoreProvider):
    """Factory. Use only in ``AXIOM_MODE=dev`` (and CI)."""

    _log_prefix = "secret_store_provider"
    _fingerprint_fields = ("path",)
    kind: ClassVar[str] = "file"
    capabilities: ClassVar[Capabilities] = _FileSecretStore.capabilities

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._raw_path: str = str(config.get("path") or "")
        self._path = Path(self._raw_path).expanduser() if self._raw_path else None
        self._warn_if_non_dev()

    def _warn_if_non_dev(self) -> None:
        mode = "dev"
        try:
            from axiom.governance.mode import current_mode
            mode = current_mode()
        except Exception:
            raw = (os.environ.get("AXIOM_MODE") or "dev").strip().lower()
            if raw in ("dev", "staging", "production"):
                mode = raw
        if mode == "dev" or self.uid in _NON_DEV_WARNED:
            return
        _NON_DEV_WARNED.add(self.uid)
        self._logger.warning(
            "file SecretStoreProvider %r constructed in AXIOM_MODE=%r — "
            "plaintext-equivalent at rest (base64, 0600 file), no rotation, "
            "no audit. Dev/CI fallback only; use `keychain` or `openbao`.",
            self.name, mode,
        )

    def open(self) -> SecretStore:  # type: ignore[override]
        if self._path is None:
            raise ValueError("file SecretStoreProvider needs a 'path' config key")
        return _FileSecretStore(path=self._path)

    def available(self) -> bool:  # type: ignore[override]
        return self._path is not None


__all__ = ["FileSecretStoreProvider"]
