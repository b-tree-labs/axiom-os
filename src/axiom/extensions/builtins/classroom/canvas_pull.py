# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pull Canvas course content into the classroom materials store.

Phase 0.1 of `axi classroom canvas pull`. The classroom RAG / chunker
pipeline downstream of `axi classroom prep corpus` already knows how
to ingest from the materials store; this module is the bridge that
fetches Canvas-side content (pages, announcements, files, module
outline) and writes it through the same path. No duplicate upload —
the "Don't be Canvas Tool #31" go-deep value.

Idempotent by design: the materials store dedupes on content hash, so
re-running the pull is safe.
"""

from __future__ import annotations

from typing import Any

from .classroom_materials import ClassroomMaterialsStore
from .lms.canvas import CanvasLMSProvider


def pull_course_to_materials(
    provider: CanvasLMSProvider,
    course_id: str,
    store: ClassroomMaterialsStore,
) -> dict[str, int]:
    """Fetch Canvas course content into ``store``.

    Returns a summary dict: counts per content type plus a ``total``.
    Content-hash dedup makes the call idempotent.
    """
    summary = {"pages": 0, "announcements": 0, "files": 0, "outline": 0, "total": 0}

    # Pages — HTML body, stored as `<slug>.html`
    for page in provider.get_pages(course_id):
        store.add_text(
            page.body or "",
            filename=f"{page.url_slug}.html",
            title=page.title,
        )
        summary["pages"] += 1

    # Announcements — HTML message, stored as `announcement-<id>.html`
    for ann in provider.get_announcements(course_id):
        store.add_text(
            ann.message or "",
            filename=f"announcement-{ann.announcement_id}.html",
            title=ann.title,
        )
        summary["announcements"] += 1

    # Files — raw bytes, stored under display_name
    for f in provider.get_files(course_id):
        body = provider.get_file_content(course_id, f.file_id)
        if body is None:
            continue
        store.add_bytes(
            body,
            filename=f.display_name or f.file_id,
            title=f.display_name or None,
        )
        summary["files"] += 1

    # Module outline — synthesized markdown reflecting structure
    modules = provider.get_modules(course_id)
    if modules:
        outline_md = _build_outline_md(provider, course_id, modules)
        store.add_text(
            outline_md,
            filename="course-outline.md",
            title="Course outline",
        )
        summary["outline"] = 1

    summary["total"] = sum(v for k, v in summary.items() if k != "total")
    return summary


def _build_outline_md(
    provider: CanvasLMSProvider,
    course_id: str,
    modules: list[Any],
) -> str:
    """Render module hierarchy as markdown so the chunker can ingest it
    with proper boundary signal (headings → semantic chunks)."""
    lines: list[str] = ["# Course outline", ""]
    for m in modules:
        lines.append(f"## {m.name}")
        items = provider.get_module_items(course_id, m.module_id)
        if not items:
            lines.append("")
            continue
        for item in items:
            label = item.title or item.type
            lines.append(f"- **{item.type}** — {label}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
