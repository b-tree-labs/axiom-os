# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""AEOS100-103: a long-running service says how it stays up through a change.

Axiom does not go down from its own causes: deploys, updates, migrations and
configuration changes (ADR-182). That is only true if every service knows how
it is switched, how it reports ready, how long it drains, and whether its
schema changes are safe to share between the old and new versions. A service
that declares none of this is one nobody has thought about, so it is an
error, not a warning.

Whether the declaration has been PROVEN is a separate question. A contract
whose zero-gap upgrade test does not exist yet is a warning (AEOS102), so the
fleet can declare honestly now and prove one service at a time. A proof that
names a file which is not there is an error (AEOS103): it reads as evidence.
"""

from __future__ import annotations

from pathlib import Path

from axiom.cli.ext.commands.lint import (
    AVAILABILITY_MIGRATIONS,
    AVAILABILITY_SWITCHES,
    _check_availability,
)

GOOD = {
    "switch": "blue-green",
    "readiness": "http:/healthz",
    "drain_s": 30,
    "migrations": "expand-contract",
    "single_flight": False,
    "verified_by": "pending",
}


def _manifest(availability=None, kinds=("service",)):
    ext = {"provides": [{"kind": k} for k in kinds]}
    if availability is not None:
        ext["availability"] = availability
    return {"extension": ext}


def _codes(manifest, ext_path: Path | None = None):
    return [(f.code, f.severity) for f in _check_availability(manifest, ext_path or Path("."))]


class TestAServiceMustDeclare:
    def test_a_service_with_no_availability_block_is_an_error(self):
        assert _codes(_manifest()) == [("AEOS100", "error")]

    def test_the_error_says_what_to_add(self):
        (finding,) = _check_availability(_manifest(), Path("."))
        assert "[extension.availability]" in finding.remediation
        assert "ADR-182" in finding.remediation

    def test_an_extension_with_no_service_is_not_asked(self):
        assert _codes(_manifest(kinds=("cmd", "skill", "tool"))) == []


class TestTheVocabularyIsClosed:
    def test_each_switch_and_migration_value_passes(self):
        for switch in AVAILABILITY_SWITCHES:
            for mig in AVAILABILITY_MIGRATIONS:
                found = _codes(_manifest({**GOOD, "switch": switch, "migrations": mig}))
                assert found == [("AEOS102", "warning")], (switch, mig)

    def test_an_unknown_switch_is_an_error(self):
        found = _codes(_manifest({**GOOD, "switch": "restart"}))
        assert ("AEOS101", "error") in found

    def test_a_restart_is_not_a_switch_strategy(self):
        """The one strategy that is down for its duration is the one this
        contract exists to remove; it must not be declarable."""
        assert "restart" not in AVAILABILITY_SWITCHES

    def test_readiness_must_say_how(self):
        for bad in ("", "yes", "http:", "/healthz"):
            found = _codes(_manifest({**GOOD, "readiness": bad}))
            assert ("AEOS101", "error") in found, bad
        for good in ("http:/healthz", "tcp", "process"):
            assert ("AEOS101", "error") not in _codes(_manifest({**GOOD, "readiness": good})), good

    def test_drain_must_be_a_non_negative_number_of_seconds(self):
        for bad in (-1, "30", None, True):
            found = _codes(_manifest({**GOOD, "drain_s": bad}))
            assert ("AEOS101", "error") in found, bad

    def test_missing_fields_are_named(self):
        (finding,) = [
            f
            for f in _check_availability(_manifest({"switch": "overlap"}), Path("."))
            if f.code == "AEOS101"
        ]
        for field in ("readiness", "drain_s", "migrations"):
            assert field in finding.message


class TestDeclaredIsNotProven:
    def test_pending_proof_is_a_warning(self):
        assert _codes(_manifest(GOOD)) == [("AEOS102", "warning")]

    def test_a_named_proof_that_exists_passes(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_zero_gap.py").write_text("def test_x(): pass\n")
        found = _codes(
            _manifest({**GOOD, "verified_by": "tests/test_zero_gap.py"}), ext_path=tmp_path
        )
        assert found == []

    def test_a_named_proof_that_is_missing_is_an_error(self, tmp_path):
        found = _codes(_manifest({**GOOD, "verified_by": "tests/nope.py"}), ext_path=tmp_path)
        assert found == [("AEOS103", "error")]


def test_every_builtin_service_extension_declares():
    """The fleet is held to the contract it adopts, not exempted from it."""
    import tomllib

    root = Path(__file__).resolve().parents[1] / "src" / "axiom" / "extensions" / "builtins"
    missing = []
    for manifest_path in sorted(root.glob("*/axiom-extension.toml")):
        manifest = tomllib.loads(manifest_path.read_text())
        errors = [
            f.code
            for f in _check_availability(manifest, manifest_path.parent)
            if f.severity == "error"
        ]
        if errors:
            missing.append(f"{manifest_path.parent.name}: {errors}")
    assert not missing, missing
