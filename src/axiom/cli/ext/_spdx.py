# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Fuzzy SPDX license ID resolver.

Users reach for ``--license mit`` or ``--allow-license apache2`` far more
often than they reach for the canonical ``Apache-2.0`` spelling. This
module translates common shorthands to canonical SPDX identifiers for the
v0.1 allowlist. Unknown / proprietary inputs return ``None`` so callers can
reject with a clear error.

The v0.1 allowlist is intentionally small — we grow it as needs emerge
rather than trying to ship the full SPDX catalogue. Anything outside the
list requires a deliberate override (``scan --allow-license ...``) so a
policy violation stays visible.
"""

from __future__ import annotations

# Canonical SPDX IDs the v0.1 tooling accepts without an explicit override.
ALLOWLIST: tuple[str, ...] = (
    "Apache-2.0",
    "MIT",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "MPL-2.0",
    "LGPL-3.0",
    "GPL-3.0",
    "ISC",
    "Unlicense",
)


# Exact lowercase keys the resolver accepts -> canonical SPDX ID.
#
# Every canonical ID maps to itself (case-insensitive) plus a handful of
# common aliases. We deliberately do NOT accept "agpl" or "cc-by-*" — they
# aren't on the v0.1 allowlist, so even if we resolved them the caller would
# reject the result.
_ALIASES: dict[str, str] = {
    # Apache 2.0
    "apache": "Apache-2.0",
    "apache2": "Apache-2.0",
    "apache-2": "Apache-2.0",
    "apache-2.0": "Apache-2.0",
    "apache2.0": "Apache-2.0",
    # MIT
    "mit": "MIT",
    # BSD
    "bsd": "BSD-3-Clause",  # most common default
    "bsd2": "BSD-2-Clause",
    "bsd-2": "BSD-2-Clause",
    "bsd-2-clause": "BSD-2-Clause",
    "bsd3": "BSD-3-Clause",
    "bsd-3": "BSD-3-Clause",
    "bsd-3-clause": "BSD-3-Clause",
    # MPL
    "mpl": "MPL-2.0",
    "mpl2": "MPL-2.0",
    "mpl-2.0": "MPL-2.0",
    # LGPL
    "lgpl": "LGPL-3.0",
    "lgpl3": "LGPL-3.0",
    "lgpl-3.0": "LGPL-3.0",
    # GPL
    "gpl": "GPL-3.0",
    "gpl3": "GPL-3.0",
    "gpl-3.0": "GPL-3.0",
    # ISC
    "isc": "ISC",
    # Unlicense / public domain
    "unlicense": "Unlicense",
    "public-domain": "Unlicense",
}


def resolve_spdx(user_input: str) -> str | None:
    """Return the canonical SPDX identifier matching ``user_input`` or ``None``.

    Resolution:

    1. Exact match against the allowlist (case-sensitive) — returns as-is.
    2. Lowercase alias lookup (handles ``apache``, ``MIT``, ``bsd3``, ...).
    3. ``None`` for Proprietary-* / unknown inputs.
    """
    if not user_input:
        return None

    # Canonical case match — preserves user intent when they spell it right.
    if user_input in ALLOWLIST:
        return user_input

    # Alias / case-insensitive match.
    key = user_input.strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]

    # Case-insensitive canonical match (e.g. ``APACHE-2.0``).
    for canonical in ALLOWLIST:
        if canonical.lower() == key:
            return canonical

    return None


def allowlist_hint() -> str:
    """Human-readable list of accepted IDs for error messages."""
    return ", ".join(ALLOWLIST)


__all__ = ["ALLOWLIST", "allowlist_hint", "resolve_spdx"]
