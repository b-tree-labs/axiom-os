# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""One library, two vocabularies.

The claim this package makes is that intake, classification and placement are
the same everywhere and only the vocabulary differs. The way to test a claim
like that is to run the same machinery under two unrelated domains and show that
nothing in the machinery knows which one it is serving.

So these tests register an agricultural taxonomy and a nuclear one, and put the
same files through both.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.library import (
    Classification,
    ClassifierChain,
    DocumentType,
    FilenameClassifier,
    LibraryTaxonomy,
    TaxonomyError,
    get_taxonomy,
    intake,
    register_taxonomy,
)
from axiom.extensions.builtins.library import taxonomy as tx

AG = LibraryTaxonomy(
    name="ag",
    types=(
        DocumentType("soil_sample", "Soil Sample", "soil_tests"),
        DocumentType("yield_map", "Yield Map", "yield"),
        DocumentType("invoice", "Invoice", "financial"),
    ),
    category_order=("soil_tests", "yield", "financial", "documents"),
)

NUCLEAR = LibraryTaxonomy(
    name="nuclear",
    types=(
        DocumentType("irradiation_report", "Irradiation Report", "irradiation"),
        DocumentType("calibration_record", "Calibration Record", "calibration"),
        DocumentType("invoice", "Invoice", "financial"),
    ),
    category_order=("irradiation", "calibration", "financial", "documents"),
)


@pytest.fixture(autouse=True)
def _registry():
    tx.clear_taxonomies()
    register_taxonomy(AG)
    register_taxonomy(NUCLEAR)
    yield
    tx.clear_taxonomies()


def _chain(*rules, fallback="other"):
    return ClassifierChain(
        classifiers=[FilenameClassifier(name="filename", rules=list(rules))],
        fallback_type=fallback,
    )


# --- the same machinery under two domains -----------------------------------

def test_one_file_files_differently_under_two_vocabularies():
    """The whole claim: swap the taxonomy, the machinery does not change."""
    ag_chain = _chain((lambda n: "soil" in n, "soil_sample", 0.9))
    nuc_chain = _chain((lambda n: "irradiation" in n, "irradiation_report", 0.9))

    ag = intake("2026-soil-report.pdf", chain=ag_chain, taxonomy="ag")
    nuc = intake("irradiation-run-12.pdf", chain=nuc_chain, taxonomy="nuclear")

    assert ag.category == "soil_tests" and ag.label == "Soil Sample"
    assert nuc.category == "irradiation" and nuc.label == "Irradiation Report"


def test_a_shared_type_files_the_same_way_in_both():
    """Where domains agree, they should agree — no per-domain special-casing."""
    chain = _chain((lambda n: "invoice" in n, "invoice", 0.9))
    for name in ("ag", "nuclear"):
        assert intake("invoice-33.pdf", chain=chain, taxonomy=name).category == "financial"


def test_the_core_carries_domain_attributes_without_understanding_them():
    """One product extracts nutrients, another an isotope. The core sees neither."""

    class Extracting:
        name = "extractor"

        def classify(self, file_name, mime_type, content):
            return Classification(
                document_type="irradiation_report", confidence=0.9,
                attributes={"isotope": "Co-60", "flux": 1.2e13},
            )

    result = intake(
        "run.pdf", chain=ClassifierChain(classifiers=[Extracting()]), taxonomy="nuclear"
    )
    assert result.attributes == {"isotope": "Co-60", "flux": 1.2e13}


# --- always answers ---------------------------------------------------------

def test_an_unrecognised_file_is_filed_not_rejected():
    """A library that refuses what it does not know is a library that loses things."""
    result = intake("mystery.bin", chain=_chain(), taxonomy="nuclear")
    assert result.category == "documents"
    assert not result.was_recognised
    assert "unrecognised" in result.describe()


def test_a_weak_match_is_kept_over_the_fallback():
    """Better a low-confidence answer than none, as long as it says it is low."""
    chain = _chain((lambda n: n.endswith(".pdf"), "calibration_record", 0.2))
    result = intake("something.pdf", chain=chain, taxonomy="nuclear")
    assert result.document_type == "calibration_record"
    assert result.confidence == pytest.approx(0.2)


def test_a_confident_later_classifier_beats_a_weak_earlier_one():
    """Cheapest-first must not mean weakest-wins."""

    class Strong:
        name = "content"

        def classify(self, file_name, mime_type, content):
            return Classification(document_type="irradiation_report", confidence=0.95)

    weak = FilenameClassifier(
        name="filename", rules=[(lambda n: True, "calibration_record", 0.2)]
    )
    chain = ClassifierChain(classifiers=[weak, Strong()])
    result = intake("x.pdf", chain=chain, taxonomy="nuclear")
    assert result.document_type == "irradiation_report"
    assert result.classifier == "content"


