# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``mock_registry`` fixture — fake Vyzier registry for extension tests.

Serves manifest and signed-artifact responses so that extension install /
search / show flows can run without contacting a real registry.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class RegistryEntry:
    """A single registry listing."""

    name: str
    version: str
    publisher: str
    manifest: dict[str, Any]
    signature: str
    download_url: str
    conformance_level: str = "bronze"

    @property
    def pinned_name(self) -> str:
        return f"{self.name}@{self.version}"


class MockRegistry:
    """Fake Vyzier registry returning deterministic responses."""

    def __init__(self, *, base_url: str = "https://registry.test.example.org") -> None:
        self.base_url = base_url
        self._entries: dict[str, RegistryEntry] = {}
        self.requests: list[tuple[str, str]] = []

    # ---- Authoring -------------------------------------------------------

    def add(
        self,
        *,
        name: str,
        version: str,
        publisher: str,
        manifest: dict[str, Any] | None = None,
        conformance_level: str = "bronze",
    ) -> RegistryEntry:
        mf = manifest or self._default_manifest(name, version, publisher)
        sig = hashlib.sha256(f"{name}:{version}:{publisher}".encode()).hexdigest()
        entry = RegistryEntry(
            name=name,
            version=version,
            publisher=publisher,
            manifest=mf,
            signature=sig,
            download_url=f"{self.base_url}/dist/{name}/{version}/{name}-{version}.whl",
            conformance_level=conformance_level,
        )
        self._entries[entry.pinned_name] = entry
        return entry

    def _default_manifest(self, name: str, version: str, publisher: str) -> dict[str, Any]:
        return {
            "extension": {
                "name": name,
                "version": version,
                "description": f"Test extension {name}",
                "license": "Apache-2.0",
                "aeos_version": "0.1.0",
                "owner": publisher,
                "provides": [
                    {
                        "kind": "tool",
                        "name": f"{name}_tool",
                        "entry": f"{name}.tools.default:DefaultTool",
                        "description": "A default placeholder tool",
                    }
                ],
            }
        }

    # ---- Queries ---------------------------------------------------------

    def search(self, query: str) -> list[RegistryEntry]:
        self.requests.append(("search", query))
        q = query.lower()
        return [e for e in self._entries.values() if q in e.name.lower()]

    def show(self, name: str, version: str | None = None) -> RegistryEntry | None:
        self.requests.append(("show", f"{name}@{version or 'latest'}"))
        if version:
            return self._entries.get(f"{name}@{version}")
        # Latest by semver-ish lexical sort; tests can pin versions explicitly.
        candidates = [e for e in self._entries.values() if e.name == name]
        if not candidates:
            return None
        candidates.sort(key=lambda e: e.version)
        return candidates[-1]

    def list_entries(self) -> list[RegistryEntry]:
        return list(self._entries.values())

    def reset(self) -> None:
        self._entries.clear()
        self.requests.clear()


@pytest.fixture
def mock_registry() -> MockRegistry:
    """Provide a fresh ``MockRegistry`` for each test."""
    return MockRegistry()


__all__ = ["MockRegistry", "RegistryEntry", "mock_registry"]
