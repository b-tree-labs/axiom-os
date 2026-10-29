# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The ``library`` skills, declared once; every surface routes through the registry."""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, SkillSpec

from . import verbs as _v

_SURFACES = ("cli", "mcp", "agent_tool")

_ID = {"id": "the library document id"}

SPECS = (
    SkillSpec(
        name="library.upload",
        fn=_v.upload,
        description=(
            "File a document in the library: guarded, landed in bronze, catalogued, classified and "
            "indexed for retrieval. On a bound install it is filed on the bound node."
        ),
        inputs={
            "path": "a local file to upload",
            "file_name": "the document's name (with content_base64, or to rename a path)",
            "content_base64": "the document's bytes, base64, when there is no path",
            "visibility": "site (default: everyone granted at the site) or private (only you)",
            "title": "a display title (default: the file name)",
            "tags": "tags, as a list or comma-separated",
            "collection": "a collection to file it in",
        },
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.list",
        fn=_v.list_items,
        description="List the library documents you may see at your site, newest first, with filters.",
        inputs={
            "q": "match on title, file name, label, collection or tag",
            "category": "only this category",
            "collection": "only this collection",
            "tag": "only documents with this tag",
            "visibility": "site or private",
            "status": "indexed, pending, unsupported or failed",
            "deleted": "list your deleted documents instead",
            "limit": "at most this many (default 200)",
        },
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        cost_class="lookup",
        requires=(),
    ),
    SkillSpec(
        name="library.show",
        fn=_v.show,
        description="One library document: where it filed, who filed it, and whether retrieval can find it.",
        inputs=_ID,
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        cost_class="lookup",
        requires=(),
    ),
    SkillSpec(
        name="library.search",
        fn=_v.search,
        description="Find library documents by what they say (retrieval over your site's and your own corpus).",
        inputs={"q": "what to look for", "limit": "at most this many documents (default 20)"},
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        cost_class="lookup",
        requires=(),
    ),
    SkillSpec(
        name="library.update",
        fn=_v.update,
        description=(
            "Change a document you filed: title, tags, collection, or visibility (which moves its "
            "chunks to the corpus the new audience reads)."
        ),
        inputs={
            **_ID,
            "title": "a new title",
            "tags": "the full tag list (replaces the old one)",
            "collection": "the collection, or empty to clear it",
            "visibility": "site or private",
        },
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.delete",
        fn=_v.delete,
        description=(
            "Remove a document you filed from retrieval and every listing. Its bytes stay in bronze "
            "behind a tombstone, so library.restore can bring it back."
        ),
        inputs=_ID,
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.restore",
        fn=_v.restore,
        description="Bring back a document you deleted, re-indexed from the bytes bronze kept.",
        inputs=_ID,
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
)


def bind(registry: SkillRegistry) -> None:
    for spec in SPECS:
        if not registry.has(spec.name):
            registry.register_skill(spec, mutating=bool(spec.side_effects))


def bind_default() -> SkillRegistry:
    registry = SkillRegistry()
    bind(registry)
    return registry


__all__ = ["SPECS", "bind", "bind_default"]
