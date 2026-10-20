# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for `axiom.governance.controlled_code`, the controlled-code registry.

The registry answers *what is controlled, on what basis, under what rule*. It
does not gate anything, so the tests here are contract tests over a declarative
type: what a manifest may say, what the loader refuses, and what a query
returns.

The load-bearing property is the **three-valued** answer. An artifact nobody has
declared is ``UNKNOWN``, never ``NOT_CONTROLLED``. Any collapse of that back to
a two-valued answer reproduces the failure mode where a lookup that was never
wired reports everything as safe, so several tests here exist only to make that
collapse fail the suite.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from pathlib import Path

import pytest

from axiom.governance.classification import Classification
from axiom.governance.controlled_code import (
    CODE_SCHEME,
    AccessRule,
    ConflictingDeclaration,
    ControlBasis,
    ControlFinding,
    ControlledCodeEntry,
    ControlledCodeError,
    ControlledCodeRegistry,
    ControlStanding,
    ControlStatus,
    Declarations,
    ManifestError,
    UndeclaredArtifact,
    code_ref,
    load_manifest,
)
from axiom.governance.resource import ResourceRef

# ---------------------------------------------------------------------------
# Fixture manifests. Every artifact name here is fictional on purpose: Axiom
# ships the mechanism, a domain ships its own entries.
# ---------------------------------------------------------------------------

FLUXOMATIC_MANIFEST = """
schema_version = 1

[[code]]
artifact = "code://fluxomatic"
status = "controlled"
classification = "controlled"

[code.basis]
regime = "10 CFR Part 810"
citation = "site determination 2026-014"
determined_by = "@export-control-officer:example-org"
determined_on = 2026-01-15

[code.access_rule]
enclave = "example-enclave"
authorized_persons_ref = "roster://example-enclave/authorized-persons"
"""

SPROCKETFLUX_MANIFEST = """
schema_version = 1

[[code]]
artifact = "code://sprocketflux"
status = "not_controlled"

[code.basis]
citation = "site determination 2026-015"
determined_by = "@export-control-officer:example-org"
"""

# The same artifact as FLUXOMATIC_MANIFEST, declared the other way. Valid on its
# own terms, which is what makes it a genuine conflict rather than a bad entry.
FLUXOMATIC_DOWNGRADE_MANIFEST = SPROCKETFLUX_MANIFEST.replace(
    "code://sprocketflux", "code://fluxomatic"
).replace("2026-015", "2026-016")


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _basis(**overrides) -> ControlBasis:
    defaults = {
        "regime": "10 CFR Part 810",
        "citation": "site determination 2026-014",
        "determined_by": "@export-control-officer:example-org",
        "determined_on": date(2026, 1, 15),
    }
    defaults.update(overrides)
    return ControlBasis(**defaults)


def _entry(**overrides) -> ControlledCodeEntry:
    defaults = {
        "artifact": code_ref("fluxomatic"),
        "status": ControlStatus.CONTROLLED,
        "basis": _basis(),
        "access_rule": AccessRule(enclave="example-enclave"),
        "classification": Classification.CONTROLLED,
    }
    defaults.update(overrides)
    return ControlledCodeEntry(**defaults)


# ---------------------------------------------------------------------------
# A declared controlled artifact reports its basis and its access rule.
# ---------------------------------------------------------------------------


class TestDeclaredControlledArtifact:
    def test_lookup_reports_controlled(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(_write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST))
        finding = reg.lookup("fluxomatic")
        assert finding.standing is ControlStanding.CONTROLLED

    def test_finding_carries_the_control_basis(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(_write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST))
        entry = reg.lookup("fluxomatic").require_declared()
        assert entry.basis.regime == "10 CFR Part 810"
        assert entry.basis.citation == "site determination 2026-014"
        assert entry.basis.determined_by == "@export-control-officer:example-org"
        assert entry.basis.determined_on == date(2026, 1, 15)

    def test_finding_carries_the_access_rule(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(_write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST))
        entry = reg.lookup("fluxomatic").require_declared()
        assert entry.access_rule is not None
        assert entry.access_rule.enclave == "example-enclave"
        assert (
            entry.access_rule.authorized_persons_ref
            == "roster://example-enclave/authorized-persons"
        )

    def test_finding_names_the_declaring_source(self, tmp_path):
        path = _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST)
        reg = ControlledCodeRegistry.from_manifests(path)
        assert reg.lookup("fluxomatic").source == str(path)

    def test_lookup_accepts_a_resource_ref(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(_write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST))
        ref = ResourceRef(scheme=CODE_SCHEME, identifier="fluxomatic")
        assert reg.lookup(ref).standing is ControlStanding.CONTROLLED

    def test_lookup_accepts_a_full_uri_string(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(_write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST))
        assert reg.lookup("code://fluxomatic").standing is ControlStanding.CONTROLLED

    def test_lookup_is_exact_not_prefix(self, tmp_path):
        """Identity is the exact reference; a near-miss name is UNKNOWN, not a hit."""
        reg = ControlledCodeRegistry.from_manifests(_write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST))
        assert reg.lookup("fluxomatic-2").standing is ControlStanding.UNKNOWN


