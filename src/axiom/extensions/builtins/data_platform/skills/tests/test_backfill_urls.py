# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""ADR-091 URL backfill — path matching + verb wiring.

The hard part is matching a source's freshly-catalogued *bare* origin paths to
documents that were indexed with a *landing-prefixed* path (the local-ingest
scheme). These lock the boundary-aware suffix match, including the two ways it
must refuse to guess.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform.skills.backfill_urls import (
    match_catalog_to_docs,
)


def test_matches_prefixed_local_paths_to_bare_catalog():
    catalog = [
        ("/Corpus/Approved/tri.pdf", "111", "https://app.box.com/file/111"),
        ("/Corpus/Approved/sub/admn.pdf", "222", "https://app.box.com/file/222"),
    ]
    docs = [
        "landing/Corpus/Approved/tri.pdf",       # prefixed local path
        "landing/Corpus/Approved/sub/admn.pdf",
        "landing/misc/orphan.pdf",               # no catalog entry
    ]
    matched, unmatched, ambiguous = match_catalog_to_docs(catalog, docs)

    assert matched["landing/Corpus/Approved/tri.pdf"] == ("111", "https://app.box.com/file/111")
    assert matched["landing/Corpus/Approved/sub/admn.pdf"] == ("222", "https://app.box.com/file/222")
    assert unmatched == ["landing/misc/orphan.pdf"]
    assert ambiguous == []


def test_exact_bare_path_matches():
    """A corpus ingested straight through the connector stores the bare path."""
    catalog = [("/A/x.pdf", "9", "u9")]
    matched, unmatched, ambiguous = match_catalog_to_docs(catalog, ["A/x.pdf"])
    assert matched == {"A/x.pdf": ("9", "u9")}
    assert not unmatched and not ambiguous


def test_ambiguous_multi_hit_is_left_untouched():
    """A doc that suffix-matches two distinct catalog entries is not guessed."""
    catalog = [("/x/report.pdf", "1", "u1"), ("/y/x/report.pdf", "2", "u2")]
    matched, unmatched, ambiguous = match_catalog_to_docs(catalog, ["root/y/x/report.pdf"])
    assert matched == {}
    assert ambiguous == ["root/y/x/report.pdf"]


def test_bare_filename_catalog_does_not_smear():
    """A root-level 'x.pdf' must not suffix-match every */x.pdf (equality only)."""
    catalog = [("x.pdf", "1", "u1")]
    matched, unmatched, ambiguous = match_catalog_to_docs(catalog, ["a/x.pdf", "b/x.pdf"])
    assert matched == {}
    assert set(unmatched) == {"a/x.pdf", "b/x.pdf"}


def test_verb_is_registered_and_parses():
    from axiom.extensions.builtins.data_platform import skills as data_skills
    from axiom.extensions.builtins.data_platform.cli import _parser

    reg = data_skills.bind_default()
    assert reg.has("data.backfill-urls")

    args = _parser().parse_args(["backfill-urls", "--connector", "c", "--dry-run"])
    assert args.verb == "backfill-urls"
    assert args.dry_run is True
    assert args.connector == "c"
