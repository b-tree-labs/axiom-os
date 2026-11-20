# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An extension adds verbs under a noun another extension owns (#1969).

A noun belongs to one extension. Discovery used to keep the first extension
that declared a noun and skip any later one without a word, so an extension
that added a capability to an existing area either claimed a new top-level
noun for one provider or watched its commands disappear.

Now a ``kind = "cmd"`` block that names a ``verb`` contributes that verb under
its ``noun``; the verb's entry is a skill function (ADR-056), dispatched
through the capability chokepoint as ``<noun>.<verb>``. A taken verb and a
second claim on a noun are refused out loud, naming both extensions.

Acceptance rows: EV-1 dispatch, EV-2 verb collision, EV-3 duplicate noun,
EV-4 no new top-level noun, EV-5 ``--help`` lists contributed verbs.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from axiom.extensions import discovery as d

BUILTINS = Path(d.__file__).resolve().parent / "builtins"

_SKILL_MODULE = '''
from axiom.infra.skills import SkillResult


def list_parsers(params, ctx):
    return SkillResult(ok=True, value={"parsers": ["layout-aware"], "params": params})
'''


def _contributor(root: Path, name: str, *, noun: str = "rag", verb: str = "parsers",
                 pkg: str | None = None) -> Path:
    """A folder extension that contributes ``<noun> <verb>`` as a skill function."""
    pkg = pkg or name.replace("-", "_")
    ext = root / name
    (ext / pkg).mkdir(parents=True)
    (ext / pkg / "__init__.py").write_text("", encoding="utf-8")
    (ext / pkg / "skills.py").write_text(_SKILL_MODULE, encoding="utf-8")
    (ext / d.MANIFEST_FILENAME).write_text(
        f'[extension]\nname = "{name}"\nversion = "0.1.0"\n\n'
        f'[[extension.provides]]\nkind = "cmd"\nnoun = "{noun}"\nverb = "{verb}"\n'
        f'entry = "{pkg}.skills:list_parsers"\n'
        f'description = "List the document parsers {name} adds"\n',
        encoding="utf-8",
    )
    return ext


def _noun_owner(root: Path, name: str, noun: str) -> None:
    ext = root / name
    ext.mkdir(parents=True)
    (ext / d.MANIFEST_FILENAME).write_text(
        f'[extension]\nname = "{name}"\nversion = "0.1.0"\n\n'
        f'[[extension.provides]]\nkind = "cmd"\nnoun = "{noun}"\n'
        f'entry = "{name}_cli:main"\ndescription = "the {noun} area"\n',
        encoding="utf-8",
    )


def _axi(monkeypatch, tmp_path, dirs: list[Path], *argv: str) -> int:
    """``axi <argv>`` through the real top-level dispatch, over *dirs* only."""
    from axiom import axiom_cli

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("AXI_FULL_PARSER", "1")  # scan, never the startup cache
    monkeypatch.setattr(d, "get_extension_dirs", lambda: list(dirs))
    monkeypatch.setattr(sys, "argv", ["axi", *argv])
    try:
        rc = axiom_cli.main()
    except SystemExit as exc:
        rc = exc.code
    return rc or 0


# --------------------------------------------------------------------------- EV-1


def test_ev1_a_contributed_verb_under_rag_is_discovered_with_its_extension(tmp_path):
    _contributor(tmp_path / "user", "page-parsers")
    cmds = d.discover_cli_commands(tmp_path / "user", BUILTINS)
    assert cmds["rag"]["extension"] == "rag"  # the owner keeps the noun
    verb = cmds["rag"]["verbs"]["parsers"]
    assert verb["extension"] == "page-parsers"
    assert verb["function"] == "list_parsers"


def test_ev1_axi_rag_parsers_dispatches_to_the_contributed_skill(monkeypatch, tmp_path, capsys):
    user = tmp_path / "user"
    _contributor(user, "page-parsers")
    rc = _axi(monkeypatch, tmp_path, [user, BUILTINS], "rag", "parsers", "--kind", "pdf", "--all")
    out = capsys.readouterr()
    assert rc == 0, out
    assert "layout-aware" in out.out
    # flags reach the skill as params
    assert '"kind": "pdf"' in out.out and '"all": true' in out.out


# --------------------------------------------------------------------------- EV-2


def test_ev2_a_verb_two_extensions_contribute_fails_discovery_naming_both(tmp_path):
    _contributor(tmp_path / "a", "page-parsers")
    _contributor(tmp_path / "b", "table-parsers")
    with pytest.raises(d.CLICommandConflict) as exc:
        d.discover_cli_commands(tmp_path / "a", tmp_path / "b", BUILTINS, strict=True)
    msg = str(exc.value)
    assert "rag parsers" in msg and "page-parsers" in msg and "table-parsers" in msg


def test_cq1_a_refused_verb_runs_quietly_and_is_logged_at_debug(monkeypatch, tmp_path, capsys, caplog):
    _contributor(tmp_path / "a", "page-parsers")
    _contributor(tmp_path / "b", "table-parsers")
    with caplog.at_level(logging.DEBUG, logger=d.logger.name):
        rc = _axi(monkeypatch, tmp_path, [tmp_path / "a", tmp_path / "b", BUILTINS], "rag", "parsers")
    out = capsys.readouterr()
    assert rc == 0 and "layout-aware" in out.out  # the first in precedence keeps the verb
    assert out.err == ""
    said = [r for r in caplog.records if "table-parsers" in r.getMessage()]
    assert said and all(r.levelno == logging.DEBUG for r in said)


