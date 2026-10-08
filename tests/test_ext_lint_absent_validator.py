# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""AEOS023 — a check that could not run is not a check that failed.

`axi ext lint` validates the manifest against a JSON Schema that ships in
``axiom_tests``. That package is NOT on PyPI: it lives in this repo under
``packages/axiom-tests`` and reaches contributors through an editable install.
So for anyone authoring an extension against a released axiom wheel — which
is every external author — it is absent by construction.

It used to be reported like this::

    [FAIL] AEOS021: manifest schema violation: axiom-tests not installed

Three things wrong with that, in increasing order of cost. It accuses the
author's manifest, which is fine. It names a package no `pip install` can
satisfy, so the remediation is impossible. And because it is an *error*, it
made `axi ext lint` unpassable from a released wheel — which blocked
`quickstart`, `validate` and `publish` behind it. The authoring ladder
advertised by `axi ext init`'s own "Next steps" could not be climbed past
the first rung by anyone outside this repo.

The fix is to tell the two states apart and say which one happened.
"""

from __future__ import annotations

import builtins
import textwrap

import pytest

from axiom.cli.ext.commands.lint import _validate_schema, lint_extension

_MANIFEST = textwrap.dedent(
    """
    [extension]
    name = "coolant_trend"
    version = "0.1.0"
    description = "Trend coolant temperature"
    license = "Apache-2.0"
    owner = "ut-austin"
    aeos_version = "0.1.0"

    [extension.mcp]
    enabled = false
    """
).strip()


@pytest.fixture
def extension(tmp_path):
    """A well-formed extension on disk, the shape `axi ext init` produces."""
    root = tmp_path / "coolant_trend"
    (root / "coolant_trend").mkdir(parents=True)
    (root / "coolant_trend" / "__init__.py").write_text(
        "__all__: list[str] = []\n", encoding="utf-8"
    )
    tests_dir = root / "tests" / "unit_tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_standard.py").write_text("", encoding="utf-8")
    (root / "axiom-extension.toml").write_text(_MANIFEST + "\n", encoding="utf-8")
    (root / "README.md").write_text("# coolant_trend\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (root / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "coolant_trend"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    return root


@pytest.fixture
def without_axiom_tests(monkeypatch):
    """What an author with a released wheel has: no validator.

    Patching the import rather than the helper keeps the test honest about
    *why* it is absent — an ImportError at the point `_validate_schema`
    reaches for it, which is exactly what happens in the wild.
    """
    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "axiom_tests" or name.startswith("axiom_tests."):
            raise ImportError("No module named 'axiom_tests'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)


class TestTheTwoStatesAreDistinguishable:
    def test_an_absent_validator_reports_that_it_did_not_run(self, without_axiom_tests):
        assert _validate_schema({"extension": {}}) == (False, [])

    def test_a_present_validator_reports_that_it_ran(self, extension):
        pytest.importorskip("axiom_tests")
        ran, _ = _validate_schema({"extension": {"name": "x"}})
        assert ran is True

    def test_the_empty_lists_are_not_interchangeable(self, without_axiom_tests):
        """`(False, [])` and `(True, [])` both carry no errors and mean
        opposite things. Collapsing them is the original bug."""
        did_not_run = _validate_schema({"extension": {}})
        assert did_not_run[1] == [], "no violations were found"
        assert did_not_run[0] is False, "but nothing was checked either"


class TestAuthoringFromAReleasedWheel:
    def test_lint_passes(self, extension, without_axiom_tests):
        """The whole point: an author with a good manifest and no validator
        can get through `axi ext lint`, and so through everything gated on
        it."""
        errors = [f for f in lint_extension(extension) if f.severity == "error"]
        assert errors == [], [f"{f.code}: {f.message}" for f in errors]

    def test_but_it_says_the_schema_was_not_checked(self, extension, without_axiom_tests):
        """Passing silently would be the other failure — the author would
        believe the schema was verified when it was not."""
        warned = [f for f in lint_extension(extension) if f.code == "AEOS023"]
        assert len(warned) == 1
        assert warned[0].severity == "warning"
        assert "NOT CHECKED" in warned[0].message

    def test_the_remediation_does_not_name_an_impossible_install(
        self, extension, without_axiom_tests
    ):
        """axiom-tests is not on PyPI. Telling an author to install it sends
        them to a command that cannot succeed."""
        (warned,) = [f for f in lint_extension(extension) if f.code == "AEOS023"]
        assert "pip install axiom-tests" not in warned.remediation
        assert "not on PyPI" in warned.remediation

    def test_nothing_is_reported_as_a_manifest_violation(self, extension, without_axiom_tests):
        """The accusation that started this: a valid manifest called invalid."""
        assert [f for f in lint_extension(extension) if f.code == "AEOS021"] == []


class TestTheCheckStillChecks:
    """Relaxing a rule is only safe if the rule still fires when it should."""

    def test_a_real_violation_is_still_an_error(self, extension, monkeypatch):
        monkeypatch.setattr(
            "axiom.cli.ext.commands.lint._validate_schema",
            lambda manifest: (True, ["'version' is a required property"]),
        )
        found = [f for f in lint_extension(extension) if f.code == "AEOS021"]
        assert len(found) == 1
        assert found[0].severity == "error"
        assert "required property" in found[0].message

    def test_and_a_checked_valid_manifest_warns_about_nothing(self, extension, monkeypatch):
        monkeypatch.setattr(
            "axiom.cli.ext.commands.lint._validate_schema",
            lambda manifest: (True, []),
        )
        assert [f for f in lint_extension(extension) if f.code == "AEOS023"] == []
