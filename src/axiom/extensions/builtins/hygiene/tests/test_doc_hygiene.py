# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Doc-standards signals (docs/conventions/doc-standards.md).

Every check gets both a fixture that must trip it and a clean fixture
that must not — a check that cannot fail proves nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.hygiene.doc_hygiene import (
    audit_docs,
    check_adr_collisions,
    check_broken_links,
    check_filename_case,
    check_kind_prefixes,
    check_loose_root_files,
    check_missing_h1,
    check_retired_folders,
)
from axiom.extensions.builtins.hygiene.node_health import Severity


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    return tmp_path


def _mk(repo: Path, rel: str, text: str = "# Title\n\nbody\n") -> Path:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


# ---------------------------------------------------------------------------
# loose root files
# ---------------------------------------------------------------------------


def test_loose_root_file_flagged(repo: Path) -> None:
    _mk(repo, "docs/stray-design-note.md")
    findings = check_loose_root_files(repo)
    assert any("stray-design-note.md" in f.message for f in findings)


def test_readme_and_glossary_allowed_at_root(repo: Path) -> None:
    _mk(repo, "docs/README.md")
    _mk(repo, "docs/glossary.md")
    (repo / "docs/glossary-terms.toml").write_text("x = 1\n")
    assert check_loose_root_files(repo) == []


# ---------------------------------------------------------------------------
# kind prefixes
# ---------------------------------------------------------------------------


def test_wrong_prefix_in_kind_folder_flagged(repo: Path) -> None:
    _mk(repo, "docs/prds/design-notes.md")
    _mk(repo, "docs/specs/prd-misfiled.md")
    findings = check_kind_prefixes(repo)
    assert len(findings) == 2
    assert all(f.severity == Severity.WARNING for f in findings)


def test_correct_prefixes_pass(repo: Path) -> None:
    _mk(repo, "docs/prds/prd-widget.md")
    _mk(repo, "docs/specs/spec-widget.md")
    _mk(repo, "docs/adrs/adr-001-first-decision.md")
    _mk(repo, "docs/adrs/adr-002-a1-amendment.md")
    _mk(repo, "docs/prds/README.md")  # folder READMEs are exempt
    _mk(repo, "docs/specs/spec-aeos-0.1.md")  # version dots are legitimate
    assert check_kind_prefixes(repo) == []


def test_adr_without_number_flagged(repo: Path) -> None:
    _mk(repo, "docs/adrs/adr-widget-routing.md")
    findings = check_kind_prefixes(repo)
    assert len(findings) == 1


# ---------------------------------------------------------------------------
# filename casing
# ---------------------------------------------------------------------------


def test_shouty_and_underscore_names_flagged(repo: Path) -> None:
    _mk(repo, "docs/research/Concept_Draft.md")
    findings = check_filename_case(repo)
    assert len(findings) == 1


def test_scratch_and_archive_dirs_exempt_from_casing(repo: Path) -> None:
    _mk(repo, "docs/working/Messy_Draft.md")
    _mk(repo, "docs/_archive/OLD_PLAN.md")
    assert check_filename_case(repo) == []


# ---------------------------------------------------------------------------
# ADR number collisions
# ---------------------------------------------------------------------------


def test_duplicate_adr_number_flagged(repo: Path) -> None:
    _mk(repo, "docs/adrs/adr-023-first-topic.md")
    _mk(repo, "docs/adrs/adr-023-second-topic.md")
    findings = check_adr_collisions(repo)
    assert len(findings) == 1
    assert "023" in findings[0].message


def test_amendment_suffix_is_not_a_collision(repo: Path) -> None:
    _mk(repo, "docs/adrs/adr-023-first-topic.md")
    _mk(repo, "docs/adrs/adr-023-a1-amendment.md")
    assert check_adr_collisions(repo) == []


# ---------------------------------------------------------------------------
# retired folder names
# ---------------------------------------------------------------------------


def test_retired_folders_flagged(repo: Path) -> None:
    (repo / "docs/requirements").mkdir()
    (repo / "docs/tech-specs").mkdir()
    findings = check_retired_folders(repo)
    assert len(findings) == 2


def test_canonical_folders_pass(repo: Path) -> None:
    (repo / "docs/prds").mkdir()
    (repo / "docs/specs").mkdir()
    assert check_retired_folders(repo) == []


# ---------------------------------------------------------------------------
# missing H1
# ---------------------------------------------------------------------------


def test_kind_doc_without_h1_flagged(repo: Path) -> None:
    _mk(repo, "docs/prds/prd-widget.md", "**Bold, not a heading**\n\nbody\n")
    findings = check_missing_h1(repo)
    assert len(findings) == 1


def test_kind_doc_with_h1_passes(repo: Path) -> None:
    _mk(repo, "docs/prds/prd-widget.md", "# Widget PRD\n\nbody\n")
    assert check_missing_h1(repo) == []


# ---------------------------------------------------------------------------
# broken relative links
# ---------------------------------------------------------------------------


def test_broken_relative_link_flagged(repo: Path) -> None:
    _mk(repo, "docs/specs/spec-widget.md", "# S\n\n[gone](../prds/prd-gone.md)\n")
    findings = check_broken_links(repo)
    assert len(findings) == 1


def test_resolving_link_and_external_targets_pass(repo: Path) -> None:
    _mk(repo, "docs/prds/prd-widget.md")
    _mk(
        repo,
        "docs/specs/spec-widget.md",
        "# S\n\n[ok](../prds/prd-widget.md) [web](https://example.org/x.md)\n"
        "[sibling-repo](../../../other-repo/docs/specs/spec-far.md)\n",
    )
    assert check_broken_links(repo) == []


def test_archive_links_exempt(repo: Path) -> None:
    _mk(repo, "docs/_archive/old-plan.md", "# O\n\n[gone](./nothing.md)\n")
    assert check_broken_links(repo) == []


# ---------------------------------------------------------------------------
# aggregator
# ---------------------------------------------------------------------------


def test_audit_docs_runs_every_check(repo: Path) -> None:
    _mk(repo, "docs/stray.md")
    _mk(repo, "docs/prds/Wrong_Case.md")
    (repo / "docs/requirements").mkdir()
    checks = {f.check for f in audit_docs(repo)}
    assert "docs_loose_root_files" in checks
    assert "docs_retired_folders" in checks


def test_audit_docs_clean_repo_is_quiet(repo: Path) -> None:
    _mk(repo, "docs/README.md")
    _mk(repo, "docs/prds/prd-widget.md")
    _mk(repo, "docs/specs/spec-widget.md", "# S\n\n[ok](../prds/prd-widget.md)\n")
    assert audit_docs(repo) == []


def test_audit_docs_no_docs_dir_is_quiet(tmp_path: Path) -> None:
    assert audit_docs(tmp_path) == []
