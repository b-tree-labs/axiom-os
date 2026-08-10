# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``keychain`` SecretStoreProvider — macOS Keychain via the ``security`` CLI.

The custody backend for foreign credentials (issue #667): values live in
the OS keychain, never at rest in files or env exports. Refs look like
``keychain://<name>``; the item is a generic password under the
configured service (default ``axiom-secrets``) with account ``<name>``.

The ``security`` invocation is injectable (``config["runner"]``) so unit
tests never touch the real keychain; the skip-by-default integration
test uses a throwaway ``axiom-test-*`` service namespace.

Following the :mod:`axiom.setup.secrets` precedent, writes go
delete-then-add through the CLI. Values transit the ``security`` argv
for the lifetime of the subprocess (same tradeoff ``setup.secrets``
accepted); they are never logged, returned, or persisted anywhere else.

On non-darwin hosts (or when ``security`` is missing) the provider
raises :class:`KeychainUnavailable` with an actionable message — a
graceful capability error, not a silent fallback.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess
from typing import Callable, ClassVar

from .protocol import (
    Capabilities,
    Secret,
    SecretRef,
    SecretStore,
    SecretStoreProvider,
)

_log = logging.getLogger(__name__)

DEFAULT_SERVICE = "axiom-secrets"

# runner(args, *, input=None) -> (returncode, stdout, stderr).
# ``args`` excludes the leading "security" binary name.
SecurityRunner = Callable[..., tuple[int, str, str]]


class KeychainUnavailable(RuntimeError):
    """No usable OS keychain on this host — capability error, not a bug."""


def _subprocess_runner(args: list[str], *, input: str | None = None) -> tuple[int, str, str]:
    cp = subprocess.run(
        ["security", *args],
        input=input, capture_output=True, text=True, check=False,
    )
    return cp.returncode, cp.stdout, cp.stderr


class _KeychainSecretStore:
    """Runtime client. SecretRefs look like ``keychain://<name>``."""

    capabilities = Capabilities(
        read=True,
        write=True,
        delete=True,
        list_paths=False,     # enumeration comes from the metadata index
        versions=False,
        dynamic_credentials=False,
        rotation=False,
        audit_stream=False,
        encryption_at_rest=True,
    )

    def __init__(self, *, service: str, runner: SecurityRunner) -> None:
        self._service = service
        self._runner = runner

    def get(self, ref: SecretRef) -> Secret:
        rc, out, _err = self._runner(
            ["find-generic-password", "-s", self._service, "-a", ref.path, "-w"],
        )
        if rc != 0:
            raise KeyError(
                f"no keychain item for {ref.path!r} "
                f"(service={self._service!r})"
            )
        # `security ... -w` terminates the value with a newline.
        value = out[:-1] if out.endswith("\n") else out
        return Secret(
            value=value.encode("utf-8"),
            metadata={"backend": "keychain", "service": self._service},
        )

    def put(self, ref: SecretRef, value: bytes) -> None:
        # Delete-then-add: `add-generic-password -U` update semantics are
        # unreliable across macOS versions (setup.secrets precedent).
        self._runner(
            ["delete-generic-password", "-s", self._service, "-a", ref.path],
        )
        # No -T flag: the creating app (the `security` CLI) is the only
        # trusted app by default, so our own reads stay non-interactive.
        # (-T "" would deny even the security CLI and make every read pop
        # a GUI consent dialog — a hang for headless callers.)
        rc, _out, err = self._runner(
            [
                "add-generic-password",
                "-s", self._service,
                "-a", ref.path,
                "-w", value.decode("utf-8"),
            ],
        )
        if rc != 0:
            # err comes from the security CLI and never echoes -w values.
            raise RuntimeError(
                f"keychain write failed for {ref.path!r} (rc={rc}): {err.strip()}"
            )

    def delete(self, ref: SecretRef) -> None:
        rc, _out, _err = self._runner(
            ["delete-generic-password", "-s", self._service, "-a", ref.path],
        )
        if rc != 0:
            raise KeyError(
                f"no keychain item for {ref.path!r} "
                f"(service={self._service!r})"
            )

    def list_paths(self, prefix: str) -> list[str]:
        raise PermissionError(
            "keychain store does not enumerate items; secret names come "
            "from the foreign-credential metadata index"
        )

    def lease(self, ref: SecretRef, ttl_seconds: int) -> Secret:  # pragma: no cover
        raise PermissionError("keychain store does not support dynamic credentials")

    def rotate(self, ref: SecretRef) -> None:  # pragma: no cover
        raise PermissionError(
            "keychain store does not rotate; use the foreign RotationProvider "
            "flow (`axi secrets rotate <name>`)"
        )


class KeychainSecretStoreProvider(SecretStoreProvider):
    """Factory. Darwin `security` CLI; injectable runner for tests."""

    _log_prefix = "secret_store_provider"
    _fingerprint_fields = ("service",)
    kind: ClassVar[str] = "keychain"
    capabilities: ClassVar[Capabilities] = _KeychainSecretStore.capabilities

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._service: str = config.get("service") or DEFAULT_SERVICE
        self._runner: SecurityRunner | None = config.get("runner")

    def open(self) -> SecretStore:  # type: ignore[override]
        if self._runner is not None:
            return _KeychainSecretStore(service=self._service, runner=self._runner)
        if not self.available():
            raise KeychainUnavailable(
                "no usable OS keychain on this host "
                f"(platform={platform.system()}): the `keychain` secret "
                "store needs macOS with the `security` CLI. Set "
                "AXIOM_FOREIGN_SECRETS_BACKEND=file for the dev/CI "
                "file backend, or run on a darwin host."
            )
        return _KeychainSecretStore(
            service=self._service, runner=_subprocess_runner,
        )

    def available(self) -> bool:  # type: ignore[override]
        if self._runner is not None:
            return True
        return (
            platform.system() == "Darwin"
            and shutil.which("security") is not None
        )


__all__ = ["KeychainSecretStoreProvider", "KeychainUnavailable", "DEFAULT_SERVICE"]
