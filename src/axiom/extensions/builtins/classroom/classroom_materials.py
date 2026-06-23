# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Instructor-side classroom materials store.

Phase 1 of the materials-flow tier. Persists uploaded course files to
disk so the coordinator can serve them to joining students (Phase 2+).
Files are content-addressed (sha256) so identical uploads deduplicate,
and the metadata index is a single JSON file for easy inspection.

Disk layout under ``<base_dir>``::

    <base_dir>/
        materials_index.json            ← list of MaterialEntry
        materials/
            <file_id>                   ← raw bytes, one file per unique content

``<base_dir>`` is typically ``~/.axi/coordinator/classrooms/<classroom_id>``
so each classroom owns its materials independently; re-using the same
course across multiple classrooms re-uploads (deliberate: keeps the
mental model "this class's materials" simple for instructors).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Content addressing
# ---------------------------------------------------------------------------


def compute_file_id(content: bytes) -> str:
    """URL-safe base64 of the sha256 of ``content``.

    Shorter than hex, still collision-safe for any practical corpus,
    usable unescaped in a URL path.
    """
    digest = hashlib.sha256(content).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MaterialEntry:
    """One item in the classroom's materials list."""

    file_id: str       # content hash (url-safe base64 sha256)
    filename: str      # original filename, instructor's name for the file
    title: str         # display title (defaults to filename)
    size_bytes: int
    content_hash: str  # same as file_id today, kept separate so we can
                       # swap hash functions later without breaking URLs
    added_at: str      # ISO 8601 with timezone


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


@dataclass
class ClassroomMaterialsStore:
    base_dir: Path

    # ---- Path helpers ----

    @property
    def _materials_dir(self) -> Path:
        return self.base_dir / "materials"

    @property
    def _index_path(self) -> Path:
        return self.base_dir / "materials_index.json"

    # ---- Public API ----

    def add_file(
        self,
        source_path: Path,
        *,
        title: str | None = None,
    ) -> MaterialEntry:
        """Persist ``source_path`` into the store and return its entry."""
        source_path = Path(source_path)
        if not source_path.is_file():
            raise FileNotFoundError(f"not a readable file: {source_path}")
        content = source_path.read_bytes()
        return self._add_bytes(
            content,
            filename=source_path.name,
            title=title,
        )

    def add_text(
        self,
        content: str,
        *,
        filename: str,
        title: str | None = None,
    ) -> MaterialEntry:
        return self._add_bytes(
            content.encode("utf-8"),
            filename=filename,
            title=title,
        )

    def add_bytes(
        self,
        content: bytes,
        *,
        filename: str,
        title: str | None = None,
    ) -> MaterialEntry:
        """Persist raw bytes into the store under ``filename``.

        Public counterpart of ``_add_bytes`` so external ingest paths
        (Canvas pull, etc.) don't have to reach into the private API.
        """
        return self._add_bytes(content, filename=filename, title=title)

    def list_entries(self) -> list[MaterialEntry]:
        return [_entry_from_dict(e) for e in self._load()["entries"]]

    def get_path(self, file_id: str) -> Path:
        """Filesystem path of the stored content. Raises KeyError if unknown."""
        entries = self._load()["entries"]
        if not any(e["file_id"] == file_id for e in entries):
            raise KeyError(f"no material with file_id {file_id!r}")
        return self._materials_dir / file_id

    def remove(self, file_id: str) -> None:
        """Forget this material. No-op if unknown."""
        index = self._load()
        index["entries"] = [e for e in index["entries"] if e["file_id"] != file_id]
        self._save(index)
        content_path = self._materials_dir / file_id
        if content_path.exists():
            content_path.unlink()

    # ---- Internals ----

    def _add_bytes(
        self,
        content: bytes,
        *,
        filename: str,
        title: str | None,
    ) -> MaterialEntry:
        file_id = compute_file_id(content)
        self._materials_dir.mkdir(parents=True, exist_ok=True)
        content_path = self._materials_dir / file_id
        if not content_path.exists():
            tmp = content_path.with_suffix(content_path.suffix + ".tmp")
            tmp.write_bytes(content)
            os.replace(tmp, content_path)

        entry = MaterialEntry(
            file_id=file_id,
            filename=filename,
            title=title or filename,
            size_bytes=len(content),
            content_hash=file_id,
            added_at=datetime.now(UTC).isoformat(),
        )

        index = self._load()
        # Last-writer wins on metadata for the same content hash; content
        # itself is immutable on disk under its hash.
        index["entries"] = [
            e for e in index["entries"] if e["file_id"] != file_id
        ]
        index["entries"].append(asdict(entry))
        self._save(index)
        return entry

    def _load(self) -> dict:
        if not self._index_path.is_file():
            return {"entries": []}
        try:
            raw = json.loads(self._index_path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"materials index at {self._index_path} is corrupt: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise ValueError(
                f"materials index at {self._index_path} is corrupt: "
                "not a JSON object"
            )
        raw.setdefault("entries", [])
        return raw

    def _save(self, index: dict) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            dir=self.base_dir,
            prefix=self._index_path.name + ".",
            suffix=".tmp",
            delete=False,
        ) as tf:
            json.dump(index, tf, indent=2, sort_keys=True)
            tmp_path = Path(tf.name)
        os.replace(tmp_path, self._index_path)


def _entry_from_dict(raw: dict) -> MaterialEntry:
    return MaterialEntry(
        file_id=str(raw["file_id"]),
        filename=str(raw["filename"]),
        title=str(raw["title"]),
        size_bytes=int(raw["size_bytes"]),
        content_hash=str(raw.get("content_hash") or raw["file_id"]),
        added_at=str(raw["added_at"]),
    )


__all__ = [
    "ClassroomMaterialsStore",
    "MaterialEntry",
    "compute_file_id",
]
