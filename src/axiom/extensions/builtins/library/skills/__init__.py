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
            "ec_label": "the export-control label you confirm (public, internal, regulated, controlled)",
            "retention_class": "the retention class (default operational)",
        },
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.status",
        fn=_v.status,
        description=(
            "What happened to one file you uploaded: every pipeline stage, what each extractor found, "
            "where each output went, the export-control verdict and where the Data Book filed it."
        ),
        inputs=_ID,
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
    SkillSpec(
        name="library.reextract",
        fn=_v.reextract,
        description=(
            "Run a document's extractors again from the bytes bronze kept (all, named ones, or only those "
            "whose version changed); the new results replace the current ones and every route is offered them."
        ),
        inputs={**_ID, "extractors": "extractor names to re-run (default: all that match)",
                "stale_only": "only extractors whose installed version differs from the one that ran"},
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.move",
        fn=_v.move,
        description=(
            "File a document in another Data Book folder. The move is remembered, so later similar "
            "documents file there too."
        ),
        inputs={**_ID, "folder": "the folder's path in the Book"},
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.share",
        fn=_v.share,
        description=(
            "Share a Library file or folder with a person or group at your site, as viewer or editor, "
            "optionally until a date; remove someone; set general access (restricted, or everyone at the "
            "site with a role); or change its two sharing settings. Not a chat or session share: it "
            "changes who may open one Library file or folder, and every change is recorded."
        ),
        inputs={
            **_ID,
            "with": "who: @name:<site> or group:<id>",
            "role": "viewer (default) or editor",
            "expires": "YYYY-MM-DD, within a year",
            "remove": "true to remove `with`'s access",
            "general": "restricted, site, or site:<role>",
            "editors_can_share": "the Owner's setting: may editors share it (true/false)",
            "viewers_can_download": "the Owner's setting: may viewers download and copy (true/false)",
            "notify": "false to not tell the person (default: tell them)",
            "message": "a note for the person told",
        },
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.access",
        fn=_v.access,
        description=(
            "Who has access to a Library file or folder and why: its Owner, the people and groups with "
            "their roles and expiry (when you may see them), general access, and whether you may share it. "
            "Not your account's access or sign-in: one Library object's access list."
        ),
        inputs=_ID,
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        requires=(),
    ),
    SkillSpec(
        name="library.transfer",
        fn=_v.transfer,
        description=(
            "Make someone already on a Library file's or folder's access list its Owner; you become an "
            "Editor and it stays where it is filed."
        ),
        inputs={**_ID, "to": "the new Owner: @name:<site>, already on the access list"},
        # Giving away ownership is a person's act, never an agent's tool call.
        surfaces=("cli",),
        side_effects=True,
        idempotent=False,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.request",
        fn=_v.request,
        description=(
            "Ask the Owner for access to a Library file or folder (works from its address alone, which "
            "reveals nothing), see or withdraw your request, remind once a day; or, as someone who may "
            "share it, list its requests and approve (with the role you choose) or decline with a reason."
        ),
        inputs={
            **_ID,
            "action": "ask (default), status, withdraw, remind, list, approve, decline",
            "role": "the role asked for or given: viewer (default) or editor",
            "note": "a note to the Owner, when asking",
            "request_id": "the request, for withdraw, remind, approve, decline",
            "reason": "why, when declining: the requester sees it as written",
        },
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=False,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.ec_status",
        fn=_v.ec_status,
        description=(
            "Why a library file is held from retrieval (the controlled code and the kind of file), its "
            "export-control review trail, and whether you may release it."
        ),
        inputs=_ID,
        surfaces=_SURFACES,
        side_effects=False,
        idempotent=True,
        risk="read",
        cost_class="lookup",
        requires=(),
    ),
    SkillSpec(
        name="library.ec_release",
        fn=_v.ec_release,
        description=(
            "Release one held file version for retrieval. Needs the site's ec-reviewer role; the uploader can "
            "never release their own file. The reason is required and the decision is attested."
        ),
        inputs={**_ID, "reason": "one line saying why it may be released"},
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=False,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.ec_keep_held",
        fn=_v.ec_keep_held,
        description=(
            "Record that an ec-reviewer looked at a held file and keeps it held. The reason is required "
            "and the decision is attested. An export-control review decision, not the harness's own /help."
        ),
        inputs={**_ID, "reason": "one line saying why it stays held"},
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=False,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.ec_rehold",
        fn=_v.ec_rehold,
        description=(
            "Take a release back: the file's text leaves retrieval and it is held again. An ec-reviewer or "
            "the uploader may; the reason is required and the decision is attested."
        ),
        inputs={**_ID, "reason": "one line saying why it is held again"},
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=False,
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
    SkillSpec(
        name="library.label",
        fn=_v.label,
        description=(
            "Show, confirm or change a document's export-control label (public, internal, regulated, "
            "controlled) and its retention class (system, operational, qualification, lifetime, legal_hold). "
            "With no label or class given, it answers what the document's label is."
        ),
        inputs={**_ID, "label": "the export-control label to confirm",
                "retention_class": "the retention class (may lengthen, never shorten)",
                "release_hold": "true to end a legal hold",
                "sweep": "true to take every unlabelled indexed document at your site out of retrieval"},
        surfaces=_SURFACES,
        side_effects=True,
        idempotent=True,
        risk="reversible-write",
        requires=(),
    ),
    SkillSpec(
        name="library.purge",
        fn=_v.purge,
        description=(
            "Delete a document forever, from Deleted files. Refused before its retention class's floor, "
            "under a legal hold and under an export-control hold; recorded in the audit chain first."
        ),
        inputs=_ID,
        # Destroying bytes is a person's act at a terminal or in the page, never an agent's tool call.
        surfaces=("cli",),
        side_effects=True,
        idempotent=True,
        risk="destructive",
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