# ---------------------------------------------------------------------------
# Declared-not-controlled is a real declaration, distinct from silence.
# ---------------------------------------------------------------------------


class TestExplicitlyNotControlled:
    def test_declared_not_controlled_reports_not_controlled(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(
            _write(tmp_path, "b.toml", SPROCKETFLUX_MANIFEST)
        )
        assert reg.lookup("sprocketflux").standing is ControlStanding.NOT_CONTROLLED

    def test_not_controlled_is_distinguishable_from_undeclared(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(
            _write(tmp_path, "b.toml", SPROCKETFLUX_MANIFEST)
        )
        declared = reg.lookup("sprocketflux")
        silent = reg.lookup("never-heard-of-it")
        assert declared.standing is not silent.standing
        assert declared.is_declared is True
        assert silent.is_declared is False

    def test_not_controlled_still_carries_its_determination(self, tmp_path):
        """A not-controlled declaration without a determination is a rumour."""
        reg = ControlledCodeRegistry.from_manifests(
            _write(tmp_path, "b.toml", SPROCKETFLUX_MANIFEST)
        )
        entry = reg.lookup("sprocketflux").require_declared()
        assert entry.basis.citation == "site determination 2026-015"
        assert entry.basis.regime is None

    def test_only_an_explicit_declaration_is_known_not_controlled(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(
            _write(tmp_path, "b.toml", SPROCKETFLUX_MANIFEST),
            _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST),
        )
        assert reg.lookup("sprocketflux").known_not_controlled is True
        assert reg.lookup("fluxomatic").known_not_controlled is False
        assert reg.lookup("never-heard-of-it").known_not_controlled is False


# ---------------------------------------------------------------------------
# The unknown outcome, and the refusal to be a boolean.
# ---------------------------------------------------------------------------


class TestUndeclaredIsUnknownNotSafe:
    def test_undeclared_artifact_is_unknown(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(_write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST))
        finding = reg.lookup("never-heard-of-it")
        assert finding.standing is ControlStanding.UNKNOWN
        assert finding.standing is not ControlStanding.NOT_CONTROLLED
        assert finding.entry is None

    def test_unknown_has_no_declaring_source(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(_write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST))
        assert reg.lookup("never-heard-of-it").source is None

    def test_require_declared_raises_on_unknown(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(_write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST))
        with pytest.raises(UndeclaredArtifact) as exc:
            reg.lookup("never-heard-of-it").require_declared()
        assert "never-heard-of-it" in str(exc.value)

    @pytest.mark.parametrize("name", ["fluxomatic", "sprocketflux", "never-heard-of-it"])
    def test_a_finding_is_never_a_boolean(self, tmp_path, name):
        """Truth-testing any finding raises, so `if lookup(x):` cannot compile a lie.

        Uniform across all three standings on purpose. If only the unknown case
        refused, `if finding:` would work in every test a developer wrote and
        fail only against the artifact nobody had declared yet.
        """
        reg = ControlledCodeRegistry.from_manifests(
            _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST),
            _write(tmp_path, "b.toml", SPROCKETFLUX_MANIFEST),
        )
        finding = reg.lookup(name)
        with pytest.raises(TypeError) as exc:
            bool(finding)
        assert "standing" in str(exc.value)

    def test_finding_truthiness_is_refused_in_an_if_statement(self, tmp_path):
        reg = ControlledCodeRegistry.empty()
        with pytest.raises(TypeError):
            if reg.lookup("never-heard-of-it"):  # pragma: no cover - must raise
                pass

    def test_there_is_no_two_valued_query_on_the_registry(self):
        """The collapse this design exists to prevent: a bare boolean answer.

        If someone adds `is_controlled()` back, this fails and they have to read
        why. The three-valued `lookup` is the only query.
        """
        for banned in ("is_controlled", "controlled", "is_export_controlled"):
            assert not hasattr(ControlledCodeRegistry, banned), (
                f"{banned!r} would collapse the three-valued answer back to two; "
                "an undeclared artifact would report False and read as safe"
            )

    def test_there_is_no_two_valued_query_on_the_finding(self):
        for banned in ("is_controlled", "controlled", "is_safe", "allowed"):
            assert not hasattr(ControlFinding, banned)


# ---------------------------------------------------------------------------
# The empty registry.
# ---------------------------------------------------------------------------


class TestEmptyRegistry:
    def test_empty_registry_is_a_valid_state(self):
        reg = ControlledCodeRegistry.empty()
        assert reg.entries == ()
        assert reg.sources == ()

    def test_empty_registry_answers_unknown_to_everything(self):
        """Not 'nothing is controlled'. Nothing has been *declared*."""
        reg = ControlledCodeRegistry.empty()
        for name in ("fluxomatic", "sprocketflux", "anything-at-all"):
            finding = reg.lookup(name)
            assert finding.standing is ControlStanding.UNKNOWN
            assert finding.known_not_controlled is False

    def test_composing_zero_manifests_is_empty_not_an_error(self):
        assert ControlledCodeRegistry.from_manifests().entries == ()


# ---------------------------------------------------------------------------
# Composition across sources, and the conflict policy.
# ---------------------------------------------------------------------------


class TestComposition:
    def test_two_manifests_compose(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(
            _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST),
            _write(tmp_path, "b.toml", SPROCKETFLUX_MANIFEST),
        )
        assert len(reg.entries) == 2
        assert reg.lookup("fluxomatic").standing is ControlStanding.CONTROLLED
        assert reg.lookup("sprocketflux").standing is ControlStanding.NOT_CONTROLLED

    def test_both_sources_are_reported(self, tmp_path):
        a = _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST)
        b = _write(tmp_path, "b.toml", SPROCKETFLUX_MANIFEST)
        reg = ControlledCodeRegistry.from_manifests(a, b)
        assert set(reg.sources) == {str(a), str(b)}

    def test_identical_redeclaration_is_accepted(self, tmp_path):
        """Two sources that agree are not a conflict; a vendored copy must boot."""
        reg = ControlledCodeRegistry.from_manifests(
            _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST),
            _write(tmp_path, "a-copy.toml", FLUXOMATIC_MANIFEST),
        )
        assert len(reg.entries) == 1
        assert reg.lookup("fluxomatic").standing is ControlStanding.CONTROLLED

    def test_conflicting_redeclaration_is_refused(self, tmp_path):
        """One source says controlled, another says not. Nobody wins."""
        with pytest.raises(ConflictingDeclaration):
            ControlledCodeRegistry.from_manifests(
                _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST),
                _write(tmp_path, "downgrade.toml", FLUXOMATIC_DOWNGRADE_MANIFEST),
            )

    def test_conflict_message_names_both_sources_and_the_artifact(self, tmp_path):
        downgrade = FLUXOMATIC_MANIFEST.replace("example-enclave", "some-other-enclave")
        a = _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST)
        b = _write(tmp_path, "b.toml", downgrade)
        with pytest.raises(ConflictingDeclaration) as exc:
            ControlledCodeRegistry.from_manifests(a, b)
        message = str(exc.value)
        assert "code://fluxomatic" in message
        assert str(a) in message
        assert str(b) in message

    def test_conflict_is_refused_in_either_order(self, tmp_path):
        """Order independence is the proof that no source silently wins.

        Composing the safe declaration second must not let it be overwritten,
        and composing it first must not let it mask the disagreement.
        """
        a = _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST)
        b = _write(tmp_path, "downgrade.toml", FLUXOMATIC_DOWNGRADE_MANIFEST)
        with pytest.raises(ConflictingDeclaration):
            ControlledCodeRegistry.from_manifests(a, b)
        with pytest.raises(ConflictingDeclaration):
            ControlledCodeRegistry.from_manifests(b, a)

    def test_agreeing_sources_compose_the_same_in_either_order(self, tmp_path):
        a = _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST)
        b = _write(tmp_path, "b.toml", SPROCKETFLUX_MANIFEST)
        forward = ControlledCodeRegistry.from_manifests(a, b)
        reverse = ControlledCodeRegistry.from_manifests(b, a)
        assert set(forward.entries) == set(reverse.entries)

    def test_a_conflict_within_one_manifest_is_also_refused(self, tmp_path):
        doubled = FLUXOMATIC_MANIFEST + SPROCKETFLUX_MANIFEST.replace(
            "code://sprocketflux", "code://fluxomatic"
        ).replace("schema_version = 1", "")
        with pytest.raises(ConflictingDeclaration):
            ControlledCodeRegistry.from_manifests(_write(tmp_path, "a.toml", doubled))

    def test_a_non_file_source_composes_with_a_manifest(self, tmp_path):
        """Sites differ: a declaration source need not be a file on disk."""
        in_memory = Declarations.from_entries(
            [_entry(artifact=code_ref("gadgetron"))],
            origin="test://in-memory",
        )
        reg = ControlledCodeRegistry.compose(
            load_manifest(_write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST)),
            in_memory,
        )
        assert reg.lookup("gadgetron").standing is ControlStanding.CONTROLLED
        assert reg.lookup("gadgetron").source == "test://in-memory"


