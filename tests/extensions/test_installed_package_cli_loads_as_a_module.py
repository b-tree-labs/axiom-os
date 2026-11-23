# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An installed extension's CLI is imported from its package, not loaded by path (#2008).

``axi ext install`` pip-installs an extension's package and links its folder
for discovery. The dispatcher loaded the CLI entry from a file under that
folder unless the manifest said ``builtin = true``, and a module loaded by
path has no parent package, so its relative imports broke. Contributors set
``builtin = true`` to get the import path, which says the extension shipped
with Axiom when it did not.

Acceptance rows: PK-1 an installed package (``builtin = false``) loads and
dispatches with relative imports working, for its noun and for a verb it
contributes under another noun; PK-2 a folder extension still loads by path.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from axiom.extensions import discovery as d

BUILTINS = Path(d.__file__).resolve().parent / "builtins"

_COMMANDS = '''
from ..helper import VALUE
from axiom.infra.skills import SkillResult


def main():
    print(f"noun says {VALUE}")
    return 0


def parsers(params, ctx):
    return SkillResult(ok=True, value=f"verb says {VALUE}")
'''


def _installed_package_extension(tmp_path: Path, monkeypatch, pkg: str) -> Path:
    """A folder extension whose package is also installed elsewhere on sys.path, as pip does."""
    folder = tmp_path / "extensions" / pkg
    src = folder / pkg
    (src / "commands").mkdir(parents=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "helper.py").write_text('VALUE = "relative imports work"\n', encoding="utf-8")
    (src / "commands" / "__init__.py").write_text(_COMMANDS, encoding="utf-8")
    (folder / d.MANIFEST_FILENAME).write_text(
        f'[extension]\nname = "{pkg}"\nversion = "0.1.0"\nbuiltin = false\n\n'
        f'[[extension.provides]]\nkind = "cmd"\nnoun = "{pkg}"\n'
        f'entry = "{pkg}.commands:main"\ndescription = "an installed area"\n\n'
        f'[[extension.provides]]\nkind = "cmd"\nnoun = "rag"\nverb = "parsers"\n'
        f'entry = "{pkg}.commands:parsers"\ndescription = "parsers from an installed package"\n',
        encoding="utf-8",
    )
    site = tmp_path / "site-packages"
    shutil.copytree(src, site / pkg)
    monkeypatch.syspath_prepend(str(site))
    for name in [m for m in sys.modules if m == pkg or m.startswith(pkg + ".")]:
        monkeypatch.delitem(sys.modules, name)
    return folder.parent


def _axi(monkeypatch, tmp_path, dirs: list[Path], *argv: str) -> int:
    from axiom import axiom_cli

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXI_FULL_PARSER", "1")
    monkeypatch.setattr(d, "get_extension_dirs", lambda: list(dirs))
    monkeypatch.setattr(sys, "argv", ["axi", *argv])
    try:
        rc = axiom_cli.main()
    except SystemExit as exc:
        rc = exc.code
    return rc or 0


def test_pk1_an_installed_packages_noun_dispatches_with_relative_imports(
    monkeypatch, tmp_path, capsys
):
    exts = _installed_package_extension(tmp_path, monkeypatch, "pkgext_noun")
    assert d.discover_cli_commands(exts)["pkgext_noun"]["builtin"] is False
    rc = _axi(monkeypatch, tmp_path, [exts, BUILTINS], "pkgext_noun")
    out = capsys.readouterr()
    assert rc == 0, out
    assert "noun says relative imports work" in out.out


def test_pk1_an_installed_packages_contributed_verb_dispatches(monkeypatch, tmp_path, capsys):
    exts = _installed_package_extension(tmp_path, monkeypatch, "pkgext_verb")
    rc = _axi(monkeypatch, tmp_path, [exts, BUILTINS], "rag", "parsers")
    out = capsys.readouterr()
    assert rc == 0, out
    assert "verb says relative imports work" in out.out


def test_pk2_a_folder_extension_still_loads_by_path(monkeypatch, tmp_path, capsys):
    folder = tmp_path / "extensions" / "folder-area"
    folder.mkdir(parents=True)
    (folder / "folder_area_cli.py").write_text(
        "def main():\n    print('loaded from the folder')\n    return 0\n", encoding="utf-8")
    (folder / d.MANIFEST_FILENAME).write_text(
        '[extension]\nname = "folder-area"\nversion = "0.1.0"\n\n'
        '[[extension.provides]]\nkind = "cmd"\nnoun = "folder-area"\n'
        'entry = "folder_area_cli:main"\ndescription = "a folder extension"\n',
        encoding="utf-8",
    )
    with pytest.raises(ImportError):
        __import__("folder_area_cli")  # not importable: only the path can load it
    rc = _axi(monkeypatch, tmp_path, [folder.parent], "folder-area")
    out = capsys.readouterr()
    assert rc == 0, out
    assert "loaded from the folder" in out.out
