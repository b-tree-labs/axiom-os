# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the `context` extension — AGENTS.md → per-tool context fan-out.

Per ADR-051: AGENTS.md is the single canonical project-context file; every
other assistant's context file (Cursor, JetBrains Junie, Copilot) is
*generated* from it and kept honest by a drift check. These tests pin the
generated formats, the sync/check/init behaviors, and the CLI wiring.
"""

from __future__ import annotations

from pathlib import Path


from axiom.extensions.builtins.context import core, generators

SAMPLE = "# My Project\n\nRule one.\nRule two.\n"


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def test_three_targets_registered():
    by_name = {t.name: t for t in generators.TARGETS}
    assert set(by_name) == {"cursor", "junie", "copilot"}
    assert by_name["cursor"].relpath == ".cursor/rules/axiom.mdc"
    assert by_name["junie"].relpath == ".junie/guidelines.md"
    assert by_name["copilot"].relpath == ".github/copilot-instructions.md"


def test_every_target_carries_do_not_edit_marker_and_body():
    for t in generators.TARGETS:
        out = t.render(SAMPLE)
        assert generators.GENERATED_MARKER in out, f"{t.name} missing marker"
        assert "Rule one." in out and "Rule two." in out, f"{t.name} missing body"


def test_cursor_target_has_mdc_frontmatter():
    out = {t.name: t for t in generators.TARGETS}["cursor"].render(SAMPLE)
    assert out.startswith("---\n")
    assert "alwaysApply: true" in out.split("---", 2)[1]


def test_markdown_targets_have_no_frontmatter():
    for name in ("junie", "copilot"):
        out = {t.name: t for t in generators.TARGETS}[name].render(SAMPLE)
        assert not out.startswith("---\n")
        assert out.lstrip().startswith("<!--")  # html-comment marker


def test_render_is_deterministic():
    t = generators.TARGETS[0]
    assert t.render(SAMPLE) == t.render(SAMPLE)


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def _repo_with_canonical(tmp_path: Path) -> Path:
    (tmp_path / "AGENTS.md").write_text(SAMPLE, encoding="utf-8")
    return tmp_path


def test_sync_writes_all_three_target_files(tmp_path):
    root = _repo_with_canonical(tmp_path)
    results = core.sync(root, write=True)

    assert {r.status for r in results} == {"created"}
    for t in generators.TARGETS:
        dest = root / t.relpath
        assert dest.exists(), f"{t.name} not written"
        assert generators.GENERATED_MARKER in dest.read_text(encoding="utf-8")


def test_sync_is_idempotent(tmp_path):
    root = _repo_with_canonical(tmp_path)
    core.sync(root, write=True)
    second = core.sync(root, write=True)
    assert {r.status for r in second} == {"unchanged"}


def test_sync_rewrites_when_canonical_changes(tmp_path):
    root = _repo_with_canonical(tmp_path)
    core.sync(root, write=True)
    (root / "AGENTS.md").write_text(SAMPLE + "\nRule three.\n", encoding="utf-8")
    results = core.sync(root, write=True)
    assert {r.status for r in results} == {"written"}
    assert "Rule three." in (root / ".junie/guidelines.md").read_text()


def test_sync_dry_run_does_not_write(tmp_path):
    root = _repo_with_canonical(tmp_path)
    results = core.sync(root, write=False)
    assert {r.status for r in results} == {"created"}  # would create
    assert not (root / ".junie/guidelines.md").exists()  # but didn't


def test_sync_without_canonical_returns_empty(tmp_path):
    assert core.sync(tmp_path, write=True) == []


# ---------------------------------------------------------------------------
# check (drift)
# ---------------------------------------------------------------------------


def test_check_ok_when_in_sync(tmp_path):
    root = _repo_with_canonical(tmp_path)
    core.sync(root, write=True)
    results = core.check(root)
    assert {r.status for r in results} == {"ok"}
    assert core.has_drift(results) is False


def test_check_reports_missing_target(tmp_path):
    root = _repo_with_canonical(tmp_path)
    core.sync(root, write=True)
    (root / ".junie/guidelines.md").unlink()
    results = core.check(root)
    by = {r.target: r.status for r in results}
    assert by["junie"] == "missing"
    assert core.has_drift(results) is True


def test_check_detects_hand_edit_drift(tmp_path):
    root = _repo_with_canonical(tmp_path)
    core.sync(root, write=True)
    (root / ".github/copilot-instructions.md").write_text(
        "someone hand-edited this\n", encoding="utf-8"
    )
    results = core.check(root)
    by = {r.target: r.status for r in results}
    assert by["copilot"] == "drift"
    assert core.has_drift(results) is True


# ---------------------------------------------------------------------------
# init (adoption)
# ---------------------------------------------------------------------------


def test_init_scaffolds_canonical_when_absent(tmp_path):
    res = core.init(tmp_path)
    canonical = tmp_path / "AGENTS.md"
    assert canonical.exists()
    assert res.created_canonical is True
    # Post-init the repo is fully in sync.
    assert core.has_drift(core.check(tmp_path)) is False


def test_init_does_not_clobber_existing_canonical(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Hand authored\n\nKeep me.\n", encoding="utf-8")
    res = core.init(tmp_path)
    assert res.created_canonical is False
    assert "Keep me." in (tmp_path / "AGENTS.md").read_text()


def test_init_promotes_existing_real_claude_md(tmp_path):
    """A repo with a hand-authored CLAUDE.md and no AGENTS.md should have its
    CLAUDE.md *promoted* to canonical AGENTS.md, not scaffolded with a starter."""
    real = "# My Project\n\nLots of real hand-authored context.\n"
    (tmp_path / "CLAUDE.md").write_text(real, encoding="utf-8")

    res = core.init(tmp_path)

    # CLAUDE.md content became the canonical AGENTS.md (not the starter).
    assert (tmp_path / "AGENTS.md").read_text() == real
    assert res.promoted_from_claude is True
    assert res.created_canonical is False  # promoted, not scaffolded
    # CLAUDE.md is now a symlink back to AGENTS.md.
    claude = tmp_path / "CLAUDE.md"
    assert claude.is_symlink()
    assert Path(claude.readlink()).name == "AGENTS.md"
    # Generated files derive from the real content, and the repo is in sync.
    assert "real hand-authored context" in (tmp_path / ".junie/guidelines.md").read_text()
    assert core.has_drift(core.check(tmp_path)) is False


def test_init_does_not_promote_when_agents_already_exists(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Canonical\n", encoding="utf-8")
    (tmp_path / "CLAUDE.md").write_text("# stale separate file\n", encoding="utf-8")
    res = core.init(tmp_path)
    assert res.promoted_from_claude is False
    assert (tmp_path / "AGENTS.md").read_text() == "# Canonical\n"  # untouched


def test_init_creates_claude_symlink_to_agents(tmp_path):
    core.init(tmp_path)
    claude = tmp_path / "CLAUDE.md"
    assert claude.is_symlink()
    assert Path(claude.readlink()).name == "AGENTS.md"


def test_init_is_idempotent(tmp_path):
    core.init(tmp_path)
    res2 = core.init(tmp_path)
    assert res2.created_canonical is False
    assert core.has_drift(core.check(tmp_path)) is False


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# findings (discovery, ADR-051 §E)
# ---------------------------------------------------------------------------


def test_findings_empty_when_in_sync(tmp_path):
    root = _repo_with_canonical(tmp_path)
    core.sync(root, write=True)
    assert core.findings(root) == []


def test_findings_flag_uninitialized_repo(tmp_path):
    found = core.findings(tmp_path)
    assert [f.code for f in found] == ["context.uninitialized"]
    assert "axi context init" in found[0].remediation


def test_findings_flag_drift_with_remediation(tmp_path):
    root = _repo_with_canonical(tmp_path)
    core.sync(root, write=True)
    (root / ".junie/guidelines.md").unlink()  # missing
    (root / ".cursor/rules/axiom.mdc").write_text("tampered\n", encoding="utf-8")  # drift
    codes = {f.code for f in core.findings(root)}
    assert codes == {"context.missing", "context.drift"}
    assert all("axi context sync" in f.remediation for f in core.findings(root))


def test_cli_parser_has_verbs():
    from axiom.extensions.builtins.context.cli import build_parser

    for verb in ("sync", "check", "init", "status"):
        args = build_parser().parse_args([verb])
        assert args.action == verb


def test_cli_check_exit_code_subprocess(tmp_path):
    """E2E: run the real `axi context` entry point; check exits non-zero on drift.

    Points PYTHONPATH at the source under test so it does not depend on
    whatever may be installed in site-packages (non-editable installs lag).
    """
    import json
    import os
    import subprocess
    import sys

    import axiom

    root = _repo_with_canonical(tmp_path)
    core.sync(root, write=True)

    env = dict(os.environ)
    env["PYTHONPATH"] = (
        str(Path(axiom.__file__).resolve().parents[1])
        + os.pathsep
        + env.get("PYTHONPATH", "")
    )

    def run(verb, *extra):
        return subprocess.run(
            [sys.executable, "-m", "axiom.extensions.builtins.context.cli",
             verb, "--root", str(root), *extra],
            capture_output=True, text=True, timeout=60, env=env,
        )

    # In sync → exit 0.
    assert run("check").returncode == 0
    # Introduce drift → exit non-zero.
    (root / ".cursor/rules/axiom.mdc").write_text("tampered\n", encoding="utf-8")
    bad = run("check")
    assert bad.returncode != 0
    # `sync` repairs it; check passes again.
    assert run("sync").returncode == 0
    assert run("check").returncode == 0
    # json output is parseable.
    payload = json.loads(run("check", "--format", "json").stdout)
    assert {r["target"] for r in payload} == {"cursor", "junie", "copilot"}
