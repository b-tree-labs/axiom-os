# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""AEOS090/091 — an extension has to say what its values are worth.

ADR-136 makes uncertainty a platform primitive and spec-uncertainty §2
states the contract an extension implements. A contract nothing checks is a
suggestion, and the specific failure it guards is invisible: an extension
produces values with no uncertainty, every aggregate over them reports
`claimable: false`, and nothing anywhere fails.
"""

from __future__ import annotations

from axiom.cli.ext.commands.lint import (
    UNCERTAINTY_POSTURES,
    _check_uncertainty_posture,
)


def _codes(manifest):
    return [(f.code, f.severity) for f in _check_uncertainty_posture(manifest)]


class TestTheVocabularyIsClosed:
    """Free text cannot be checked, and the point of the field is that a
    reader can tell the four states apart."""

    def test_each_admitted_posture_passes(self):
        for posture in UNCERTAINTY_POSTURES:
            assert _codes({"extension": {"uncertainty": {"posture": posture}}}) == []

    def test_a_misspelled_posture_is_an_error_not_a_warning(self):
        """A declared surface that is misspelled is worse than one that is
        missing, because a reader believes it."""
        found = _codes({"extension": {"uncertainty": {"posture": "carrys"}}})
        assert found == [("AEOS090", "error")]

    def test_the_error_names_the_admitted_values(self):
        (finding,) = _check_uncertainty_posture(
            {"extension": {"uncertainty": {"posture": "probably-fine"}}}
        )
        for posture in UNCERTAINTY_POSTURES:
            assert posture in finding.message or posture in finding.remediation

    def test_the_shorthand_form_is_accepted(self):
        """`uncertainty = "carries"` rather than a table. Refusing it would be
        pedantry about a declaration we want made."""
        assert _codes({"extension": {"uncertainty": "carries"}}) == []
        assert _codes({"extension": {"uncertainty": "nonsense"}}) == [("AEOS090", "error")]


class TestSilenceIsOnlyAGapWhenValuesAreProduced:
    def test_a_value_producing_capability_with_no_posture_warns(self):
        for kind in ("normalizer", "emitter"):
            found = _codes({"extension": {"provides": [{"kind": kind}]}})
            assert found == [("AEOS091", "warning")], kind

    def test_an_extension_that_produces_no_values_is_not_nagged(self):
        assert _codes({"extension": {"provides": [{"kind": "skill"}, {"kind": "cmd"}]}}) == []

    def test_declaring_a_posture_silences_the_warning(self):
        assert (
            _codes(
                {
                    "extension": {
                        "uncertainty": {"posture": "none"},
                        "provides": [{"kind": "normalizer"}],
                    }
                }
            )
            == []
        )

    def test_the_warning_names_the_kinds_that_triggered_it(self):
        (finding,) = _check_uncertainty_posture(
            {"extension": {"provides": [{"kind": "emitter"}, {"kind": "normalizer"}]}}
        )
        assert "emitter" in finding.message and "normalizer" in finding.message

    def test_it_is_a_warning_because_the_fleet_has_not_declared_yet(self):
        """Erroring on a new check fails lint everywhere at once and teaches
        people to pass --no-verify. The ratchet, not the cliff."""
        (finding,) = _check_uncertainty_posture(
            {"extension": {"provides": [{"kind": "normalizer"}]}}
        )
        assert finding.severity == "warning"

    def test_the_remediation_says_what_to_write_and_why(self):
        (finding,) = _check_uncertainty_posture(
            {"extension": {"provides": [{"kind": "normalizer"}]}}
        )
        assert "[extension.uncertainty]" in finding.remediation
        assert "nothing fails" in finding.remediation


class TestTheFieldIsNotADeclaredSurfaceNothingUses:
    def test_the_data_platform_manifest_declares_its_own_posture(self):
        """A field the platform requires of others and does not set itself is
        the defect class this repo calls a declared surface that does not
        exist."""
        import tomllib
        from pathlib import Path

        import axiom.extensions.builtins.data_platform as dp

        manifest = Path(dp.__file__).parent / "axiom-extension.toml"
        with manifest.open("rb") as fh:
            parsed = tomllib.load(fh)
        block = parsed["extension"]["uncertainty"]
        assert block["posture"] in UNCERTAINTY_POSTURES
        assert _check_uncertainty_posture(parsed) == []

    def test_a_new_extension_is_scaffolded_with_a_posture(self):
        """A rule an author meets only after failing lint is a rule they meet
        too late."""
        from axiom.cli.ext.templates.scaffold import _manifest

        text = _manifest(name="demo_ext", description="d", owner="o", license="Apache-2.0")
        assert "[extension.uncertainty]" in text
        assert 'posture = "not-applicable"' in text
        # And the scaffolded value must itself be admitted.
        import tomllib

        parsed = tomllib.loads(text)
        assert parsed["extension"]["uncertainty"]["posture"] in UNCERTAINTY_POSTURES
        assert _check_uncertainty_posture(parsed) == []


class TestTheSchemaAdmitsTheFieldItRequires:
    """A manifest field the lint requires and the schema rejects is a rule
    that cannot be obeyed. `additionalProperties: false` on the Extension
    block means a new field has to be declared in both places or every
    manifest that complies fails validation."""

    def _schema(self):
        import json
        from pathlib import Path

        import axiom_tests

        path = Path(axiom_tests.__file__).parent / "schemas" / "aeos-manifest-0.1.json"
        return json.loads(path.read_text())

    def test_the_extension_block_declares_uncertainty(self):
        ext = self._schema()["$defs"]["Extension"]
        assert ext["additionalProperties"] is False
        assert "uncertainty" in ext["properties"]

    def test_the_schema_vocabulary_matches_the_lint_vocabulary(self):
        """Two copies of a closed vocabulary drift. If they do, a manifest
        passes one gate and fails the other and nobody can tell which is
        right."""
        field = self._schema()["$defs"]["Extension"]["properties"]["uncertainty"]
        for variant in field["oneOf"]:
            enum = variant.get("enum") or variant["properties"]["posture"]["enum"]
            assert tuple(enum) == UNCERTAINTY_POSTURES

    def test_both_the_shorthand_and_the_table_validate(self):
        """Validated against the field's OWN subschema, not a whole manifest.

        A full-manifest fixture drags in every unrelated requirement, and a
        failure then says nothing about this field — the first version of this
        test failed on a malformed `provides` block and told me nothing.
        """
        import jsonschema

        field = self._schema()["$defs"]["Extension"]["properties"]["uncertainty"]
        for value in (
            "carries",
            "not-applicable",
            {"posture": "carries"},
            {"posture": "none", "symbols": ["demo_ext:*"], "verb": "demo.coverage"},
        ):
            jsonschema.validate(value, field)

    def test_a_posture_outside_the_vocabulary_fails_the_schema_too(self):
        """Belt and braces: the lint catches it with a readable message, and
        the schema catches it for anything that skips the lint."""
        import jsonschema
        import pytest as _pytest

        field = self._schema()["$defs"]["Extension"]["properties"]["uncertainty"]
        for bad in ("carrys", {"posture": "probably-fine"}, {"symbols": ["x:*"]}):
            with _pytest.raises(jsonschema.ValidationError):
                jsonschema.validate(bad, field)

    def test_the_real_data_platform_manifest_validates_whole(self):
        """The end-to-end check the subschema tests cannot give: a real
        manifest carrying the real field passes the real schema."""
        import tomllib
        from pathlib import Path

        import jsonschema

        import axiom.extensions.builtins.data_platform as dp

        with (Path(dp.__file__).parent / "axiom-extension.toml").open("rb") as fh:
            manifest = tomllib.load(fh)
        assert "uncertainty" in manifest["extension"]
        jsonschema.validate(manifest, self._schema())
