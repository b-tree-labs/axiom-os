# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Provenance/artifact routing for ingest (the classify→route seam).

Export-control / proprietary handling is driven by *what an artifact is and
where it came from* — a licensed-vendor source folder, an executable/archive
artifact — not by scanning prose for buzzwords. This generic mechanism takes a
rule set and returns an exclude/quarantine/allow decision; a consumer layer
supplies the domain-specific rules (which sources are controlled, tier map).
"""

from __future__ import annotations

from axiom.rag.ingest_router import Disposition, ProvenanceRule, route_path


def _rules():
    return [
        ProvenanceRule("restricted-vendor/", Disposition.EXCLUDE, reason="licensed vendor software"),
        ProvenanceRule("*.zip", Disposition.QUARANTINE, reason="archive — inspect before ingest"),
        ProvenanceRule("public/", Disposition.ALLOW, tier="rag-community", reason="public docs"),
    ]


def test_excludes_by_directory_provenance():
    d = route_path("restricted-vendor/manual.pdf", _rules())
    assert d.disposition is Disposition.EXCLUDE
    assert d.matched == "restricted-vendor/"
    assert "licensed" in d.reason


def test_quarantines_by_artifact_extension():
    d = route_path("data/bundle.zip", _rules())
    assert d.disposition is Disposition.QUARANTINE


def test_allows_with_resolved_tier():
    d = route_path("public/intro.md", _rules())
    assert d.disposition is Disposition.ALLOW
    assert d.tier == "rag-community"


def test_first_matching_rule_wins():
    rules = [
        ProvenanceRule("public/secret/", Disposition.EXCLUDE, reason="carve-out"),
        ProvenanceRule("public/", Disposition.ALLOW, tier="rag-community"),
    ]
    assert route_path("public/secret/x.md", rules).disposition is Disposition.EXCLUDE
    assert route_path("public/ok.md", rules).disposition is Disposition.ALLOW


def test_default_allow_when_no_rule_matches():
    d = route_path(
        "misc/file.md",
        _rules(),
        default_disposition=Disposition.ALLOW,
        default_tier="rag-org",
    )
    assert d.disposition is Disposition.ALLOW
    assert d.tier == "rag-org"
    assert d.matched is None


def test_default_quarantine_for_unknown_provenance():
    d = route_path("misc/file.md", [], default_disposition=Disposition.QUARANTINE)
    assert d.disposition is Disposition.QUARANTINE
    assert d.matched is None


def test_nested_path_under_excluded_dir_is_excluded():
    rules = [ProvenanceRule("a/b/", Disposition.EXCLUDE)]
    assert route_path("a/b/c/d.pdf", rules).disposition is Disposition.EXCLUDE


# -- rule-set loading (consumer supplies a config) ----------------------------


def test_load_rules_from_dicts():
    from axiom.rag.ingest_router import load_rules

    rules = load_rules([
        {"pattern": "vendor/", "disposition": "exclude", "reason": "licensed"},
        {"pattern": "*.zip", "disposition": "quarantine"},
        {"pattern": "public/", "disposition": "allow", "tier": "rag-community"},
    ])
    assert rules[0].disposition is Disposition.EXCLUDE and rules[0].pattern == "vendor/"
    assert rules[1].disposition is Disposition.QUARANTINE and rules[1].tier is None
    assert rules[2].disposition is Disposition.ALLOW and rules[2].tier == "rag-community"


def test_load_rules_file_toml(tmp_path):
    import textwrap

    from axiom.rag.ingest_router import load_rules_file

    p = tmp_path / "rules.toml"
    p.write_text(
        textwrap.dedent(
            """
            [[rule]]
            pattern = "vendor/"
            disposition = "exclude"
            reason = "licensed vendor software"

            [[rule]]
            pattern = "public/"
            disposition = "allow"
            tier = "rag-community"
            """
        )
    )
    rules = load_rules_file(p)
    assert [r.pattern for r in rules] == ["vendor/", "public/"]
    assert rules[0].disposition is Disposition.EXCLUDE
    assert rules[1].tier == "rag-community"


def test_load_rules_rejects_unknown_disposition():
    import pytest

    from axiom.rag.ingest_router import load_rules

    with pytest.raises(ValueError):
        load_rules([{"pattern": "x/", "disposition": "nuke"}])


def test_load_rules_empty():
    from axiom.rag.ingest_router import load_rules

    assert load_rules([]) == []
    assert load_rules(None) == []


# -- auditing an existing corpus against the rules ----------------------------


def test_audit_flags_excluded_and_quarantined():
    from axiom.rag.ingest_router import audit_paths

    rules = [
        ProvenanceRule("vendor/", Disposition.EXCLUDE, reason="licensed"),
        ProvenanceRule("*.zip", Disposition.QUARANTINE, reason="archive"),
    ]
    paths = ["vendor/manual.pdf", "docs/intro.md", "data/bundle.zip", "public/ok.md"]
    report = audit_paths(paths, rules)
    assert report.total == 4
    flagged = {p: disp for p, disp, _ in report.flagged}
    assert flagged["vendor/manual.pdf"] is Disposition.EXCLUDE
    assert flagged["data/bundle.zip"] is Disposition.QUARANTINE
    assert "docs/intro.md" not in flagged
    assert report.excluded == 1
    assert report.quarantined == 1


def test_audit_clean_corpus_has_no_flags():
    from axiom.rag.ingest_router import audit_paths

    report = audit_paths(["a.md", "b.md"], [])
    assert report.flagged == []
    assert report.excluded == 0
    assert report.quarantined == 0
    assert report.total == 2
