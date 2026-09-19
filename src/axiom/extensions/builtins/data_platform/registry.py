# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The data-platform contribution registry.

``DataPlatformRegistry`` is the *home* a consumer layer registers its
ingest sources, schema packs, and transform packs into. The orchestrator
agent reads from it; the heavy lakehouse layer (when present) reads from
it. The registry itself imports no heavy dependency — it is a typed
collection with register / get / list per contribution kind.

Each registry instance is isolated (no process-global singleton) so that
tests and multi-tenant callers do not bleed into one another.
"""

from __future__ import annotations

from .contracts import IngestSource, SchemaPack, TransformPack


class DataPlatformRegistry:
    """Collects registered ingest sources and medallion packs.

    Registration is keyed by each contribution's ``name``. Re-registering
    a name raises rather than silently overwriting, so a typo or a double
    plug-in is loud at startup.
    """

    def __init__(self) -> None:
        self._sources: dict[str, IngestSource] = {}
        self._schema_packs: dict[str, SchemaPack] = {}
        self._transform_packs: dict[str, TransformPack] = {}

    # ---- ingest sources -------------------------------------------------

    def register_source(self, source: IngestSource) -> None:
        """Register a pollable ingest source under its ``name``."""
        self._require_protocol(source, IngestSource, "IngestSource")
        self._insert(self._sources, source.name, source, "ingest source")

    def get_source(self, name: str) -> IngestSource:
        """Return the source registered under ``name`` (raises ``KeyError``)."""
        return self._sources[name]

    def list_sources(self) -> list[IngestSource]:
        """Return all registered sources in registration order."""
        return list(self._sources.values())

    # ---- schema packs ---------------------------------------------------

    def register_schema_pack(self, pack: SchemaPack) -> None:
        """Register a medallion schema pack under its ``name``."""
        self._require_protocol(pack, SchemaPack, "SchemaPack")
        self._insert(self._schema_packs, pack.name, pack, "schema pack")

    def get_schema_pack(self, name: str) -> SchemaPack:
        return self._schema_packs[name]

    def list_schema_packs(self) -> list[SchemaPack]:
        return list(self._schema_packs.values())

    # ---- transform packs ------------------------------------------------

    def register_transform_pack(self, pack: TransformPack) -> None:
        """Register a medallion transform pack under its ``name``."""
        self._require_protocol(pack, TransformPack, "TransformPack")
        self._insert(self._transform_packs, pack.name, pack, "transform pack")

    def get_transform_pack(self, name: str) -> TransformPack:
        return self._transform_packs[name]

    def list_transform_packs(self) -> list[TransformPack]:
        return list(self._transform_packs.values())

    # ---- internals ------------------------------------------------------

    @staticmethod
    def _insert(store: dict, name: str, value: object, kind: str) -> None:
        if not name:
            raise ValueError(f"{kind} must declare a non-empty name")
        if name in store:
            raise ValueError(f"{kind} {name!r} is already registered")
        store[name] = value

    @staticmethod
    def _require_protocol(obj: object, proto: type, label: str) -> None:
        if not isinstance(obj, proto):
            raise TypeError(f"object does not satisfy the {label} protocol: {obj!r}")


__all__ = ["DataPlatformRegistry"]
