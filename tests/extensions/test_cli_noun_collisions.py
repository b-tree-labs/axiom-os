# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One noun, one registration (ADR-056): a collision guard for the CLI.

Discovery keeps the first extension of a given name and skips the rest. On
axiom-os-lm 0.70.21 that dropped ``axi app`` on every node with a product
installed: that product ships a declarations-only extension also named ``app``
(its feature flags), its builtins directory is searched first, and Axiom's
``app`` extension, with the ``acceptance`` / ``variants`` / ``run`` verbs, was
skipped as a duplicate. ``axi app`` answered "unknown subcommand".

So: every noun Axiom's builtins provide is provided by exactly one extension,
no builtin that provides a noun shares its name with another, and a
same-named extension that would hide a noun is said out loud.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

import pytest

from axiom.extensions import discovery as d

BUILTINS = Path(d.__file__).resolve().parent / "builtins"

#: Collisions that predate this guard. Each is a real bug (the second
#: registration's verbs are unreachable) with a named owner; the guard keeps
#: the list from growing, and the last test fails once one is fixed so the
#: entry is removed rather than left to excuse a new collision.
KNOWN_COLLISIONS: dict[str, set[str]] = {
    # `connect` (Registry Fabric, ADR-074) and `connector` (the cross-cutting
    # registry) both claim `axi connector`; `connect` sorts first and wins.
    "connector": {"connect", "connector"},
}


def _manifests(root: Path) -> list[d.Extension]:
    out = []
    for child in sorted(root.iterdir()):
        for name in (d.MANIFEST_FILENAME, "neut-extension.toml"):
            m = child / name
            if m.is_file():
                out.append(d.parse_manifest(m))
                break
    return out


def _noun_owners(exts: list[d.Extension]) -> dict[str, set[str]]:
    owners: dict[str, set[str]] = defaultdict(set)
    for ext in exts:
        for cmd in ext.cli_commands:
            owners[cmd.noun].add(ext.name)
    return owners


def test_every_builtin_cli_noun_is_registered_once():
    owners = _noun_owners(_manifests(BUILTINS))
    twice = {n: o for n, o in owners.items() if len(o) > 1 and KNOWN_COLLISIONS.get(n) != o}
    assert not twice, f"a noun registered by more than one extension: {twice}"


def test_a_builtin_that_provides_a_noun_has_a_name_no_other_builtin_has():
    seen: dict[str, int] = defaultdict(int)
    exts = _manifests(BUILTINS)
    for ext in exts:
        seen[ext.name] += 1
    clashes = [e.name for e in exts if e.cli_commands and seen[e.name] > 1]
    assert not clashes, f"a noun-providing extension shares its name: {clashes}"


def test_the_known_collisions_are_still_collisions():
    owners = _noun_owners(_manifests(BUILTINS))
    for noun, expected in KNOWN_COLLISIONS.items():
        assert owners.get(noun) == expected, (
            f"{noun!r} is no longer the collision KNOWN_COLLISIONS records; "
            "remove the entry"
        )


def _ext(root: Path, dirname: str, manifest: str) -> None:
    (root / dirname).mkdir(parents=True)
    (root / dirname / d.MANIFEST_FILENAME).write_text(manifest, encoding="utf-8")


_FLAGS_ONLY = """
[extension]
name = "app"
version = "0.1.0"
builtin = true

[[flags]]
key = "control"
description = "the Control verb"
default = "off"
"""


def test_a_product_extension_named_app_does_not_hide_axi_app(tmp_path):
    """The 0.70.21 node: the product's flags-only ``app`` is searched before Axiom's."""
    product = tmp_path / "product_os" / "extensions" / "builtins"
    _ext(product, "app", _FLAGS_ONLY)
    cmds = d.discover_cli_commands(product, BUILTINS)
    assert "app" in cmds
    assert cmds["app"]["module"] == "axiom.extensions.builtins.app_buildout.cli"
    # and the product's declarations survive alongside it
    names = [e.name for e in d.discover_extensions(product, BUILTINS)]
    assert names.count("app") == 1


def test_a_same_named_extension_that_hides_a_noun_is_said(tmp_path, caplog):
    first = tmp_path / "a"
    second = tmp_path / "b"
    _ext(first, "thing", '[extension]\nname = "thing"\nversion = "0.1.0"\n')
    _ext(second, "thing", (
        '[extension]\nname = "thing"\nversion = "0.1.0"\n\n'
        '[[extension.provides]]\nkind = "cmd"\nnoun = "thing"\n'
        'entry = "x.cli:main"\ndescription = "d"\n'
    ))
    with caplog.at_level(logging.WARNING, logger=d.logger.name):
        cmds = d.discover_cli_commands(first, second)
    assert "thing" not in cmds  # first still wins: an override stays an override
    said = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("thing" in m and "hides" in m for m in said), said


@pytest.mark.parametrize("argv", [["acceptance", "--help"], ["run", "--help"],
                                  ["variants", "--help"]])
def test_axi_app_verbs_live_under_one_noun(argv, capsys):
    from axiom.extensions.builtins.app_buildout import cli

    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 0
    assert "usage:" in capsys.readouterr().out


def _axi(monkeypatch, tmp_path, *argv: str) -> int:
    """``axi <argv>`` through the real top-level dispatch, in process."""
    import sys

    from axiom import axiom_cli

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "argv", ["axi", *argv])
    try:
        rc = axiom_cli.main()
    except SystemExit as exc:
        rc = exc.code
    return rc or 0


def test_axi_app_run_help_dispatches(monkeypatch, tmp_path, capsys):
    assert _axi(monkeypatch, tmp_path, "app", "run", "--help") == 0
    out = capsys.readouterr().out
    assert "unknown subcommand" not in out and "--variant" in out


def test_axi_app_acceptance_phase_p1_prints_counts(monkeypatch, tmp_path, capsys):
    spec = tmp_path / "spec.md"
    spec.write_text(
        "# Spec\n\n## P1 The MVP\n\n### MVP-07 Auth\n\n| ID | Acceptance | Trace |\n"
        "|---|---|---|\n| MVP-07-A1 | A row | x |\n", encoding="utf-8")
    (tmp_path / "repo").mkdir()
    cfg = tmp_path / "acceptance.toml"
    cfg.write_text(f'[acceptance]\nspec = "{spec}"\n\n[[repo]]\nname = "r"\n'
                   f'path = "{tmp_path / "repo"}"\nlang = "python"\n', encoding="utf-8")
    assert _axi(monkeypatch, tmp_path, "app", "acceptance", "--phase", "P1",
                "--config", str(cfg)) == 0
    out = capsys.readouterr().out
    assert "unknown subcommand" not in out
    assert "P1:" in out and "unproven" in out
