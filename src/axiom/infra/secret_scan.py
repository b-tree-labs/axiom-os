# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Secret pattern scanner — the fail-closed gate for content leaving a node.

Deliberately high-precision, low-recall: every pattern here is a
well-known credential *format* (provider-prefixed tokens, key headers),
not a heuristic like entropy or the word "password". A refused export
names the finding; a false positive costs a re-run with an exclude,
while a false negative costs a replicated credential — so patterns are
added only when their format is distinctive. Broad/noisy classes (JWTs,
generic hex, ``key=`` assignments) are intentionally absent.

Findings carry the pattern name and location, never the matched text —
the scanner must not become the leak.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# name → compiled pattern. Order is match-report order.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key-block", re.compile(
        r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY"
    )),
    ("aws-access-key-id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("github-fine-grained-pat", re.compile(
        r"\bgithub_pat_[A-Za-z0-9_]{22,255}\b"
    )),
    ("gitlab-pat", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-style-api-key", re.compile(r"\bsk-[A-Za-z0-9_-]{32,}\b")),
)


@dataclass(frozen=True)
class SecretFinding:
    """One detection: which pattern, where. Never the matched text."""

    pattern: str
    location: str
    line: int


def scan_text(text: str, *, location: str = "") -> list[SecretFinding]:
    """Scan a text for credential formats. Returns findings, possibly empty."""
    findings: list[SecretFinding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(
                    SecretFinding(pattern=name, location=location, line=lineno)
                )
    return findings


def scan_bytes(data: bytes, *, location: str = "") -> list[SecretFinding]:
    """Scan raw bytes; undecodable byte ranges are ignored (credentials in
    the classes above are ASCII, so lossy decoding cannot hide one)."""
    return scan_text(data.decode("utf-8", errors="ignore"), location=location)
