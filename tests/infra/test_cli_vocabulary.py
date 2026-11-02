# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axiom.infra.cli_vocabulary`` — the canonical noun/verb/flag
surface and the markdown linter that consumes it.
"""

from __future__ import annotations

import argparse

from axiom.infra.cli_vocabulary import (
    NounSpec,
    VerbSpec,
    Vocabulary,
    _walk_subparsers,
    lint_markdown,
)


def _build_vocab() -> Vocabulary:
    """Hand-built vocabulary so tests don't depend on the live extension
    registry."""
    return Vocabulary(nouns={
        "data": NounSpec(
            name="data",
            shorts=("d",),
            verbs={
                "install": VerbSpec(
                    name="install",
                    flags=("--db-password-ref", "--kube-context", "--namespace"),
                ),
                "ingest": VerbSpec(
                    name="ingest",
                    flags=("--connector", "--since"),
                ),
            },
        ),
        "publish": NounSpec(
            name="publish",
            shorts=("pub", "p"),
            verbs={
                "push": VerbSpec(
                    name="push",
                    flags=("--endpoint", "--headed", "--force", "--all"),
                    positionals=("path",),
                ),
            },
        ),
        "audit": NounSpec(
            name="audit",
            verbs={
                "list": VerbSpec(name="list", flags=("--since", "--actor")),
            },
        ),
    })


# ---------------------------------------------------------------------------
# vocabulary resolution
# ---------------------------------------------------------------------------


class TestVocabulary:
    def test_has_noun_canonical(self):
        v = _build_vocab()
        assert v.has_noun("data")
        assert v.has_noun("publish")

    def test_has_noun_via_short(self):
        v = _build_vocab()
        assert v.has_noun("d")
        assert v.has_noun("pub")
        assert v.has_noun("p")

    def test_unknown_noun(self):
        v = _build_vocab()
        assert not v.has_noun("doesnotexist")
        assert v.resolve_noun("doesnotexist") is None

    def test_resolve_short_returns_canonical_spec(self):
        v = _build_vocab()
        assert v.resolve_noun("pub").name == "publish"
        assert v.resolve_noun("d").name == "data"


# ---------------------------------------------------------------------------
# argparse introspection
# ---------------------------------------------------------------------------


class TestWalkSubparsers:
    def test_extracts_verbs_and_flags(self):
        p = argparse.ArgumentParser()
        sub = p.add_subparsers(dest="verb")
        install = sub.add_parser("install")
        install.add_argument("--ns")
        install.add_argument("--pwd-ref", "-p")
        ingest = sub.add_parser("ingest")
        ingest.add_argument("--connector", required=True)
        ingest.add_argument("path")

        verbs = _walk_subparsers(p)

        assert set(verbs) == {"install", "ingest"}
        assert "--ns" in verbs["install"].flags
        assert "--pwd-ref" in verbs["install"].flags
        assert "-p" in verbs["install"].short_flags
        assert "--connector" in verbs["ingest"].flags
        assert verbs["ingest"].positionals == ("path",)


# ---------------------------------------------------------------------------
# markdown linter
# ---------------------------------------------------------------------------


class TestLintMarkdown:
    def test_clean_runbook_no_findings(self):
        v = _build_vocab()
        text = r"""
\`\`\`bash
axi data install --kube-context k3d-axi-local --namespace axiom-data
axi audit list --since 24h --actor @op:org
\`\`\`
"""
        findings = lint_markdown(text, vocab=v)
        assert findings == []

    def test_unknown_noun_flagged(self):
        v = _build_vocab()
        text = "```bash\naxi pub2 push --endpoint box\n```\n"
        f = lint_markdown(text, vocab=v)
        assert len(f) == 1
        assert f[0].issue == "unknown_noun"
        assert "pub2" in f[0].detail

    def test_short_noun_resolves_no_finding(self):
        """`pub` is a registered short for `publish`; should not be flagged."""
        v = _build_vocab()
        text = "```bash\naxi pub push --endpoint box-browser /tmp/x.txt\n```\n"
        f = lint_markdown(text, vocab=v)
        assert f == []

    def test_unknown_verb_flagged(self):
        v = _build_vocab()
        text = "```bash\naxi data nonexistent --whatever\n```\n"
        f = lint_markdown(text, vocab=v)
        assert any(x.issue == "unknown_verb" for x in f)

    def test_unknown_flag_flagged(self):
        v = _build_vocab()
        text = "```bash\naxi data install --bogus-flag X\n```\n"
        f = lint_markdown(text, vocab=v)
        assert any(x.issue == "unknown_flag" for x in f)
        assert any("--bogus-flag" in x.detail for x in f)

    def test_runbook_drift_storage_vs_endpoint(self):
        """The exact drift from the 2026-05-31 stand-up:
        runbook said `--storage`; CLI uses `--endpoint`. Linter should
        have caught this."""
        v = _build_vocab()
        text = "```bash\naxi publish push --storage box-browser --headed any-file.txt\n```\n"
        f = lint_markdown(text, vocab=v)
        assert any(x.issue == "unknown_flag" and "--storage" in x.detail for x in f), \
            f"expected --storage to be flagged as unknown_flag; got: {f}"

    def test_flag_with_equals_value(self):
        v = _build_vocab()
        text = "```bash\naxi data install --kube-context=k3d-axi-local\n```\n"
        f = lint_markdown(text, vocab=v)
        assert f == []

    def test_lines_outside_bash_blocks_ignored(self):
        v = _build_vocab()
        text = "Some prose that mentions `axi bogus-noun` inline.\n"
        f = lint_markdown(text, vocab=v)
        assert f == []

    def test_ignores_non_axi_lines(self):
        v = _build_vocab()
        text = "```bash\nls -la\nkubectl get pods\necho hello\n```\n"
        f = lint_markdown(text, vocab=v)
        assert f == []
