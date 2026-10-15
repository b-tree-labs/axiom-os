# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What kinds of document a product has, and where each one files.

The library machinery — intake, classification, placement, retrieval — is the
same everywhere. What differs is the vocabulary: one product files soil tests
and yield maps, another files irradiation reports and calibration records. That
vocabulary is the whole of the difference, so it is the whole of what a domain
supplies.

A taxonomy is therefore data, not code: document types, the categories they file
under, and the order those categories are shown in. A product registers one and
gets a library; it does not subclass anything and cannot change how placement
works, which is what keeps every product's library behaving the same way.

An unknown type files under a declared fallback rather than being rejected. A
library that refuses what it does not recognise is a library that loses
documents, and the document a user could not file is the one they needed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


class TaxonomyError(ValueError):
    """A taxonomy that would behave surprisingly if it were accepted."""


@dataclass(frozen=True)
class DocumentType:
    """One kind of document a product recognises."""

    key: str
    label: str
    category: str


@dataclass(frozen=True)
class LibraryTaxonomy:
    """A product's document vocabulary — the domain half of a library."""

    name: str
    types: tuple[DocumentType, ...]
    category_order: tuple[str, ...]
    fallback_category: str = "documents"
    fallback_type: str = "other"

    def __post_init__(self) -> None:
        if not self.name:
            raise TaxonomyError("a taxonomy needs a name; it is how a product asks for it")

        seen: set[str] = set()
        for doc_type in self.types:
            if doc_type.key in seen:
                raise TaxonomyError(
                    f"{self.name}: duplicate document type {doc_type.key!r} — "
                    "the second would silently shadow the first"
                )
            seen.add(doc_type.key)

        declared = set(self.category_order) | {self.fallback_category}
        unplaceable = sorted(
            {t.category for t in self.types} - declared
        )
        if unplaceable:
            # A category no order mentions renders in an arbitrary position, or
            # not at all. Catching it here beats a user finding a document that
            # filed nowhere.
            raise TaxonomyError(
                f"{self.name}: categories {unplaceable} are used by document "
                "types but missing from category_order"
            )

    @property
    def by_key(self) -> Mapping[str, DocumentType]:
        return MappingProxyType({t.key: t for t in self.types})

    def category_for(self, document_type: str) -> str:
        """Where a document of this type files. Never raises.

        An unrecognised type files under the fallback rather than being
        rejected: losing a document is worse than filing it imprecisely, and the
        user can always move it.
        """
        found = self.by_key.get(document_type)
        return found.category if found else self.fallback_category

    def label_for(self, document_type: str) -> str:
        found = self.by_key.get(document_type)
        return found.label if found else document_type.replace("_", " ").title()


_TAXONOMIES: dict[str, LibraryTaxonomy] = {}


def register_taxonomy(taxonomy: LibraryTaxonomy) -> None:
    """Install a product's vocabulary.

    Re-registering the same name replaces it, so a host can reload its domain
    pack without restarting. Registering two different vocabularies under one
    name would make the active library depend on import order, so the name is
    the identity and the last registration wins visibly rather than silently
    merging.
    """
    _TAXONOMIES[taxonomy.name] = taxonomy


def get_taxonomy(name: str) -> LibraryTaxonomy:
    if name not in _TAXONOMIES:
        known = ", ".join(sorted(_TAXONOMIES)) or "none registered"
        raise TaxonomyError(
            f"no library taxonomy named {name!r} (known: {known}). A product "
            "registers its document vocabulary before opening its library."
        )
    return _TAXONOMIES[name]


def registered_taxonomies() -> tuple[str, ...]:
    return tuple(sorted(_TAXONOMIES))


def clear_taxonomies() -> None:
    """Tests only; never in a running process."""
    _TAXONOMIES.clear()


__all__ = [
    "DocumentType",
    "LibraryTaxonomy",
    "TaxonomyError",
    "clear_taxonomies",
    "get_taxonomy",
    "register_taxonomy",
    "registered_taxonomies",
]
