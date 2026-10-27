# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Consistent content fingerprinting — centralized hashing for dedup and forensics.

Standard lengths:
    SHORT  (8)  — config hashes, provider identity
    MEDIUM (12) — review items, self-heal fingerprints, code blocks
    LONG   (16) — content dedup, echo suppression, signal correlation
    FULL   (64) — file integrity, audit trails

Usage:
    from axiom.infra.hash_utils import fingerprint, fingerprint_file

    fp = fingerprint("some text")                # 12-char hex (default)
    fp = fingerprint("some text", length=16)     # 16-char hex
    fp = fingerprint_file(path)                  # 16-char hex of file bytes
"""

from __future__ import annotations

import hashlib
from pathlib import Path

SHORT = 8
MEDIUM = 12
LONG = 16
FULL = 64


def fingerprint(content: str, *, length: int = MEDIUM) -> str:
    """SHA-256 fingerprint of text content, truncated to *length* hex chars."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:length]


def fingerprint_bytes(data: bytes, *, length: int = MEDIUM) -> str:
    """SHA-256 fingerprint of raw bytes, truncated to *length* hex chars."""
    return hashlib.sha256(data).hexdigest()[:length]


def fingerprint_file(path: Path | str, *, length: int = LONG) -> str:
    """SHA-256 fingerprint of a file's contents, truncated to *length* hex chars.

    Reads in 64KB chunks to handle large files efficiently.
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:length]


__all__ = [
    "fingerprint",
    "fingerprint_bytes",
    "fingerprint_file",
    "SHORT",
    "MEDIUM",
    "LONG",
    "FULL",
]
