# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The personal credential fabric (CRED-1) — `axi cred`, the `axi mem` analogue.

Put any credential for any system; get it (or have an agent use it) anytime —
with the local principal as the lock. Release is gated by a per-credential
**posture floor** (ENF-4) and optional **require_mfa** fresh tap (IDENT-9).
``list`` shows names + floors, never values. The custody backend is pluggable
(in-memory for tests; OS keychain in prod; Badge later).
"""

from __future__ import annotations

import json
from typing import Optional, Protocol, runtime_checkable

from axiom.infra.principal import PrincipalContext

_PREFIX = "axiom.cred."
_INDEX = "axiom.cred.__index__"


class PostureError(Exception):
    """Principal below a credential's posture floor."""


class MfaRequired(Exception):
    """A fresh second factor is required to release this credential."""


@runtime_checkable
class CredBackend(Protocol):
    def get(self, key: str) -> Optional[str]: ...

    def put(self, key: str, value: str) -> None: ...

    def delete(self, key: str) -> None: ...


class InMemoryCredBackend:
    def __init__(self) -> None:
        self._d: dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self._d.get(key)

    def put(self, key: str, value: str) -> None:
        self._d[key] = value

    def delete(self, key: str) -> None:
        self._d.pop(key, None)


class KeychainCredBackend:
    """OS-keychain custody (via setup.secrets)."""

    def get(self, key: str) -> Optional[str]:
        from axiom.setup.secrets import get_secret

        return get_secret(key)

    def put(self, key: str, value: str) -> None:
        from axiom.setup.secrets import store_secret

        store_secret(key, value)

    def delete(self, key: str) -> None:
        self.put(key, "")  # best-effort; real revocation is FED-2


class CredStore:
    def __init__(self, backend: Optional[CredBackend] = None) -> None:
        self._b = backend or KeychainCredBackend()

    def _index(self) -> set:
        raw = self._b.get(_INDEX)
        return set(json.loads(raw)) if raw else set()

    def _save_index(self, names: set) -> None:
        self._b.put(_INDEX, json.dumps(sorted(names)))

    def _record(self, name: str) -> Optional[dict]:
        raw = self._b.get(_PREFIX + name)
        return json.loads(raw) if raw else None

    def put(self, name: str, value: str, *, min_posture: str = "open", require_mfa: bool = False) -> None:
        self._b.put(_PREFIX + name, json.dumps(
            {"value": value, "min_posture": min_posture, "require_mfa": require_mfa}))
        idx = self._index()
        idx.add(name)
        self._save_index(idx)

    def list(self) -> list:
        """Names + their floors — never values."""
        out = []
        for name in sorted(self._index()):
            rec = self._record(name) or {}
            out.append({"name": name, "min_posture": rec.get("min_posture", "open"),
                        "require_mfa": rec.get("require_mfa", False)})
        return out

    def get(self, name: str, *, principal: PrincipalContext, mfa_confirm=None, step_up=None) -> str:
        """Release a credential — gated by its posture floor + require_mfa.

        ``step_up`` (``(target_posture) -> PrincipalContext``) is the canonical
        step-up moment (ENF-3): the **first dereference of a real credential**
        elevates the principal rather than failing outright.
        """
        rec = self._record(name)
        if rec is None:
            raise KeyError(name)
        floor = rec.get("min_posture", "open")
        if not principal.meets(floor):
            if step_up is not None:
                principal = step_up(floor)      # elevate at the moment of consequence
            if not principal.meets(floor):
                raise PostureError(
                    f"'{name}' needs posture '{floor}'; principal '{principal.handle}' "
                    f"is '{principal.posture}' — step up first")
        if rec.get("require_mfa"):
            if mfa_confirm is None or not mfa_confirm():
                raise MfaRequired(f"'{name}' requires a fresh second factor")
        return rec["value"]

    def rm(self, name: str) -> bool:
        if name not in self._index():
            return False
        self._b.delete(_PREFIX + name)
        idx = self._index()
        idx.discard(name)
        self._save_index(idx)
        return True


__all__ = ["CredStore", "InMemoryCredBackend", "KeychainCredBackend", "MfaRequired", "PostureError"]
