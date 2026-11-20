# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The vocabulary a library has before a product registers its own.

A product registers a taxonomy and, optionally, a classifier chain for it. An
installation with neither still has a working library: documents file by what
kind of file they are, which is the one classification every file supports. The
first product taxonomy registered replaces this one as the active default,
because a product that bothered to say what its documents are meant it.

``AXIOM_LIBRARY_TAXONOMY`` names the active taxonomy explicitly when a node
carries more than one.
"""

from __future__ import annotations

import os
import threading
from pathlib import PurePosixPath

from axiom.extensions.builtins.library.classify import Classification, ClassifierChain
from axiom.extensions.builtins.library.taxonomy import (
    DocumentType,
    LibraryTaxonomy,
    get_taxonomy,
    register_taxonomy,
    registered_taxonomies,
)

GENERIC = "generic"
TAXONOMY_ENV = "AXIOM_LIBRARY_TAXONOMY"

GENERIC_TAXONOMY = LibraryTaxonomy(
    name=GENERIC,
    types=(
        DocumentType("document", "Document", "documents"),
        DocumentType("spreadsheet", "Spreadsheet", "spreadsheets"),
        DocumentType("presentation", "Presentation", "presentations"),
        DocumentType("image", "Image", "images"),
        DocumentType("data", "Data file", "data"),
    ),
    category_order=("documents", "spreadsheets", "presentations", "images", "data"),
    fallback_category="documents",
)

_BY_EXTENSION = {
    ".pdf": "document", ".doc": "document", ".docx": "document", ".odt": "document",
    ".txt": "document", ".md": "document", ".rst": "document",
    ".xlsx": "spreadsheet", ".csv": "spreadsheet", ".tsv": "spreadsheet",
    ".pptx": "presentation",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image", ".webp": "image",
    ".json": "data", ".xml": "data", ".log": "data",
}


class FileKindClassifier:
    """The classification every file supports: what kind of file it is."""

    name = "file-kind"

    def classify(self, file_name: str, mime_type: str | None, content: bytes | None) -> Classification | None:
        kind = _BY_EXTENSION.get(PurePosixPath(file_name or "").suffix.lower())
        if kind is None:
            return None
        return Classification(document_type=kind, confidence=0.5, classifier=self.name)


_CHAINS: dict[str, ClassifierChain] = {}
_lock = threading.Lock()


def register_chain(taxonomy: str, chain: ClassifierChain) -> None:
    """Install a product's classifier chain for its taxonomy (cheapest first)."""
    with _lock:
        _CHAINS[taxonomy] = chain


def chain_for(taxonomy: str) -> ClassifierChain:
    with _lock:
        found = _CHAINS.get(taxonomy)
    if found is not None:
        return found
    fallback = get_taxonomy(taxonomy).fallback_type
    return ClassifierChain([FileKindClassifier()], fallback_type=fallback)


def active_taxonomy() -> LibraryTaxonomy:
    """The taxonomy uploads file under on this installation."""
    if GENERIC not in registered_taxonomies():
        register_taxonomy(GENERIC_TAXONOMY)
    wanted = (os.environ.get(TAXONOMY_ENV) or "").strip()
    if wanted:
        return get_taxonomy(wanted)
    products = [n for n in registered_taxonomies() if n != GENERIC]
    return get_taxonomy(products[0] if len(products) == 1 else GENERIC)


__all__ = [
    "GENERIC",
    "GENERIC_TAXONOMY",
    "TAXONOMY_ENV",
    "FileKindClassifier",
    "active_taxonomy",
    "chain_for",
    "register_chain",
]
