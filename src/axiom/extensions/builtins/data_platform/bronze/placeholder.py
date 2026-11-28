# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Recognise a placeholder where a file should be (#1948).

A source can hold a stand-in instead of the file it names:

* a **Git LFS pointer**: a small text file (``version
  https://git-lfs.github.com/spec/v1`` / ``oid`` / ``size``) left behind when a
  repository was copied without fetching its LFS objects;
* **content that contradicts its extension**: a ``.pdf`` with no ``%PDF``
  signature (a sign-in page saved under the file's name, a truncated
  download, any other stand-in).

Either one landed as a document fails extraction quietly and goes missing from
retrieval with nobody told. This module only answers "is it a placeholder, and
which kind"; what to do about it belongs to the bronze gate and the ingest run.

Pure: bytes and a name in, a reason or ``None`` out. The extension table is
deliberately small and generic; an extension it does not know is never called
a placeholder. When the document conform step lands (ADR-079), this check moves
there with it.
"""

from __future__ import annotations

from pathlib import PurePosixPath

#: The reason recorded for a Git LFS pointer.
LFS_POINTER = "placeholder: git-lfs pointer"

_LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1"
#: Real pointers are ~130 bytes. Anything far larger that happens to start with
#: the spec line is a document about LFS, not a pointer.
_LFS_MAX_SIZE = 1024

#: How much of the head a caller should read for :func:`detect_placeholder`.
HEAD_BYTES = 2048

_ZIP = (b"PK\x03\x04",)

#: extension -> (signatures, how far into the head a signature may start).
#: PDF allows leading bytes before its header (the spec tolerates up to 1 KiB).
_SIGNATURES: dict[str, tuple[tuple[bytes, ...], int]] = {
    ".pdf": ((b"%PDF",), 1024),
    ".png": ((b"\x89PNG\r\n\x1a\n",), 0),
    ".jpg": ((b"\xff\xd8\xff",), 0),
    ".jpeg": ((b"\xff\xd8\xff",), 0),
    ".docx": (_ZIP, 0),
    ".xlsx": (_ZIP, 0),
    ".pptx": (_ZIP, 0),
}


def detect_placeholder(head: bytes, name: str | None, *, size: int | None = None) -> str | None:
    """Return why ``head`` is a placeholder, or ``None`` when it is not one.

    ``head`` is the content, or at least its first :data:`HEAD_BYTES`. ``size``
    is the whole content's length when only the head was read; it defaults to
    ``len(head)``. Empty content is not called a placeholder here: it has its
    own path (``empty_content``) downstream.
    """
    if not head:
        return None
    total = len(head) if size is None else size

    if head.startswith(_LFS_PREFIX) and total <= _LFS_MAX_SIZE:
        return LFS_POINTER

    ext = PurePosixPath(name or "").suffix.lower()
    known = _SIGNATURES.get(ext)
    if known is None:
        return None
    signatures, window = known
    if window:
        found = any(sig in head[: window + len(sig)] for sig in signatures)
    else:
        found = any(head.startswith(sig) for sig in signatures)
    if found:
        return None
    return f"placeholder: {ext} without its signature"


__all__ = ["HEAD_BYTES", "LFS_POINTER", "detect_placeholder"]
