# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``state reconcile`` -- bring a retired state folder into the one store (#2010).

Every CLI name now reads ``~/.axi`` and ``<repo>/.axi``. A node that ran a
product's CLI before that has state in the folder the product's name produced.
These tests drive the skill and the CLI against a temporary HOME and project
only; nothing here reads or writes a real home folder.
"""

from __future__ import annotations

import json
import logging
import os
import tomllib
from pathlib import Path

import pytest

from axiom.extensions.builtins.state import cli as state_cli
from axiom.extensions.builtins.state import skills as state_skills
from axiom.infra.brand_migration import LEGACY_PROJECT_DIR
from axiom.infra.skills import SkillContext, SkillRegistry

OLD = LEGACY_PROJECT_DIR  # the retired folder name, as a fixture


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A temporary HOME and project; both state folders resolve inside them."""
    home = tmp_path / "home"
    project = home / "repo"
    (project / ".git").mkdir(parents=True)
    monkeypatch.delenv("AXI_STATE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("AXIOM_ROOT", str(project))
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.setenv("AXIOM_SNAPSHOT_BEFORE", "0")
    monkeypatch.delenv("AXIOM_DB_URL", raising=False)
    assert str(home).startswith(str(tmp_path))
    return home, project


def _write(path: Path, text: str, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _reconcile(**params):
    reg = SkillRegistry()
    state_skills.bind(reg)
    from axiom.infra.paths import get_user_state_dir

    ctx = SkillContext(registry=reg, state_dir=get_user_state_dir(), logger=logging.getLogger("t"))
    return reg.invoke("state.reconcile", params, ctx)


def _seed(home: Path, project: Path) -> None:
    """Both folders, with every kind of difference the reconcile handles."""
    # user scope
    _write(home / OLD / "only-old.json", '{"a": 1}')
    _write(home / OLD / "agents" / "nested.txt", "deep")
    _write(home / OLD / "same.txt", "identical")
    _write(home / ".axi" / "same.txt", "identical")
    _write(home / OLD / "notes.md", "old notes")
    _write(home / ".axi" / "notes.md", "new notes")
    _write(
        home / OLD / "settings.toml",
        '[routing]\ndefault_mode = "cloud"\n[user]\norg = "old.example"\nname = "Ada"\n',
        mtime=2_000_000_000,  # newer
    )
    _write(
        home / ".axi" / "settings.toml",
        '[routing]\ndefault_mode = "local"\n[user]\norg = "new.example"\nemail = "a@b.c"\n',
        mtime=1_000_000_000,  # older
    )
    # project scope
    _write(project / OLD / "settings.toml", 'theme = "dark"\n')


@pytest.mark.acceptance("2010-A4")
def test_a_dry_run_changes_nothing_and_lists_every_action(node):
    home, project = node
    _seed(home, project)
    before = (_tree(home / OLD), _tree(home / ".axi"), _tree(project / OLD))

    r = _reconcile()

    assert r.ok, r.errors
    assert (_tree(home / OLD), _tree(home / ".axi"), _tree(project / OLD)) == before
    assert not (project / ".axi").exists(), "a dry run created the project folder"
    v = r.value
    assert v["applied"] is False
    actions = {
        (f["scope"], a["path"]): a["action"] for f in v["folders"] for a in f["actions"]
    }
    assert actions == {
        ("user", "only-old.json"): "copy",
        ("user", "agents/nested.txt"): "copy",
        ("user", "same.txt"): "same",
        ("user", "notes.md"): "keep-both",
        ("user", "settings.toml"): "merge",
        ("project", "settings.toml"): "copy",
    }


@pytest.mark.acceptance("2010-A5")
def test_apply_with_conflicting_settings_loses_no_value(node):
    home, project = node
    _seed(home, project)

    r = _reconcile(apply=True)

    assert r.ok, r.errors
    merged = tomllib.loads((home / ".axi" / "settings.toml").read_text())
    # newest file wins each conflict by default; the old folder's file is newer
    assert merged["routing"]["default_mode"] == "cloud"
    assert merged["user"]["org"] == "old.example"
    # keys on one side only are kept from both
    assert merged["user"]["name"] == "Ada"
    assert merged["user"]["email"] == "a@b.c"

    # every conflict is reported with both values and which was kept
    conflicts = {c["key"]: c for f in r.value["folders"] for a in f["actions"]
                 for c in a.get("conflicts", [])}
    assert set(conflicts) == {"routing.default_mode", "user.org"}
    assert conflicts["routing.default_mode"]["current"] == "local"
    assert conflicts["routing.default_mode"]["legacy"] == "cloud"
    assert conflicts["routing.default_mode"]["kept"] == "legacy"

    # the losing value is recoverable from the archive
    archive = Path(r.value["archive"])
    archived = (archive / "user" / "current" / "settings.toml").read_text()
    assert 'default_mode = "local"' in archived
    assert (archive / "user" / "legacy" / "settings.toml").exists()


@pytest.mark.acceptance("2010-A5")
def test_prefer_one_side_decides_every_conflict(node):
    home, project = node
    _seed(home, project)

    r = _reconcile(apply=True, prefer="current")

    assert r.ok, r.errors
    merged = tomllib.loads((home / ".axi" / "settings.toml").read_text())
    assert merged["routing"]["default_mode"] == "local"
    assert merged["user"]["org"] == "new.example"
    assert merged["user"]["name"] == "Ada", "a key only in the old folder is still brought across"


@pytest.mark.acceptance("2010-A6")
def test_a_file_only_in_the_old_folder_is_copied(node):
    home, project = node
    _seed(home, project)

    assert _reconcile(apply=True).ok

    assert json.loads((home / ".axi" / "only-old.json").read_text()) == {"a": 1}
    assert (home / ".axi" / "agents" / "nested.txt").read_text() == "deep"
    assert tomllib.loads((project / ".axi" / "settings.toml").read_text()) == {"theme": "dark"}


def test_a_differing_file_that_is_not_settings_keeps_both(node):
    home, project = node
    _seed(home, project)

    assert _reconcile(apply=True).ok

    assert (home / ".axi" / "notes.md").read_text() == "new notes"
    assert (home / ".axi" / f"notes.md.from-{OLD[1:]}").read_text() == "old notes"


@pytest.mark.acceptance("2010-A7")
def test_the_old_folder_still_exists_after_apply_and_a_marker_is_written(node):
    home, project = node
    _seed(home, project)
    before = _tree(home / OLD)

    r = _reconcile(apply=True)

    assert r.ok, r.errors
    assert (home / OLD).is_dir() and (project / OLD).is_dir()
    assert _tree(home / OLD) == before, "the old folder was changed"
    marker = json.loads((home / ".axi" / f"reconciled-from-{OLD[1:]}.json").read_text())
    assert marker["archive"] == r.value["archive"]
    assert (project / ".axi" / f"reconciled-from-{OLD[1:]}.json").exists()
    assert "no longer read" in r.value["notice"]


def test_a_second_apply_changes_nothing_more(node):
    home, project = node
    _seed(home, project)
    assert _reconcile(apply=True).ok
    after_first = _tree(home / ".axi")

    r = _reconcile()

    assert r.ok
    pending = [a for f in r.value["folders"] for a in f["actions"]
               if a["action"] in ("copy", "keep-both") or a.get("added")]
    assert not pending, pending
    assert _tree(home / ".axi") == after_first


def test_a_registered_product_folder_is_brought_across_too(node):
    """The folder a registered product's own CLI name produced, not only the
    retired name: a decision recorded there (agent consent, say) is not lost."""
    from axiom.infra import branding

    home, _project = node
    _write(home / ".acme" / "agents_consent.json", '{"enabled": ["hygiene"]}')
    branding.register(branding.BrandingConfig(cli_name="acme"))
    try:
        r = _reconcile(apply=True)
    finally:
        branding.reset()

    assert r.ok, r.errors
    assert json.loads((home / ".axi" / "agents_consent.json").read_text()) == {"enabled": ["hygiene"]}
    assert (home / ".acme").is_dir()


def test_only_an_apply_is_journaled(node):
    """A dry run reads; only the apply changes state, so only it gets an audit row."""
    home, project = node
    _seed(home, project)
    journal = home / ".axi" / "audit" / "actions.jsonl"

    assert _reconcile().ok
    assert not journal.exists(), "a dry run was journaled"

    assert _reconcile(apply=True).ok
    rows = [json.loads(line) for line in journal.read_text().splitlines() if line.strip()]
    assert [r["skill"] for r in rows] == ["state.reconcile"]
    assert rows[0]["outcome"] == "ok"


class _Tty:
    def __init__(self, tty: bool = True):
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def _notice(home, argv=("status",), tty=True, now=None):
    import io

    from axiom.extensions.builtins.state import reconcile as rc

    err = io.StringIO()
    shown = rc.startup_notice(list(argv), stdout=_Tty(tty), stderr=err, now=now)
    return shown, err.getvalue()


@pytest.mark.acceptance("2010-A8")
def test_startup_says_once_a_day_that_an_unreconciled_folder_is_not_read(node):
    home, _project = node
    _write(home / OLD / "settings.toml", 'a = 1\n')

    shown, err = _notice(home, now=1_000_000.0)
    assert shown and err.strip() == (
        f"Settings in ~/{OLD} are no longer read; run `axi state reconcile` to bring them across."
    )
    assert _notice(home, now=1_000_000.0 + 3600)[0] is None, "shown twice in one day"
    assert _notice(home, now=1_000_000.0 + 86_401)[0], "not shown again the next day"


@pytest.mark.acceptance("2010-A8")
def test_startup_notice_is_never_in_json_or_when_stdout_is_not_a_terminal(node):
    home, _project = node
    _write(home / OLD / "settings.toml", 'a = 1\n')

    assert _notice(home, argv=("status", "--json"))[1] == ""
    assert _notice(home, tty=False)[1] == ""


@pytest.mark.acceptance("2010-A8")
def test_startup_notice_is_silent_once_reconciled_or_with_no_old_folder(node):
    home, project = node
    assert _notice(home)[0] is None
    _seed(home, project)
    assert _reconcile(apply=True).ok
    assert _notice(home)[0] is None


def test_startup_notice_names_a_registered_product_folder_and_its_cli(node):
    from axiom.infra import branding

    home, _project = node
    _write(home / ".acme" / "settings.toml", 'a = 1\n')
    branding.register(branding.BrandingConfig(cli_name="acme"))
    try:
        shown, err = _notice(home)
    finally:
        branding.reset()
    assert "~/.acme" in err and "`acme state reconcile`" in err


@pytest.mark.acceptance("2010-A8")
def test_every_cli_invocation_asks_for_the_startup_notice(node, monkeypatch):
    """The notice is only useful if the CLI entry point actually asks for it."""
    import sys

    from axiom import axiom_cli
    from axiom.extensions.builtins.state import reconcile as rc

    seen = []
    monkeypatch.setattr(rc, "startup_notice", lambda argv, **kw: seen.append(list(argv)))
    for hook in ("_show_pending_changelog", "_check_and_prompt_update", "_self_heal_daemon_agents",
                 "print_usage"):
        monkeypatch.setattr(axiom_cli, hook, lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["axi"])

    with pytest.raises(SystemExit):
        axiom_cli.main()

    assert seen == [[]]


def test_nothing_to_do_when_there_is_no_old_folder(node):
    r = _reconcile()
    assert r.ok
    assert r.value["folders"] == []


def test_the_cli_is_a_dry_run_by_default_and_prints_each_action(node, capsys):
    home, project = node
    _seed(home, project)

    assert state_cli.main(["reconcile"]) == 0
    out = capsys.readouterr().out

    assert "DRY RUN" in out
    for path in ("only-old.json", "agents/nested.txt", "notes.md", "settings.toml"):
        assert path in out
    assert "routing.default_mode" in out and "'local'" in out and "'cloud'" in out
    assert not (home / ".axi" / "only-old.json").exists()


def test_the_cli_apply_prints_the_archive_and_the_notice(node, capsys):
    home, project = node
    _seed(home, project)

    assert state_cli.main(["reconcile", "--apply", "--prefer", "legacy"]) == 0
    out = capsys.readouterr().out

    assert "archive" in out and "no longer read" in out
    assert (home / ".axi" / "only-old.json").exists()


def test_reconcile_is_registered_and_declared():
    import tomllib as _t

    reg = SkillRegistry()
    state_skills.bind(reg)
    assert reg.has("state.reconcile")
    assert "mcp" not in (reg.spec("state.reconcile").surfaces or ())
    manifest = Path(state_skills.__file__).parents[1] / "axiom-extension.toml"
    m = _t.loads(manifest.read_text())
    cmd = next(p for p in m["extension"]["provides"] if p["kind"] == "cmd")
    assert "reconcile" in cmd["subcommands"]
