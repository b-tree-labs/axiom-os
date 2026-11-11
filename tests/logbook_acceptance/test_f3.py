# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit F3: `axi ext init --template logbook` and the `logbook.declared` check
in `axi ext doctor`.

Ticket: docs/working/logbook-units/F3.md in the product repo that holds the plan. The template
reproduces F2's example (`docs/guides/examples/example_logbook/`), and a parity test keeps the two
from drifting. Nothing here needs a database: the doctor check validates the declaration only.
"""

from __future__ import annotations

import argparse
import tomllib
from pathlib import Path

import pytest

from axiom.cli.ext.commands.doctor import run_doctor
from axiom.cli.ext.commands.init import InitProvider, validate_name
from axiom.cli.ext.commands.lint import lint_extension
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.templates import get_template, registry
from axiom.extensions.builtins.attest import registry as logbook_registry
from axiom.extensions.builtins.attest.logbooks import load_logbook, parse_logbook

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "docs" / "guides" / "examples" / "example_logbook" / "logbook" / "example_log.toml"
NAME = "field_notes"


@pytest.fixture
def fresh_registry(monkeypatch):
    """An empty node registry, so the duplicate check sees only what a test registers."""
    monkeypatch.setattr(logbook_registry, "_logbooks", {})
    monkeypatch.setattr(logbook_registry, "_discovered", True)


def _scaffold(dest: Path, name: str = NAME) -> Path:
    get_template("logbook").create(
        dest / name, name=name, owner="example-lab", license="Apache-2.0", description=""
    )
    return dest / name


def _declared(ext: Path) -> list:
    return [r for r in run_doctor(ext, skip_tests=True) if r.check == "logbook.declared"]


@pytest.mark.acceptance("LB-F3-A1")
def test_f3_a1_the_registry_offers_a_logbook_template_and_one_default():
    """`axi ext templates` lists `logbook`, and the only default is still `compound`."""
    ids = [t.id for t in registry()]
    assert "logbook" in ids
    assert [t.id for t in registry() if t.is_default] == ["compound"]
    assert "log" in get_template("logbook").description


@pytest.mark.acceptance("LB-F3-A2")
def test_f3_a2_the_scaffold_is_the_compound_layout_plus_a_logbook_and_lints_clean(tmp_path):
    """The compound layout plus `logbook/<name>.toml`, and `ext lint` finds no error."""
    ext = _scaffold(tmp_path)
    compound = tmp_path / "compound"
    get_template("compound").create(
        compound / NAME, name=NAME, owner="example-lab", license="Apache-2.0", description=""
    )
    expected = {p.relative_to(compound / NAME) for p in (compound / NAME).rglob("*")}
    written = {p.relative_to(ext) for p in ext.rglob("*")}
    assert expected <= written
    assert written - expected == {Path("logbook"), Path("logbook") / f"{NAME}.toml"}
    errors = [f for f in lint_extension(ext) if f.severity == "error"]
    assert errors == [], errors


@pytest.mark.acceptance("LB-F3-A3")
def test_f3_a3_the_manifest_declares_the_logbook_and_validates(tmp_path):
    """A `kind = "logbook"` block whose `id` is the logbook's, valid against the AEOS schema."""
    from axiom_tests._manifest import validate_manifest

    ext = _scaffold(tmp_path)
    manifest = tomllib.loads((ext / "axiom-extension.toml").read_text(encoding="utf-8"))
    assert validate_manifest(manifest) == []
    (block,) = [p for p in manifest["extension"]["provides"] if p["kind"] == "logbook"]
    assert block["id"] == NAME == load_logbook(ext / block["file"]).id
    assert block["file"] == f"logbook/{NAME}.toml"