# ---------------------------------------------------------------------------
# Loading fails loudly. It never yields a quietly empty registry.
# ---------------------------------------------------------------------------


class TestManifestFailsLoudly:
    def test_missing_file_raises_naming_the_path(self, tmp_path):
        missing = tmp_path / "nope.toml"
        with pytest.raises(ManifestError) as exc:
            load_manifest(missing)
        assert str(missing) in str(exc.value)

    def test_malformed_toml_raises_naming_path_and_problem(self, tmp_path):
        path = _write(tmp_path, "bad.toml", "this is not = = toml\n")
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        message = str(exc.value)
        assert str(path) in message
        assert "TOML" in message

    def test_malformed_manifest_does_not_load_as_empty(self, tmp_path):
        """The failure this guards against: a silent `{}` that reads as 'clean'."""
        path = _write(tmp_path, "bad.toml", "this is not = = toml\n")
        with pytest.raises(ManifestError):
            ControlledCodeRegistry.from_manifests(path)

    def test_one_bad_manifest_fails_the_whole_composition(self, tmp_path):
        good = _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST)
        bad = _write(tmp_path, "bad.toml", "nope = = \n")
        with pytest.raises(ManifestError):
            ControlledCodeRegistry.from_manifests(good, bad)

    def test_missing_schema_version_raises(self, tmp_path):
        path = _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST.replace("schema_version = 1", ""))
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        assert "schema_version" in str(exc.value)

    def test_future_schema_version_raises(self, tmp_path):
        path = _write(
            tmp_path,
            "a.toml",
            FLUXOMATIC_MANIFEST.replace("schema_version = 1", "schema_version = 99"),
        )
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        assert "99" in str(exc.value)

    def test_unknown_top_level_key_raises(self, tmp_path):
        path = _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST + '\nwibble = "x"\n')
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        assert "wibble" in str(exc.value)

    def test_unknown_entry_key_raises(self, tmp_path):
        path = _write(
            tmp_path,
            "a.toml",
            FLUXOMATIC_MANIFEST.replace(
                'status = "controlled"', 'status = "controlled"\nwibble = "x"'
            ),
        )
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        assert "wibble" in str(exc.value)

    def test_missing_required_field_raises_naming_it(self, tmp_path):
        path = _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST.replace('status = "controlled"', ""))
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        assert "status" in str(exc.value)

    def test_unknown_status_raises_with_candidates(self, tmp_path):
        path = _write(
            tmp_path,
            "a.toml",
            FLUXOMATIC_MANIFEST.replace('status = "controlled"', 'status = "maybe"'),
        )
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        message = str(exc.value)
        assert "maybe" in message
        assert "not_controlled" in message

    def test_entry_error_names_the_artifact_it_came_from(self, tmp_path):
        path = _write(
            tmp_path,
            "a.toml",
            FLUXOMATIC_MANIFEST.replace('status = "controlled"', 'status = "maybe"'),
        )
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        assert "code://fluxomatic" in str(exc.value)

    def test_a_manifest_with_no_entries_loads_as_empty_declarations(self, tmp_path):
        """Deliberately empty is fine. It is *silently* empty that is the hazard."""
        path = _write(tmp_path, "a.toml", "schema_version = 1\n")
        assert load_manifest(path).entries == ()


