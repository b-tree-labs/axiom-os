# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A logbook an installed extension declares is a logbook the node has.

Discovery used to scan Axiom's own builtins and nothing else, and nothing
called it with any other root. A consumer could declare a logbook, ship it,
and pass its own tests (which call the loader directly), and the node it was
installed on would still answer "no logbook" — because the node never looked.
The roots are now the node's extension directories, the same ones every
other extension kind is found in.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from .. import registry
from ..logbooks import LogbookError

DEMO = Path(__file__).parent / "logbooks" / "demo_log.toml"


def _extension(root: Path, name: str, *, logbook_id: str = "demo_log") -> Path:
    """An extension under ``root`` declaring one logbook."""
    ext = root / name
    (ext / "logbook").mkdir(parents=True)
    text = DEMO.read_text(encoding="utf-8")
    text = text.replace('id = "demo_log"', f'id = "{logbook_id}"', 1)
    (ext / "logbook" / f"{logbook_id}.toml").write_text(text, encoding="utf-8")
    (ext / "axiom-extension.toml").write_text(
        "[extension]\n"
        f'name = "{name}"\n'
        "\n[[extension.provides]]\n"
        'kind = "logbook"\n'
        f'name = "{logbook_id}"\n'
        f'file = "logbook/{logbook_id}.toml"\n',
        encoding="utf-8",
    )
    return ext


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(registry, "_logbooks", {})
    monkeypatch.setattr(registry, "_discovered", False)
    yield


def test_the_fixture_declares_the_id_the_test_rewrites():
    """The rewrite below is a string replace; if the fixture's id line moved,
    every test here would silently test the original id instead."""
    assert 'id = "demo_log"' in DEMO.read_text(encoding="utf-8")


def test_a_logbook_from_an_installed_extension_is_found(fresh, tmp_path, monkeypatch):
    installed = tmp_path / "consumer" / "extensions" / "builtins"
    _extension(installed, "shift_log_ext", logbook_id="shift_log")
    monkeypatch.setattr(registry, "_extension_roots", lambda: [installed, registry._BUILTINS])

    ids = {b.id for b in registry.all_logbooks()}

    assert "shift_log" in ids


def test_discovery_reads_the_nodes_extension_dirs_by_default(monkeypatch):
    """The default roots ARE the node's extension directories — not a copy
    of the list that could drift from it."""
    sentinel = [Path("/nonexistent/a"), Path("/nonexistent/b")]
    from axiom.extensions import discovery

    monkeypatch.setattr(discovery, "get_extension_dirs", lambda: sentinel)
    assert registry._extension_roots() == sentinel


def test_an_earlier_root_overrides_a_later_extension_of_the_same_name(fresh, tmp_path, monkeypatch):
    """The node's precedence rule: an extension in an earlier root replaces a
    same-named one in a later root. An override is not a duplicate."""
    early, late = tmp_path / "early", tmp_path / "late"
    _extension(early, "logs")
    _extension(late, "logs")
    monkeypatch.setattr(registry, "_extension_roots", lambda: [early, late])

    books = registry.all_logbooks()

    assert [b.id for b in books] == ["demo_log"]
    assert str(early) in str(books[0].source)


def test_two_different_extensions_claiming_one_id_still_refuse(fresh, tmp_path, monkeypatch):
    """Ids are unique per node. Two extensions that are not overrides of each
    other both declaring one id is a conflict, and it fails loudly."""
    root = tmp_path / "root"
    _extension(root, "one")
    _extension(root, "two")
    monkeypatch.setattr(registry, "_extension_roots", lambda: [root])

    with pytest.raises(LogbookError, match="declared twice"):
        registry.all_logbooks()


def test_a_root_that_does_not_exist_is_skipped(fresh, tmp_path, monkeypatch):
    missing = tmp_path / "gone"
    root = tmp_path / "root"
    _extension(root, "logs")
    monkeypatch.setattr(registry, "_extension_roots", lambda: [missing, root])

    assert [b.id for b in registry.all_logbooks()] == ["demo_log"]


def test_explicit_roots_still_win(fresh, tmp_path):
    root = tmp_path / "root"
    _extension(root, "logs", logbook_id="only_this")
    registry.discover([root])
    assert [b.id for b in registry.all_logbooks()] == ["only_this"]
    shutil.rmtree(root)