def test_a_broken_classifier_does_not_make_every_upload_unfilable(caplog):
    """One bad rule must not take intake down — and must not do so quietly."""
    import logging

    class Broken:
        name = "broken"

        def classify(self, *_a):
            raise RuntimeError("bad rule")

    good = FilenameClassifier(name="filename", rules=[(lambda n: True, "invoice", 0.9)])
    chain = ClassifierChain(classifiers=[Broken(), good])

    with caplog.at_level(logging.ERROR, logger="axiom.extensions.builtins.library.classify"):
        result = intake("x.pdf", chain=chain, taxonomy="ag")

    assert result.category == "financial", "a healthy rule was lost to a broken one"
    assert any("broken" in r.message for r in caplog.records)


def test_the_decision_is_explainable():
    """A wrong placement should be a correction, not an argument with an oracle."""
    chain = _chain((lambda n: "soil" in n, "soil_sample", 0.82))
    text = intake("soil.pdf", chain=chain, taxonomy="ag").describe()
    assert "Soil Sample" in text and "82%" in text and "filename" in text


# --- taxonomy hygiene -------------------------------------------------------

def test_a_duplicate_document_type_is_refused():
    with pytest.raises(TaxonomyError, match="duplicate document type"):
        LibraryTaxonomy(
            name="broken",
            types=(DocumentType("a", "A", "x"), DocumentType("a", "A2", "x")),
            category_order=("x",),
        )


def test_a_category_missing_from_the_order_is_refused():
    """Otherwise a document files into a category the UI never renders."""
    with pytest.raises(TaxonomyError, match="missing from category_order"):
        LibraryTaxonomy(
            name="broken",
            types=(DocumentType("a", "A", "unlisted"),),
            category_order=("x",),
        )


def test_asking_for_an_unregistered_taxonomy_says_what_is_registered():
    with pytest.raises(TaxonomyError, match="known: ag, nuclear"):
        get_taxonomy("mars")


# --- the library as the first real contributor to /api/v1 -------------------

def test_the_manifest_declaration_becomes_a_live_route():
    """Proves the D3 chain with a real extension, not a fixture.

    The contribution registry was tested against a temporary manifest written by
    the test. This is the first shipped extension that declares `kind = "api"`,
    so it is the first evidence the mechanism works on something real — and it
    catches the case where a manifest names an entry that does not import.
    """
    from fastapi import APIRouter
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.library.api import register_routes

    router = APIRouter(prefix="/api/v1")
    register_routes(router, subpath="/library")

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    listed = client.get("/api/v1/library/taxonomies")
    assert listed.status_code == 200
    assert set(listed.json()["taxonomies"]) == {"ag", "nuclear"}

    one = client.get("/api/v1/library/taxonomies/nuclear")
    assert one.status_code == 200
    body = one.json()
    assert body["category_order"][0] == "irradiation"
    assert {t["key"] for t in body["types"]} == {
        "irradiation_report", "calibration_record", "invoice"
    }


def test_an_unknown_taxonomy_404s_with_what_is_registered():
    """A 404 that names the alternatives is an answer; a bare 404 is a dead end."""
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from axiom.extensions.builtins.library.api import register_routes

    router = APIRouter(prefix="/api/v1")
    register_routes(router, subpath="/library")
    app = FastAPI()
    app.include_router(router)

    r = TestClient(app).get("/api/v1/library/taxonomies/mars")
    assert r.status_code == 404
    assert "ag" in r.json()["detail"] and "nuclear" in r.json()["detail"]


def test_the_manifest_entry_matches_the_function_that_exists():
    """A declaration naming a missing entry ships a route that 404s in prod."""
    import importlib
    import pathlib
    import tomllib

    manifest = pathlib.Path(
        "src/axiom/extensions/builtins/library/axiom-extension.toml"
    )
    if not manifest.exists():  # packaged install
        pytest.skip("manifest not present in this layout")

    data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    api_blocks = [
        p for p in data["extension"].get("provides", []) if p.get("kind") == "api"
    ]
    assert api_blocks, "the library no longer declares an api surface"

    for block in api_blocks:
        module_path, _, function_name = block["entry"].partition(":")
        module = importlib.import_module(module_path)
        assert hasattr(module, function_name), (
            f"manifest names {block['entry']!r} but that function does not exist"
        )