# ---------------------------------------------------------------------------
# Doctrine, encoded in the schema: export control attaches to the code itself.
# ---------------------------------------------------------------------------


class TestDoctrineIsStructural:
    def test_inputs_and_outputs_may_egress(self, tmp_path):
        reg = ControlledCodeRegistry.from_manifests(_write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST))
        rule = reg.lookup("fluxomatic").require_declared().access_rule
        assert rule is not None
        assert rule.artifact_may_egress is False
        assert rule.inputs_and_outputs_may_egress is True

    def test_input_and_output_egress_is_not_a_settable_field(self):
        """Not a knob. No entry can be written that makes data export-controlled."""
        with pytest.raises(TypeError):
            AccessRule(enclave="e", inputs_and_outputs_may_egress=False)

    def test_a_non_code_artifact_cannot_be_declared(self, tmp_path):
        path = _write(
            tmp_path,
            "a.toml",
            FLUXOMATIC_MANIFEST.replace("code://fluxomatic", "dataset://reactor-runs-2026"),
        )
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        message = str(exc.value)
        assert "dataset" in message
        assert CODE_SCHEME in message

    def test_declaring_data_as_controlled_is_refused_with_the_doctrine(self, tmp_path):
        """The recurrent mistake gets the doctrine back, not a generic complaint."""
        path = _write(
            tmp_path,
            "a.toml",
            FLUXOMATIC_MANIFEST.replace(
                'status = "controlled"',
                'status = "controlled"\ncontrolled_data = true',
            ),
        )
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        message = str(exc.value)
        assert "controlled_data" in message
        assert "not export-controlled" in message

    def test_declaring_output_egress_is_refused_with_the_doctrine(self, tmp_path):
        path = _write(
            tmp_path,
            "a.toml",
            FLUXOMATIC_MANIFEST.replace(
                'enclave = "example-enclave"',
                'enclave = "example-enclave"\ninputs_and_outputs_may_egress = false',
            ),
        )
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        message = str(exc.value)
        assert "inputs_and_outputs_may_egress" in message
        assert "different regime" in message

    def test_an_ordinary_typo_does_not_get_the_doctrine_lecture(self, tmp_path):
        """Targeted, not blanket. A plain typo gets the expected-keys list."""
        path = _write(tmp_path, "a.toml", FLUXOMATIC_MANIFEST + '\nwibble = "x"\n')
        with pytest.raises(ManifestError) as exc:
            load_manifest(path)
        message = str(exc.value)
        assert "not export-controlled" not in message
        assert "expected one of" in message

    def test_a_bare_name_resolves_to_the_code_scheme(self):
        assert code_ref("fluxomatic") == ResourceRef(scheme=CODE_SCHEME, identifier="fluxomatic")

    def test_the_registry_does_not_enforce(self):
        """Enforcement lives elsewhere. Nothing here decides, permits or gates."""
        for verb in ("enforce", "decide", "permit", "deny", "authorize", "gate"):
            assert not hasattr(ControlledCodeRegistry, verb)