def test_ev2_a_verb_the_owner_already_has_is_refused_naming_both(monkeypatch, tmp_path, capsys):
    user = tmp_path / "user"
    _contributor(user, "page-parsers", verb="status")  # `rag status` is the owner's
    rc = _axi(monkeypatch, tmp_path, [user, BUILTINS], "rag", "status", "--help")
    out = capsys.readouterr()
    assert rc == 0
    assert "layout-aware" not in out.out  # the owner's verb ran, not the contribution
    assert out.err == ""  # a normal run stays quiet; doctor reports collisions


# --------------------------------------------------------------------------- EV-3


def test_ev3_a_second_extension_declaring_a_noun_fails_discovery_naming_both(tmp_path):
    _noun_owner(tmp_path / "a", "first-area", "widgets")
    _noun_owner(tmp_path / "b", "second-area", "widgets")
    with pytest.raises(d.CLICommandConflict) as exc:
        d.discover_cli_commands(tmp_path / "a", tmp_path / "b", strict=True)
    msg = str(exc.value)
    assert "'widgets'" in msg and "first-area" in msg and "second-area" in msg


def test_ev3_the_refused_noun_is_said_not_skipped(monkeypatch, tmp_path, capsys):
    _noun_owner(tmp_path / "a", "first-area", "widgets")
    _noun_owner(tmp_path / "b", "second-area", "widgets")
    (tmp_path / "a" / "first-area" / "first-area_cli.py").write_text(
        "def main():\n    print('first ran')\n    return 0\n", encoding="utf-8")
    cmds = d.discover_cli_commands(tmp_path / "a", tmp_path / "b")
    assert cmds["widgets"]["extension"] == "first-area"
    assert any("second-area" in r for r in cmds["widgets"]["refused"])
    rc = _axi(monkeypatch, tmp_path, [tmp_path / "a", tmp_path / "b"], "widgets")
    out = capsys.readouterr()
    assert rc == 0 and "first ran" in out.out  # who wins is unchanged
    assert out.err == ""  # CQ-1: a normal dispatch writes nothing to stderr


def test_cq1_the_known_builtin_collision_dispatches_quietly(monkeypatch, tmp_path, capsys):
    rc = _axi(monkeypatch, tmp_path, [BUILTINS], "connector", "--help")
    out = capsys.readouterr()
    assert rc == 0 and out.err == ""


def test_cq2_doctor_lists_each_collision_naming_both_extensions_and_the_fix(monkeypatch, tmp_path):
    from axiom import axiom_cli

    _noun_owner(tmp_path / "a", "first-area", "widgets")
    _noun_owner(tmp_path / "b", "second-area", "widgets")
    _contributor(tmp_path / "a", "page-parsers")
    _contributor(tmp_path / "b", "table-parsers")
    monkeypatch.setattr(d, "get_extension_dirs", lambda: [tmp_path / "a", tmp_path / "b", BUILTINS])
    monkeypatch.setattr(axiom_cli, "_setup_checks", lambda: [])
    monkeypatch.setattr(axiom_cli, "_brand_health_extras", lambda: [])
    rows = [r for r in axiom_cli._gather_diagnostics()["checks"]
            if str(r["name"]).startswith("CLI name")]
    text = "\n".join(f"{r['name']}: {r['status']}" for r in rows)
    assert "first-area" in text and "second-area" in text and "widgets" in text
    assert "page-parsers" in text and "table-parsers" in text and "rag parsers" in text
    assert all("Fix:" in r["status"] for r in rows)
    report = axiom_cli.render_doctor_report(rows, cli="axi")
    assert "second-area" in report


def test_cq3_strict_discovery_still_raises_on_the_known_collision():
    with pytest.raises(d.CLICommandConflict) as exc:
        d.discover_cli_commands(BUILTINS, strict=True)
    assert "'connector'" in str(exc.value)


# --------------------------------------------------------------------------- EV-4


def test_ev4_a_contributed_verb_never_becomes_a_top_level_noun(tmp_path):
    _contributor(tmp_path / "user", "page-parsers")
    cmds = d.discover_cli_commands(tmp_path / "user", BUILTINS)
    assert "parsers" not in cmds and "page-parsers" not in cmds


def test_ev4_a_verb_under_a_noun_nobody_owns_is_refused_not_promoted(tmp_path):
    _contributor(tmp_path / "user", "page-parsers", noun="parsing")
    assert "parsing" not in d.discover_cli_commands(tmp_path / "user")
    with pytest.raises(d.CLICommandConflict) as exc:
        d.discover_cli_commands(tmp_path / "user", strict=True)
    assert "page-parsers" in str(exc.value) and "'parsing'" in str(exc.value)


# --------------------------------------------------------------------------- EV-5


def test_ev5_noun_help_lists_contributed_verbs_and_their_extension(monkeypatch, tmp_path, capsys):
    user = tmp_path / "user"
    _contributor(user, "page-parsers")
    rc = _axi(monkeypatch, tmp_path, [user, BUILTINS], "rag", "--help")
    out = capsys.readouterr().out
    assert rc == 0
    assert "search" in out  # the owner's own help is still there
    line = next(ln for ln in out.splitlines() if ln.strip().startswith("parsers"))
    assert "page-parsers" in line and "List the document parsers" in line
