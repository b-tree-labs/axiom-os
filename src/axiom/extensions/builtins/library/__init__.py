# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A document library whose vocabulary is supplied by the product using it.

Intake, classification, placement and retrieval are the same everywhere. What
differs is what the documents *are* — one product files soil tests and yield
maps, another irradiation reports and calibration records. So the machinery is
here and the vocabulary is a registered taxonomy, which is the only thing a
domain has to write.

    register_taxonomy(LibraryTaxonomy(name="nuclear", types=(...), ...))
    result = intake(file_name, content, taxonomy="nuclear", chain=chain)

The result says what the file is, how sure the chain was, which classifier
decided, and where it filed — so a placement a user disagrees with is a
conversation about evidence rather than an argument with an oracle.
"""

from axiom.extensions.builtins.library.classify import (
    Classification,
    Classifier,
    ClassifierChain,
    FilenameClassifier,
)
from axiom.extensions.builtins.library.intake import IntakeResult, intake
from axiom.extensions.builtins.library.taxonomy import (
    DocumentType,
    LibraryTaxonomy,
    TaxonomyError,
    get_taxonomy,
    register_taxonomy,
    registered_taxonomies,
)

__all__ = [
    "Classification",
    "Classifier",
    "ClassifierChain",
    "DocumentType",
    "FilenameClassifier",
    "IntakeResult",
    "LibraryTaxonomy",
    "TaxonomyError",
    "get_taxonomy",
    "intake",
    "register_taxonomy",
    "registered_taxonomies",
]
