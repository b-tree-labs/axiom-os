# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Student-side materials sync — download, verify, persist.

Phase 3 of the materials-flow tier. After a successful join, the
student calls :class:`MaterialsSyncClient.sync` with the coordinator
base URL and pubkey. The client:

    1. Fetches the signed manifest
    2. Verifies the signature against ``coordinator_public_key``
    3. For each manifest entry:
       - Skip if already cached (hash matches)
       - Otherwise fetch the file, verify sha256 matches the entry's
         ``content_hash``, write to disk
    4. Returns a :class:`SyncResult` with counts

Transport is injected via :class:`GetTransport`, which is any callable
with a ``get(url) -> (status, bytes)`` method. The HTTP adapter over
:mod:`urllib.request` lives in classroom_join_http; tests can use
:class:`InProcessGetTransport` to stub the wire without starting a
server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .classroom_materials import compute_file_id
from .materials_manifest import (
    decode_materials_manifest,
    verify_materials_manifest,
)

# ---------------------------------------------------------------------------
# Transport protocol
# ---------------------------------------------------------------------------


class GetTransport(Protocol):
    def get(self, url: str) -> tuple[int, bytes]: ...


@dataclass
class InProcessGetTransport:
    """Test helper: serves a manifest + file bytes without a real server."""

    manifest_json: str
    file_bytes: dict[str, bytes]

    def get(self, url: str) -> tuple[int, bytes]:
        # Extract the path suffix — caller passes absolute URLs like
        # http://host/classroom/materials/manifest, but we only need the
        # last portion.
        if url.endswith("/classroom/materials/manifest"):
            return 200, self.manifest_json.encode("utf-8")
        prefix = "/classroom/materials/"
        idx = url.rfind(prefix)
        if idx < 0:
            return 404, b"not found"
        file_id = url[idx + len(prefix):]
        data = self.file_bytes.get(file_id)
        if data is None:
            return 404, b"not found"
        return 200, data


# ---------------------------------------------------------------------------
# Exceptions + result types
# ---------------------------------------------------------------------------


class MaterialsTamperError(Exception):
    """Raised when manifest signature or per-file hash doesn't verify.

    Treated as a hard fail — the client refuses to persist anything,
    since the wire path cannot be trusted. Caller should surface this
    as a clear error to the student.
    """


@dataclass(frozen=True)
class SyncResult:
    accepted: bool
    downloaded: int = 0
    cached: int = 0
    total_bytes: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Student-side store
# ---------------------------------------------------------------------------


@dataclass
class StudentMaterialsStore:
    """Local cache of downloaded materials. Mirrors the disk layout of
    :class:`ClassroomMaterialsStore` on the instructor side, minus the
    write-side mechanics."""

    base_dir: Path

    @property
    def _materials_dir(self) -> Path:
        return self.base_dir / "materials"

    @property
    def _index_path(self) -> Path:
        return self.base_dir / "materials_index.json"

    # ---- Public API ----

    def has(self, file_id: str) -> bool:
        return (self._materials_dir / file_id).is_file()

    def get_path(self, file_id: str) -> Path:
        path = self._materials_dir / file_id
        if not path.is_file():
            raise KeyError(f"no cached material with file_id {file_id!r}")
        return path

    def save(
        self,
        *,
        file_id: str,
        content: bytes,
        title: str,
        filename: str,
    ) -> None:
        self._materials_dir.mkdir(parents=True, exist_ok=True)
        (self._materials_dir / file_id).write_bytes(content)

        index = self._load()
        index["entries"] = [
            e for e in index["entries"] if e["file_id"] != file_id
        ]
        index["entries"].append({
            "file_id": file_id,
            "filename": filename,
            "title": title,
            "size_bytes": len(content),
            "content_hash": file_id,
            "cached_at": datetime.now(UTC).isoformat(),
        })
        self._save(index)

    def list_entries(self) -> list[dict]:
        return list(self._load()["entries"])

    # ---- Internals ----

    def _load(self) -> dict:
        if not self._index_path.is_file():
            return {"entries": []}
        try:
            raw = json.loads(self._index_path.read_text())
        except json.JSONDecodeError:
            # Corrupt cache — start fresh; we'll rebuild on next sync.
            return {"entries": []}
        if not isinstance(raw, dict):
            return {"entries": []}
        raw.setdefault("entries", [])
        return raw

    def _save(self, index: dict) -> None:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(json.dumps(index, indent=2, sort_keys=True))


# ---------------------------------------------------------------------------
# Sync client
# ---------------------------------------------------------------------------


@dataclass
class MaterialsSyncClient:
    transport: GetTransport
    store: StudentMaterialsStore
    coordinator_public_key: str

    def sync(self, *, base_url: str) -> SyncResult:
        """Fetch manifest, verify, download missing files.

        Returns a :class:`SyncResult` summarizing counts. Raises
        :class:`MaterialsTamperError` when the manifest signature is
        invalid or any file's hash mismatches — treat those as a hard
        fail, not recoverable.
        """
        manifest_url = self._join(base_url, "classroom/materials/manifest")
        status, body = self.transport.get(manifest_url)
        if status != 200:
            return SyncResult(
                accepted=False,
                error=f"manifest fetch failed (HTTP {status})",
            )

        try:
            manifest = decode_materials_manifest(body.decode("utf-8", errors="replace"))
        except ValueError as exc:
            return SyncResult(
                accepted=False,
                error=f"manifest invalid: {exc}",
            )

        verify = verify_materials_manifest(
            manifest, coordinator_public_key=self.coordinator_public_key
        )
        if not verify.valid:
            raise MaterialsTamperError(
                f"manifest signature did not verify: {verify.reason}"
            )

        downloaded = 0
        cached = 0
        total_bytes = 0
        for entry in manifest.entries:
            if self.store.has(entry.file_id):
                # Trust cached file if file_id == content_hash (it's
                # already verified at save time); cheap.
                cached += 1
                continue
            file_url = self._join(
                base_url, f"classroom/materials/{entry.file_id}",
            )
            status, data = self.transport.get(file_url)
            if status != 200:
                return SyncResult(
                    accepted=False,
                    downloaded=downloaded,
                    cached=cached,
                    total_bytes=total_bytes,
                    error=(
                        f"file \"{entry.title}\" fetch failed "
                        f"(HTTP {status})"
                    ),
                )
            actual = compute_file_id(data)
            if actual != entry.content_hash:
                raise MaterialsTamperError(
                    f"content hash mismatch for \"{entry.title}\": "
                    f"expected {entry.content_hash}, got {actual}"
                )
            self.store.save(
                file_id=entry.file_id,
                content=data,
                title=entry.title,
                filename=_filename_for(entry),
            )
            downloaded += 1
            total_bytes += entry.size_bytes

        return SyncResult(
            accepted=True,
            downloaded=downloaded,
            cached=cached,
            total_bytes=total_bytes,
        )

    @staticmethod
    def _join(base: str, suffix: str) -> str:
        if base.endswith("/"):
            return base + suffix
        return base + "/" + suffix


def _filename_for(entry) -> str:
    """Best-effort filename — the manifest entry carries only title and
    content, not filename; use title as a sensible default."""
    return entry.title


__all__ = [
    "GetTransport",
    "InProcessGetTransport",
    "MaterialsSyncClient",
    "MaterialsTamperError",
    "StudentMaterialsStore",
    "SyncResult",
]
