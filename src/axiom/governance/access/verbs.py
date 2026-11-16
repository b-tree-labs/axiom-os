# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every verb names the least role it needs, and nothing else decides it (SHR-R1.4).

The table is prd-sharing-and-access §5 SHR-R1, verb for verb. Three verbs have a
rule beside their role, and the rule is named here rather than left to a caller:

- ``download`` and ``copy`` need Editor when the Owner turned "Viewers and
  commenters can download and copy" off.
- ``share`` needs Owner when the Owner turned "Editors can change permissions
  and share" off.
- ``delete`` is the Owner's, or an Editor's of the person's folder that holds it;
  ``restore`` is the Owner's or the person who deleted it.

Two verbs are not about a role on the object: ``folder.create`` needs only being
at the site (SHR-R4.6), and ``book.category`` needs the Book manager site role
(SHR-R4.4).
"""

from __future__ import annotations

from dataclasses import dataclass

from axiom.governance.access.roles import DOCUMENT, FOLDER, Role


@dataclass(frozen=True)
class Verb:
    name: str
    least: Role | None
    kinds: tuple[str, ...]
    #: What the verb does, in the words a refusal uses ("move it").
    act: str
    #: ``None`` when the least role alone decides; else the rule's name.
    rule: str | None = None


_BOTH = (DOCUMENT, FOLDER)

VERBS: dict[str, Verb] = {
    v.name: v
    for v in (
        # Viewer: see it, open it, its versions and activity.
        Verb("library.list", Role.VIEWER, _BOTH, "see it"),
        Verb("library.show", Role.VIEWER, _BOTH, "open it"),
        Verb("library.search", Role.VIEWER, (DOCUMENT,), "find it"),
        Verb("library.preview", Role.VIEWER, (DOCUMENT,), "preview it"),
        Verb("library.versions", Role.VIEWER, (DOCUMENT,), "see its versions"),
        Verb("library.status", Role.VIEWER, (DOCUMENT,), "see what happened to it"),
        Verb("library.access", Role.VIEWER, _BOTH, "see who has access"),
        Verb("library.download", Role.VIEWER, (DOCUMENT,), "download it", rule="download"),
        Verb("library.copy", Role.VIEWER, _BOTH, "copy it", rule="download"),
        # Commenter, once comments exist.
        Verb("library.comment", Role.COMMENTER, (DOCUMENT,), "comment on it"),
        # Editor: change it.
        Verb("library.update", Role.EDITOR, (DOCUMENT,), "change it"),
        Verb("library.reextract", Role.EDITOR, (DOCUMENT,), "re-extract it"),
        Verb("library.version_add", Role.EDITOR, (DOCUMENT,), "upload a new version"),
        Verb("library.version_restore", Role.EDITOR, (DOCUMENT,), "restore an old version"),
        Verb("library.move", Role.EDITOR, (DOCUMENT,), "move it"),
        Verb("library.folder.rename", Role.EDITOR, (FOLDER,), "rename or move this folder"),
        Verb("library.folder.file_into", Role.EDITOR, (FOLDER,), "put things in this folder"),
        Verb("library.folder.delete", Role.EDITOR, (FOLDER,), "delete this folder"),
        Verb("library.share", Role.EDITOR, _BOTH, "share it", rule="share"),
        Verb("library.delete", Role.OWNER, (DOCUMENT,), "delete it", rule="delete"),
        Verb("library.restore", Role.OWNER, (DOCUMENT,), "restore it", rule="restore"),
        # Owner.
        Verb("library.purge", Role.OWNER, (DOCUMENT,), "delete it forever"),
        Verb("library.label", Role.OWNER, (DOCUMENT,), "confirm its label"),
        Verb("library.settings", Role.OWNER, _BOTH, "change its sharing settings"),
        Verb("library.transfer", Role.OWNER, _BOTH, "transfer ownership"),
        # Not a role on the object.
        Verb("library.request", None, _BOTH, "ask for access", rule="at-site"),
        Verb("library.folder.create", None, (FOLDER,), "make a folder", rule="at-site"),
        Verb("library.book.category", None, (FOLDER,), "change a Data Book category", rule="book-manager"),
    )
}


def verb(name: str) -> Verb:
    try:
        return VERBS[name]
    except KeyError:
        raise LookupError(f"no access verb {name!r}: every verb names its least role in VERBS") from None


__all__ = ["VERBS", "Verb", "verb"]
