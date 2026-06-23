# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axi rag ingest` verb: arg-branch units + CLI subprocess smokes (§10).

Unit tests cover the command's argument handling (no paths, unsupported target,
dry-run dispatch) without a DB. The subprocess smokes run the real entry point
(`python -m axiom.rag ingest …`) so entry-point wiring and flag parsing are
exercised end-to-end, per the project's "CLI subprocess smokes required" rule.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

import pytest

from axiom.rag.cli import cmd_ingest_advanced


def _args(**kw) -> argparse.Namespace:
    base = dict(
        paths=[],
        corpus="rag-internal",
        target="local",
        dry_run=False,
        resume=False,
        yes=False,
        json=False,
        max_retries=5,
        checkpoint_dir=".axi/rag-ingest",
        calibration_sample=50,
        rules=None,
        no_rules=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_ingest_requires_at_least_one_path():
    with pytest.raises(SystemExit) as ei:
        cmd_ingest_advanced(_args(paths=[]))
    assert ei.value.code == 2


def test_ingest_rejects_unsupported_federated_target(tmp_path):
    with pytest.raises(SystemExit) as ei:
        cmd_ingest_advanced(_args(paths=[str(tmp_path)], target="peer:east"))
    assert ei.value.code == 2


def test_ingest_dry_run_unreachable_exits_one(tmp_path, monkeypatch, capsys):
    (tmp_path / "a.md").write_text("x" * 1000)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as ei:
        cmd_ingest_advanced(_args(paths=[str(tmp_path)], corpus="rag-org", dry_run=True))
    assert ei.value.code == 1
    out = capsys.readouterr().out
    assert "Preflight" in out


# -- subprocess smokes ---------------------------------------------------------


def test_smoke_ingest_help_lists_flags():
    r = subprocess.run(
        [sys.executable, "-m", "axiom.rag", "ingest", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert r.returncode == 0
    for flag in ("--dry-run", "--resume", "--target", "--corpus", "--checkpoint-dir"):
        assert flag in r.stdout


def test_smoke_ingest_dry_run_prints_preflight(tmp_path):
    (tmp_path / "a.md").write_text("# Doc\n\nsome content for the preflight scan")
    env = {**os.environ}
    env.pop("DATABASE_URL", None)  # force the unreachable branch deterministically
    r = subprocess.run(
        [sys.executable, "-m", "axiom.rag", "ingest", str(tmp_path), "--dry-run", "--corpus", "rag-org"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert "Preflight" in (r.stdout + r.stderr)


def test_ingest_bad_rules_file_exits_two(tmp_path):
    bad = tmp_path / "rules.toml"
    bad.write_text('[[rule]]\npattern = "x/"\ndisposition = "nuke"\n')  # invalid disposition
    (tmp_path / "a.md").write_text("content")
    with pytest.raises(SystemExit) as ei:
        cmd_ingest_advanced(_args(paths=[str(tmp_path)], rules=str(bad), dry_run=True))
    assert ei.value.code == 2


# -- footgun guard: shared-tier ingest requires provenance rules ---------------


def test_live_shared_tier_without_rules_refuses(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("content")
    monkeypatch.delenv("AXIOM_RAG_RULES", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as ei:
        cmd_ingest_advanced(_args(paths=[str(tmp_path)], corpus="rag-org"))  # live, no rules
    assert ei.value.code == 2  # refused before any store work


def test_live_shared_tier_with_no_rules_flag_proceeds(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("content")
    monkeypatch.delenv("AXIOM_RAG_RULES", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as ei:
        cmd_ingest_advanced(_args(paths=[str(tmp_path)], corpus="rag-org", no_rules=True))
    assert ei.value.code == 1  # guard passed; aborts later on unreachable store


def test_live_shared_tier_with_env_rules_proceeds(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("content")
    rules = tmp_path / "rules.toml"
    rules.write_text('[[rule]]\npattern = "x/"\ndisposition = "exclude"\n')
    monkeypatch.setenv("AXIOM_RAG_RULES", str(rules))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as ei:
        cmd_ingest_advanced(_args(paths=[str(tmp_path)], corpus="rag-org"))
    assert ei.value.code == 1  # rules resolved from env; guard passed; unreachable store


def test_live_internal_tier_without_rules_is_allowed(tmp_path, monkeypatch):
    (tmp_path / "a.md").write_text("content")
    monkeypatch.delenv("AXIOM_RAG_RULES", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit) as ei:
        cmd_ingest_advanced(_args(paths=[str(tmp_path)], corpus="rag-internal"))
    assert ei.value.code == 1  # personal tier: no guard; aborts on unreachable store


# -- `axi rag audit` verb ------------------------------------------------------


class _AuditStore:
    def __init__(self, paths):
        self._paths = list(paths)
        self.deleted: list[str] = []

    def list_document_paths(self, corpus):
        return self._paths

    def delete_document(self, path, corpus):
        self.deleted.append(path)

    def close(self):
        pass


def test_cmd_audit_reports_flagged(tmp_path, capsys):
    from unittest.mock import patch

    from axiom.rag import cli as rag_cli

    rules = tmp_path / "r.toml"
    rules.write_text('[[rule]]\npattern = "vendor/"\ndisposition = "exclude"\n')
    store = _AuditStore(["vendor/m.pdf", "docs/ok.md"])
    with patch("axiom.rag.cli._get_store", return_value=store):
        rag_cli.main(["audit", "--corpus", "rag-org", "--rules", str(rules)])
    out = capsys.readouterr().out
    assert "vendor/m.pdf" in out
    assert "EXCLUDE" in out
    assert store.deleted == []  # read-only without --purge


def test_cmd_audit_purge_with_yes_deletes_excluded(tmp_path):
    from unittest.mock import patch

    from axiom.rag import cli as rag_cli

    rules = tmp_path / "r.toml"
    rules.write_text('[[rule]]\npattern = "vendor/"\ndisposition = "exclude"\n')
    store = _AuditStore(["vendor/m.pdf", "docs/ok.md"])
    with patch("axiom.rag.cli._get_store", return_value=store):
        rag_cli.main(["audit", "--corpus", "rag-org", "--rules", str(rules), "--purge", "--yes"])
    assert store.deleted == ["vendor/m.pdf"]  # only the EXCLUDE-flagged doc
