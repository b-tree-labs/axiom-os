# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Server-authoritative flag resolution (ported from SoilMetrix's FeatureFlagService).

Resolution, later wins (ADR-210):

1. the registry default for this environment (``default_by_env``, else ``default``)
2. ``AXIOM_FLAG_<KEY>``
3. the variant's flag set (``[variant.<name>.flags]``, MVP-12-A1)
4. the node override
5. the account override
6. the user override

A flag ``protected_in`` the current environment ignores 4-6 there: a
deployment's own configuration (its environment, its variant) still applies, a
stored override does not. A flag nobody declared resolves to ``off``.

Each isolated variant keeps its own stored overrides (MVP-12-A4): an override
set in one is written under that variant's namespace and never read by
another. The variant that takes an install over in place reads and writes the
install's own overrides, so nothing set before it is lost.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .db_models import SCOPES
from .registry import ENV_PREFIX, OFF, ON, FlagDef, get_environment, normalize_value
from .registry import registry as _registry


class OverrideReader(Protocol):
    def overrides_for(self, scope: str, scope_id: str | None) -> dict[str, str]: ...
    def put(self, scope: str, scope_id: str, key: str, value: str, *, by: str) -> dict[str, Any]: ...
    def delete(self, scope: str, scope_id: str, key: str) -> bool: ...


class FlagError(ValueError):
    """A set the service refuses. ``code`` is what an HTTP face maps to a status."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code  # "unknown" | "protected" | "invalid"


@dataclass(frozen=True)
class Resolution:
    environment: str
    flags: dict[str, str]
    sources: dict[str, str]


def env_override(key: str, environ: Mapping[str, str] | None = None) -> str | None:
    env = os.environ if environ is None else environ
    return normalize_value(env.get(f"{ENV_PREFIX}{key.upper()}"))


class FlagService:
    """Resolve flags from the registry, the environment and stored overrides."""

    ENV_PREFIX = ENV_PREFIX

    def __init__(
        self,
        overrides: OverrideReader | None = None,
        *,
        registry: Mapping[str, FlagDef] | None = None,
        variant: Any = "active",
    ) -> None:
        if overrides is None:
            from .store import OverrideStore

            overrides = OverrideStore()
        self.overrides = overrides
        self._registry = registry
        #: ``"active"`` reads ``AXIOM_VARIANT`` per call; ``None`` is a plain
        #: install; a :class:`axiom.infra.variants.Variant` pins one.
        self._variant = variant

    def variant(self):
        if self._variant == "active":
            from axiom.infra.variants import active_variant

            return active_variant()
        return self._variant

    def _sid(self, scope_id: str | None) -> str | None:
        """The stored scope id: namespaced by an isolated variant."""
        v = self.variant()
        ns = v.override_namespace if v is not None else None
        if not scope_id or not ns:
            return scope_id
        return f"variant:{ns}/{scope_id}"

    def _layers(self, node: str | None, account: str | None, user: str | None) -> dict:
        return {
            "node": self.overrides.overrides_for("node", self._sid(node)),
            "account": self.overrides.overrides_for("account", self._sid(account)),
            "user": self.overrides.overrides_for("user", self._sid(user)),
        }

    def _variant_flags(self) -> Mapping[str, str]:
        v = self.variant()
        return dict(v.flags) if v is not None else {}

    def get_registry(self) -> Mapping[str, FlagDef]:
        return self._registry if self._registry is not None else _registry()

    # ------------------------------------------------------------ resolve

    def resolve(
        self, *, node: str | None = None, account: str | None = None, user: str | None = None
    ) -> Resolution:
        """Every declared flag's value for this caller, and where each value came from."""
        env = get_environment()
        layers = self._layers(node, account, user)
        layered = self._variant_flags()
        flags: dict[str, str] = {}
        sources: dict[str, str] = {}
        for key, flag in sorted(self.get_registry().items()):
            flags[key], sources[key] = self._resolve_one(flag, env, layers, layered)
        return Resolution(environment=env, flags=flags, sources=sources)

    def resolve_flag(
        self,
        key: str,
        *,
        node: str | None = None,
        account: str | None = None,
        user: str | None = None,
    ) -> str:
        """One flag's value without resolving the rest; ``off`` when undeclared."""
        flag = self.get_registry().get(key)
        if flag is None:
            return OFF
        env = get_environment()
        layers = {}
        if not flag.is_protected(env):
            layers = self._layers(node, account, user)
        return self._resolve_one(flag, env, layers, self._variant_flags())[0]

    def is_enabled(self, key: str, **who: str | None) -> bool:
        return self.resolve_flag(key, **who) == ON

    @staticmethod
    def _resolve_one(
        flag: FlagDef,
        env: str,
        layers: Mapping[str, Mapping[str, str]],
        variant_flags: Mapping[str, str] | None = None,
    ) -> tuple[str, str]:
        value, source = flag.default_for(env), "default"
        from_env = env_override(flag.key)
        if from_env is not None:
            value, source = from_env, "env"
        from_variant = normalize_value((variant_flags or {}).get(flag.key))
        if from_variant is not None:
            value, source = from_variant, "variant"
        if not flag.is_protected(env):
            for scope in SCOPES:
                stored = normalize_value((layers.get(scope) or {}).get(flag.key))
                if stored is not None:
                    value, source = stored, scope
        return value, source

    def get_flag_metadata(self) -> dict[str, dict[str, Any]]:
        """What a settings panel shows beside each flag."""
        env = get_environment()
        return {
            key: {
                "description": flag.description,
                "default": flag.default_for(env),
                "protected": flag.is_protected(env),
                "owner": flag.owner,
            }
            for key, flag in sorted(self.get_registry().items())
        }

    # ------------------------------------------------------------ write

    def set_override(
        self, scope: str, scope_id: str, key: str, value: Any, *, by: str
    ) -> dict[str, Any] | None:
        """Store (or, with ``value=None``, clear) one override.

        Refuses an undeclared flag, a value that is not on/off/soon, and a flag
        protected in this environment (SoilMetrix's 403).
        """
        flag = self.get_registry().get(key)
        if flag is None:
            raise FlagError(f"no flag {key!r} is declared", code="unknown")
        if scope not in SCOPES:
            raise FlagError(f"scope is one of {', '.join(SCOPES)}", code="invalid")
        if not scope_id:
            raise FlagError(f"name the {scope}: scope_id is required", code="invalid")
        env = get_environment()
        if flag.is_protected(env):
            raise FlagError(f"flag {key!r} is protected in the {env} environment", code="protected")
        stored_id = self._sid(scope_id) or scope_id
        if value is None:
            self.overrides.delete(scope, stored_id, key)
            return None
        norm = normalize_value(value)
        if norm is None:
            raise FlagError(f"{value!r} is not on, off or soon", code="invalid")
        return self.overrides.put(scope, stored_id, key, norm, by=by)


__all__ = ["FlagError", "FlagService", "OverrideReader", "Resolution", "env_override"]
