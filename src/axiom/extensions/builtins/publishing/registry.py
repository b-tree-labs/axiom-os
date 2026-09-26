# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Link registry — maps documents to their published URLs.

Persists to .neut/publisher/publisher-registry.json. The registry builds
a link_map for GenerationProvider.rewrite_links() to use when
rewriting cross-document references.

Uses LockedJsonFile for safe concurrent access across agents.
"""

from __future__ import annotations

from pathlib import Path

from axiom.infra.state import LockedJsonFile

from .state import LinkEntry


class LinkRegistry:
    """Manages the document → published URL mapping."""

    def __init__(self, registry_path: Path):
        self.path = registry_path
        self.entries: dict[str, LinkEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        data: dict = {}
        try:
            with LockedJsonFile(self.path) as f:
                data = f.read()
            for item in data.get("documents", []):
                entry = LinkEntry.from_dict(item)
                self.entries[entry.source_path] = entry
        except (KeyError, OSError):
            pass

    def save(self) -> None:
        """Persist registry to disk with exclusive lock and atomic write."""
        data = {
            "documents": [e.to_dict() for e in self.entries.values()],
        }
        with LockedJsonFile(self.path, exclusive=True) as f:
            f.write(data)

    def update(self, entry: LinkEntry) -> None:
        """Add or update a registry entry."""
        self.entries[entry.source_path] = entry
        self.save()

    def get(self, source_path: str) -> LinkEntry | None:
        """Look up a document by its source path."""
        return self.entries.get(source_path)

    def get_by_doc_id(self, doc_id: str) -> LinkEntry | None:
        """Look up a document by its doc_id."""
        for entry in self.entries.values():
            if entry.doc_id == doc_id:
                return entry
        return None

    def remove(self, source_path: str) -> bool:
        """Remove a document from the registry."""
        if source_path in self.entries:
            del self.entries[source_path]
            self.save()
            return True
        return False

    def build_link_map(self) -> dict[str, str]:
        """Build a mapping of relative .md paths → published URLs.

        This is passed to GenerationProvider.rewrite_links() for
        format-specific link rewriting.
        """
        link_map: dict[str, str] = {}
        for entry in self.entries.values():
            if entry.published_url:
                # Map the source path to its published URL
                link_map[entry.source_path] = entry.published_url
                # Also map just the filename for relative references
                filename = Path(entry.source_path).name
                link_map[filename] = entry.published_url
                # And the stem (without extension)
                stem = Path(entry.source_path).stem
                link_map[f"{stem}.md"] = entry.published_url
        return link_map

    def check_links(self, docs_root: Path) -> dict[str, list[str]]:
        """Verify that all registered source files exist on disk.

        Returns:
            Dict with 'missing' (registered but no source file),
            'orphaned' (source exists but not registered),
            'valid' (both exist).
        """
        result: dict[str, list[str]] = {
            "valid": [],
            "missing": [],
            "stale": [],
        }

        for source_path, _entry in self.entries.items():
            full_path = docs_root.parent / source_path if not Path(source_path).is_absolute() else Path(source_path)
            if full_path.exists():
                result["valid"].append(source_path)
            else:
                result["missing"].append(source_path)

        return result

    @property
    def count(self) -> int:
        return len(self.entries)
