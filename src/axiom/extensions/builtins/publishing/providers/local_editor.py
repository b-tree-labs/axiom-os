# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``LocalFileEditor`` — a versioned document editor backed by a local file.

The second document-editor kind (ADR-110 §Decision-5 / P4), and the portability
proof: a brand-new, non-Microsoft backend is one ``register_editor`` call, and
the mirror engine works against it unchanged — same ``RemoteEditorEndpoint``
contract, same optimistic-concurrency writes (``write`` refuses a stale
``expected_version`` with ``VersionConflict``). Useful in its own right: mirror a
document to a shared-drive path, or exercise the mirror with no cloud.

The "remote" is ``path``; a sidecar ``<path>.mirrormeta.json`` holds the version
counter and a bounded history (so ``versions()`` and stale-flush detection work).
"""

from __future__ import annotations

import json
from pathlib import Path

from ..mirror import RemoteDoc, VersionConflict

_HISTORY_CAP = 20


class LocalFileEditor:
    """A ``RemoteEditorEndpoint`` over a local file with a version sidecar."""

    def __init__(self, *, path: str, author_app: str = "local") -> None:
        self._path = Path(path)
        self._meta = self._path.with_suffix(self._path.suffix + ".mirrormeta.json")
        self._author = author_app

    def _load_meta(self) -> dict:
        if self._meta.exists():
            try:
                return json.loads(self._meta.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"version": "0", "history": []}

    def _current_version(self) -> str:
        if not self._path.exists():
            return "0"
        return self._load_meta().get("version", "0")

    def read(self) -> RemoteDoc:
        text = self._path.read_bytes().decode("utf-8") if self._path.exists() else ""
        meta = self._load_meta()
        return RemoteDoc(text=text, version=meta.get("version", "0"),
                         author_app=self._author)

    def write(self, text: str, expected_version: str) -> str:
        current = self._current_version()
        if expected_version != current:
            raise VersionConflict(expected_version, current)
        new_version = str(int(current) + 1)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(text.encode("utf-8"))
        meta = self._load_meta()
        history = ([{"text": text, "version": new_version, "author": self._author}]
                   + meta.get("history", []))[:_HISTORY_CAP]
        self._meta.write_text(json.dumps({"version": new_version, "history": history}))
        return new_version

    def human_save(self, text: str, author_app: str = "editor") -> str:
        """Simulate an out-of-band edit to the 'remote' (unconditional), the way a
        person editing the file directly would — bumps the version."""
        current = self._current_version()
        new_version = str(int(current) + 1)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_bytes(text.encode("utf-8"))
        meta = self._load_meta()
        history = ([{"text": text, "version": new_version, "author": author_app}]
                   + meta.get("history", []))[:_HISTORY_CAP]
        self._meta.write_text(json.dumps({"version": new_version, "history": history}))
        return new_version

    def versions(self, limit: int = 10) -> list[RemoteDoc]:
        meta = self._load_meta()
        return [RemoteDoc(text=h["text"], version=h["version"],
                          author_app=h.get("author", ""))
                for h in meta.get("history", [])[:limit]]


__all__ = ["LocalFileEditor"]
