# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Identifier helpers — auto-generated IDs + human-readable slugs.

Axiom UX principle: create flows must never force users to invent
IDs. These helpers provide the default uuid + slug pair; callers
echo the generated identifier back so the user knows what was made.
"""

from __future__ import annotations

import re
import unicodedata
import uuid

# ---------------------------------------------------------------------------
# Slug generation
# ---------------------------------------------------------------------------


_SLUG_CLEAN = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Convert arbitrary text to a URL-safe lowercase slug.

    Empty or unslugifiable input returns "untitled" so downstream
    defaults still work without guarding.
    """
    if not text:
        return "untitled"
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lower = ascii_only.lower()
    slug = _SLUG_CLEAN.sub("-", lower).strip("-")
    return slug or "untitled"


# ---------------------------------------------------------------------------
# UUID + short suffix
# ---------------------------------------------------------------------------


def generate_id() -> str:
    """Generate a new globally unique identifier (uuid4)."""
    return str(uuid.uuid4())


def short_suffix(uuid_str: str, length: int = 6) -> str:
    """Return a short hex suffix derived from a uuid (first N hex chars)."""
    cleaned = uuid_str.replace("-", "")
    return cleaned[:length]


# ---------------------------------------------------------------------------
# Default slug (title + suffix)
# ---------------------------------------------------------------------------


def default_slug(title: str | None, uuid_str: str) -> str:
    """Produce a default slug: slugified title + short uuid suffix.

    Guarantees uniqueness under slug collisions because the suffix
    comes from the uuid.
    """
    base = slugify(title or "")
    return f"{base}-{short_suffix(uuid_str)}"


# ---------------------------------------------------------------------------
# One-shot create
# ---------------------------------------------------------------------------


def create_identity(
    title: str | None = None,
    slug: str | None = None,
) -> dict:
    """Create a fresh {id, slug} pair for a new object.

    If `slug` is provided by the caller, it's slugified to enforce
    URL-safety but otherwise used verbatim (no uuid suffix appended).
    If only `title` is provided, the slug is derived + has the uuid
    suffix for uniqueness. If neither is provided, the slug is
    `untitled-<suffix>`.
    """
    new_id = generate_id()
    if slug:
        clean = slugify(slug)
    else:
        clean = default_slug(title, new_id)
    return {"id": new_id, "slug": clean}