# ---------------------------------------------------------------------------
# Entry construction invariants, enforced in both directions.
# ---------------------------------------------------------------------------


class TestEntryInvariants:
    def test_controlled_entry_requires_an_access_rule(self):
        with pytest.raises(ControlledCodeError):
            _entry(access_rule=None)

    def test_controlled_entry_requires_a_regime(self):
        with pytest.raises(ControlledCodeError):
            _entry(basis=_basis(regime=None))

    def test_controlled_entry_requires_a_classification(self):
        with pytest.raises(ControlledCodeError):
            _entry(classification=None)

    @pytest.mark.parametrize("tier", [Classification.PUBLIC, Classification.INTERNAL])
    def test_controlled_entry_refuses_a_permissive_classification(self, tier):
        with pytest.raises(ControlledCodeError):
            _entry(classification=tier)

    def test_controlled_entry_accepts_regulated(self):
        """A controlled code need not be the top tier; EAR is REGULATED."""
        entry = _entry(classification=Classification.REGULATED)
        assert entry.classification is Classification.REGULATED

    def test_not_controlled_entry_refuses_an_access_rule(self):
        """An artifact that is not controlled has no enclave to be kept inside."""
        with pytest.raises(ControlledCodeError):
            ControlledCodeEntry(
                artifact=code_ref("sprocketflux"),
                status=ControlStatus.NOT_CONTROLLED,
                basis=_basis(regime=None),
                access_rule=AccessRule(enclave="example-enclave"),
                classification=None,
            )

    def test_not_controlled_entry_refuses_a_regime(self):
        with pytest.raises(ControlledCodeError):
            _entry(
                status=ControlStatus.NOT_CONTROLLED,
                access_rule=None,
                classification=None,
            )

    def test_not_controlled_entry_refuses_a_classification(self):
        with pytest.raises(ControlledCodeError):
            _entry(
                status=ControlStatus.NOT_CONTROLLED,
                basis=_basis(regime=None),
                access_rule=None,
                classification=Classification.CONTROLLED,
            )

    def test_basis_requires_a_citation(self):
        with pytest.raises(ControlledCodeError):
            _basis(citation="")

    def test_basis_requires_a_determining_authority(self):
        with pytest.raises(ControlledCodeError):
            _basis(determined_by="")

    def test_access_rule_requires_an_enclave(self):
        with pytest.raises(ControlledCodeError):
            AccessRule(enclave="")

    def test_every_error_is_a_controlled_code_error(self):
        for cls in (ManifestError, ConflictingDeclaration, UndeclaredArtifact):
            assert issubclass(cls, ControlledCodeError)

    def test_controlled_code_error_is_a_value_error(self):
        """House habit: loader problems are ValueErrors, catchable specifically."""
        assert issubclass(ControlledCodeError, ValueError)