@pytest.mark.acceptance("LB-F3-A4")
def test_f3_a4_the_scaffolded_logbook_is_the_example_renamed(tmp_path):
    """It loads with nothing not yet enforced, and equals F2's example apart from its id and name."""
    ext = _scaffold(tmp_path)
    path = ext / "logbook" / f"{NAME}.toml"
    book = load_logbook(path)
    assert book.not_yet_enforced == ()
    assert book.display == "Field Notes"
    example = EXAMPLE.read_text(encoding="utf-8")
    renamed = example.replace("example_log", NAME).replace("Example Log", "Field Notes")
    assert path.read_text(encoding="utf-8") == renamed


@pytest.mark.acceptance("LB-F3-A5")
def test_f3_a5_doctor_reports_the_declared_logbook(tmp_path, fresh_registry):
    """`logbook.declared` passes and names the logbook and its entry types."""
    ext = _scaffold(tmp_path)
    (row,) = _declared(ext)
    assert row.ok
    assert NAME in row.detail
    for type_id in ("OPEN_PERIOD", "CHECK", "NOTE", "CLOSE_PERIOD"):
        assert type_id in row.detail


def _fails(ext: Path, needle: str) -> None:
    (row,) = _declared(ext)
    assert not row.ok, row
    assert row.remediation, row
    assert needle in row.detail + " " + row.remediation, row


@pytest.mark.acceptance("LB-F3-A6")
def test_f3_a6_doctor_fails_each_broken_declaration_and_says_what_to_fix(tmp_path, fresh_registry):
    """Missing file, bad TOML, a key the loader refuses, an id that differs from the manifest, and an
    id another extension declares: each fails with a remediation. Deferred keys pass but are listed
    as declared, not yet enforced."""
    ext = _scaffold(tmp_path)
    book = ext / "logbook" / f"{NAME}.toml"
    good = book.read_text(encoding="utf-8")

    book.unlink()
    _fails(ext, f"logbook/{NAME}.toml")

    book.write_text("[logbook\n", encoding="utf-8")
    _fails(ext, "TOML")

    book.write_text(
        good.replace('version = "1"', 'version = "1"\nmade_up = true'), encoding="utf-8"
    )
    _fails(ext, "made_up")

    book.write_text(good.replace(f'id = "{NAME}"', 'id = "other_notes"', 1), encoding="utf-8")
    _fails(ext, "other_notes")

    book.write_text(good, encoding="utf-8")
    logbook_registry.register(parse_logbook(tomllib.loads(good), source="elsewhere/other.toml"))
    _fails(ext, "elsewhere/other.toml")

    logbook_registry.reset()
    book.write_text(
        good.replace('version = "1"', 'version = "1"\nretention = "7y"'), encoding="utf-8"
    )
    (row,) = _declared(ext)
    assert row.ok, row
    assert "declared, not yet enforced" in row.detail and "retention" in row.detail


@pytest.mark.acceptance("LB-F3-A7")
def test_f3_a7_an_extension_with_no_logbook_gets_no_row(tmp_path):
    get_template("compound").create(
        tmp_path / NAME, name=NAME, owner="example-lab", license="Apache-2.0", description=""
    )
    assert _declared(tmp_path / NAME) == []


@pytest.mark.acceptance("LB-F3-A8")
def test_f3_a8_the_template_takes_only_names_validate_name_takes(tmp_path):
    """`validate_name` still rejects what it rejected, and the template refuses the same names."""
    rejected = ["", "Bad", "has-hyphen", "1digit", "notes_agent", "test", "src"]
    for name in rejected:
        assert validate_name(name) is not None, name
        with pytest.raises(ValueError):
            get_template("logbook").create(
                tmp_path / (name or "empty"), name=name, owner="", license="Apache-2.0",
                description="",
            )  # fmt: skip
        provider = InitProvider()
        parser = argparse.ArgumentParser()
        provider.add_arguments(parser)
        args = parser.parse_args([name, "--template", "logbook", "--dir", str(tmp_path)])
        assert provider.run(args, CliContext(cwd=tmp_path)) != 0, name
    assert validate_name(NAME) is None