# ---------------------------------------------------------------------------
# The mechanism is Axiom's; the entries are the domain's.
# ---------------------------------------------------------------------------

# Assembled from fragments so this file does not itself contain the token it
# searches for. The repo-wide public-mirror guard scans for the same name.
_DOMAIN_ARTIFACT = "MP" + "ACT"
_DOMAIN_ARTIFACT_RE = re.compile(rf"\b{_DOMAIN_ARTIFACT}\b", re.IGNORECASE)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNED_ROOTS = (_REPO_ROOT / "src" / "axiom", _REPO_ROOT / "tests" / "governance")
_TEXT_SUFFIXES = {".py", ".toml", ".md", ".txt", ".json", ".yaml", ".yml", ".cfg"}


def _scanned_files():
    for root in _SCANNED_ROOTS:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            yield path


class TestAxiomNamesNoDomainArtifact:
    def test_axiom_source_names_no_controlled_domain_artifact(self):
        """Axiom ships the registry. A domain ships its own entries, elsewhere."""
        offenders = [
            str(path)
            for path in _scanned_files()
            if _DOMAIN_ARTIFACT_RE.search(path.read_text(encoding="utf-8", errors="replace"))
        ]
        assert not offenders, (
            "domain artifact name found in Axiom: "
            + ", ".join(offenders)
            + ". The registry mechanism is Axiom's; the entries belong to the domain"
        )

    def test_the_scan_actually_reaches_the_registry_module(self):
        """A guard that scans nothing passes forever."""
        scanned = {p.name for p in _scanned_files()}
        assert "controlled_code.py" in scanned
        assert "test_controlled_code.py" in scanned

    def test_the_scanned_token_is_the_real_artifact_name(self):
        """Pin the token, or a typo in the assembly disarms the whole guard.

        Every other test in this class builds both the needle and the haystack
        from ``_DOMAIN_ARTIFACT``, so all of them still pass if that constant is
        wrong: the guard would search for a name nothing is called and report a
        clean tree forever. The plaintext is deliberately not written anywhere
        in this repository, so it is pinned by digest instead.
        """
        digest = hashlib.sha256(_DOMAIN_ARTIFACT.encode()).hexdigest()
        assert digest == ("3329d041cb9d0e5c47f7521b79504f0bd897a360e8115e6074ea5316ffd94f6a"), (
            "the scanned token is not the controlled artifact name it must be"
        )

    def test_the_scan_would_catch_a_violation(self, tmp_path):
        planted = tmp_path / "planted.py"
        planted.write_text(f'CODE = "{_DOMAIN_ARTIFACT}"\n', encoding="utf-8")
        assert _DOMAIN_ARTIFACT_RE.search(planted.read_text(encoding="utf-8"))

    def test_the_scan_does_not_fire_on_ordinary_english(self):
        """`impact` contains the token but is not a word-boundary match."""
        assert not _DOMAIN_ARTIFACT_RE.search("this has a large impact on latency")
