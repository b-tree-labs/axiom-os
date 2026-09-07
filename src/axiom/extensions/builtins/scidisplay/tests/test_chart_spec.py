# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the chart-spec document: schema, open registries, round-trip.

Every claim the modules make in a docstring is pinned here, per the house rule.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from axiom.extensions.builtins.scidisplay import chart_spec as cs
from axiom.extensions.builtins.scidisplay.chart_spec import (
    ChartKind,
    ChartSpec,
    RegistrationError,
    SchemaVersionError,
    SpecFormatError,
    UnknownFieldError,
    UnregisteredKindError,
    UnregisteredWindowError,
    Window,
    WindowBasis,
    lookup_window_basis,
    parse_document,
    parse_json,
    register_kind,
    register_window_basis,
    registered_window_bases,
    unregister_kind,
)

# The canonical serialisation of the v0 nucleus. Written out in full because
# key order is part of the contract, not an implementation detail.
NUCLEUS_JSON = """{
  "schema_version": "1.0",
  "kind": "timeseries",
  "site": "alpha",
  "channels": [
    "ch-1",
    "ch-2",
    "ch-3",
    "ch-4"
  ],
  "window": {
    "from": "2026-07-15T00:00:00+00:00",
    "to": "2026-07-15T04:00:00+00:00"
  },
  "transform": {
    "bucket": "1m",
    "agg": "mean"
  },
  "title": "Four channels over four hours"
}"""


def nucleus_document() -> dict:
    return json.loads(NUCLEUS_JSON)


# ---------------------------------------------------------------------------
# The nucleus parses, and round-trips.
# ---------------------------------------------------------------------------


def test_nucleus_document_parses_into_the_agreed_fields():
    spec = parse_json(NUCLEUS_JSON)
    assert spec.schema_version == "1.0"
    assert spec.kind == "timeseries"
    assert spec.site == "alpha"
    assert spec.channels == ("ch-1", "ch-2", "ch-3", "ch-4")
    assert spec.window.fields["from"] == "2026-07-15T00:00:00+00:00"
    assert spec.window.fields["to"] == "2026-07-15T04:00:00+00:00"
    assert spec.transform.bucket == "1m"
    assert spec.transform.agg == "mean"
    assert spec.title == "Four channels over four hours"


def test_nucleus_round_trips_byte_identically():
    assert parse_json(NUCLEUS_JSON).to_json() == NUCLEUS_JSON


def test_serialize_then_parse_returns_an_identical_spec():
    spec = parse_json(NUCLEUS_JSON)
    assert parse_json(spec.to_json()) == spec


def test_document_key_order_is_stable():
    doc = parse_json(NUCLEUS_JSON).to_document()
    assert list(doc) == [
        "schema_version",
        "kind",
        "site",
        "channels",
        "window",
        "transform",
        "title",
    ]


def test_spec_id_is_stable_across_a_round_trip_and_changes_with_content():
    spec = parse_json(NUCLEUS_JSON)
    assert spec.spec_id.startswith("axiom://chart-spec/")
    assert parse_json(spec.to_json()).spec_id == spec.spec_id

    doc = nucleus_document()
    doc["title"] = "A different title"
    assert parse_document(doc).spec_id != spec.spec_id


# ---------------------------------------------------------------------------
# `kind` is an open registry, and unknown is a loud, specific failure.
# ---------------------------------------------------------------------------


def test_timeseries_is_registered_at_import():
    assert "timeseries" in cs.registered_kinds()


def test_unregistered_kind_fails_loudly_naming_the_registered_kinds():
    doc = nucleus_document()
    doc["kind"] = "machine_card"
    with pytest.raises(UnregisteredKindError) as exc:
        parse_document(doc)
    message = str(exc.value)
    assert "machine_card" in message
    assert "timeseries" in message, "the failure must name what IS registered"
    assert "register_kind" in message, "and how to add one"


def test_unregistered_kind_cannot_be_smuggled_past_the_parser():
    """Constructing a ChartSpec directly is checked too, so there is no path to
    a spec object carrying a kind nobody registered."""
    with pytest.raises(UnregisteredKindError):
        ChartSpec(kind="machine_card", site="alpha", title="t")


def test_a_domain_can_register_a_new_kind_and_it_then_validates():
    register_kind(
        ChartKind(
            name="machine_card",
            summary="An operator panel of current values.",
            requires=frozenset({"channels", "title"}),
        )
    )
    doc = {
        "schema_version": "1.0",
        "kind": "machine_card",
        "site": "alpha",
        "channels": ["ch-1"],
        "title": "Panel",
    }
    spec = parse_document(doc)
    assert spec.kind == "machine_card"
    assert spec.window is None
    assert spec.transform is None


def test_a_kind_rejects_a_field_it_does_not_permit():
    """A field a kind has no use for is refused, not quietly carried: a spec
    that renders identically with and without a field is not diffable."""
    register_kind(
        ChartKind(
            name="machine_card",
            summary="An operator panel of current values.",
            requires=frozenset({"channels", "title"}),
        )
    )
    doc = nucleus_document()
    doc["kind"] = "machine_card"
    with pytest.raises(SpecFormatError) as exc:
        parse_document(doc)
    assert "machine_card" in str(exc.value)
    assert "transform" in str(exc.value) or "window" in str(exc.value)


def test_a_kind_names_the_field_it_requires_when_it_is_missing():
    doc = nucleus_document()
    del doc["transform"]
    with pytest.raises(SpecFormatError) as exc:
        parse_document(doc)
    assert "transform" in str(exc.value)
    assert "timeseries" in str(exc.value)


def test_registering_the_same_kind_twice_identically_is_a_noop():
    entry = ChartKind(name="panel", summary="s", requires=frozenset({"title"}))
    register_kind(entry)
    register_kind(entry)
    assert cs.lookup_kind("panel") == entry


def test_a_conflicting_kind_registration_is_refused_not_resolved_by_precedence():
    register_kind(ChartKind(name="panel", summary="s", requires=frozenset({"title"})))
    with pytest.raises(RegistrationError) as exc:
        register_kind(ChartKind(name="panel", summary="different", requires=frozenset({"title"})))
    assert "panel" in str(exc.value)


def test_kind_names_are_lowercase_snake_case():
    with pytest.raises(RegistrationError):
        register_kind(ChartKind(name="MachineCard", summary="s", requires=frozenset()))


def test_a_kind_cannot_require_a_field_the_document_does_not_have():
    with pytest.raises(RegistrationError) as exc:
        register_kind(ChartKind(name="panel", summary="s", requires=frozenset({"colour"})))
    assert "colour" in str(exc.value)


def test_a_kind_cannot_claim_the_always_required_identity_fields():
    """`kind` and `site` are required of every document; a kind selecting them
    would imply they were ever optional."""
    with pytest.raises(RegistrationError):
        register_kind(ChartKind(name="panel", summary="s", requires=frozenset({"site"})))


def test_unregistering_a_kind_that_is_not_registered_fails_loudly():
    with pytest.raises(UnregisteredKindError):
        unregister_kind("never_registered")


# ---------------------------------------------------------------------------
# Additive optionality.
# ---------------------------------------------------------------------------


def test_adding_an_optional_field_does_not_break_a_spec_written_without_it():
    """The v0 shape of a kind, then the same kind with one more optional field:
    the document written against the older shape parses unchanged."""
    register_kind(ChartKind(name="panel", summary="s", requires=frozenset({"channels", "title"})))
    doc = {
        "schema_version": "1.0",
        "kind": "panel",
        "site": "alpha",
        "channels": ["ch-1"],
        "title": "Panel",
    }
    before = parse_document(doc)

    # The later release: same requirements, one more optional field.
    unregister_kind("panel")
    register_kind(
        ChartKind(
            name="panel",
            summary="s",
            requires=frozenset({"channels", "title"}),
            optional=frozenset({"transform"}),
        )
    )
    after = parse_document(doc)
    assert after == before
    assert after.to_json() == before.to_json()

    enriched = dict(doc, transform={"bucket": "5m", "agg": "max"})
    assert parse_document(enriched).transform.bucket == "5m"


def test_round_trip_with_every_optional_field_present():
    register_kind(
        ChartKind(
            name="panel",
            summary="s",
            requires=frozenset({"channels", "title"}),
            optional=frozenset({"window", "transform"}),
        )
    )
    register_window_basis(
        WindowBasis(
            name="sample_basis",
            summary="A window expressed as offsets from a run.",
            fields=("run", "from_offset", "to_offset"),
            optional_fields=("phase",),
        )
    )
    text = """{
  "schema_version": "1.0",
  "kind": "panel",
  "site": "alpha",
  "channels": [
    "ch-1"
  ],
  "window": {
    "basis": "sample_basis",
    "run": "r-1",
    "from_offset": "0s",
    "to_offset": "4h",
    "phase": "ramp"
  },
  "transform": {
    "bucket": "1m",
    "agg": "mean"
  },
  "title": "Everything"
}"""
    spec = parse_json(text)
    assert spec.to_json() == text
    assert parse_json(spec.to_json()) == spec


# ---------------------------------------------------------------------------
# schema_version.
# ---------------------------------------------------------------------------


def test_missing_schema_version_fails_rather_than_defaulting():
    doc = nucleus_document()
    del doc["schema_version"]
    with pytest.raises(SchemaVersionError) as exc:
        parse_document(doc)
    assert "schema_version" in str(exc.value)


@pytest.mark.parametrize("bad", ["", "1", "one.zero", "1.0.0", "v1.0", 1.0, None, ["1.0"]])
def test_malformed_schema_version_fails(bad):
    doc = nucleus_document()
    doc["schema_version"] = bad
    with pytest.raises(SchemaVersionError):
        parse_document(doc)


def test_a_future_major_version_fails_naming_what_is_supported():
    doc = nucleus_document()
    doc["schema_version"] = "2.0"
    with pytest.raises(SchemaVersionError) as exc:
        parse_document(doc)
    assert "1.0" in str(exc.value)


def test_a_future_minor_version_fails_because_it_may_carry_fields_this_build_would_drop():
    doc = nucleus_document()
    doc["schema_version"] = "1.9"
    with pytest.raises(SchemaVersionError) as exc:
        parse_document(doc)
    assert "1.9" in str(exc.value)


# ---------------------------------------------------------------------------
# Unknown fields are never dropped.
# ---------------------------------------------------------------------------


def test_an_unknown_document_field_fails_naming_what_is_allowed():
    # Deliberately a name nobody has claimed. A RESERVED name gets a more
    # specific message that names the reservation instead of the allow-list,
    # which TestReservedFieldNames covers.
    doc = nucleus_document()
    doc["colour"] = "blue"
    with pytest.raises(UnknownFieldError) as exc:
        parse_document(doc)
    assert "colour" in str(exc.value)
    assert "channels" in str(exc.value), "the failure lists the allowed keys"


def test_an_unknown_transform_field_fails():
    doc = nucleus_document()
    doc["transform"]["fill"] = "previous"
    with pytest.raises(UnknownFieldError) as exc:
        parse_document(doc)
    assert "fill" in str(exc.value)


def test_an_unknown_window_field_fails():
    doc = nucleus_document()
    doc["window"]["timezone"] = "UTC"
    with pytest.raises(UnknownFieldError) as exc:
        parse_document(doc)
    assert "timezone" in str(exc.value)


def test_a_misspelled_field_is_a_failure_not_a_silent_drop():
    doc = nucleus_document()
    doc["titel"] = doc.pop("title")
    with pytest.raises(SpecFormatError):
        parse_document(doc)


# ---------------------------------------------------------------------------
# The window: one shape today, room for a second.
# ---------------------------------------------------------------------------


def test_absolute_is_the_implicit_window_basis_at_schema_1_0():
    """A v0 window carries no basis tag; at schema 1.0 its absence means
    absolute. The rule is pinned by the schema version, not guessed."""
    spec = parse_json(NUCLEUS_JSON)
    assert spec.window.basis == "absolute"


def test_the_implicit_basis_is_not_re_emitted_so_a_v0_window_round_trips_exactly():
    doc = parse_json(NUCLEUS_JSON).to_document()
    assert "basis" not in doc["window"]


# The fixture basis below is named `sample_basis` on purpose. An earlier version
# called it `run_relative`, and when that shape shipped for real these tests
# collided with the registry. A test that needs an UNREGISTERED name must pick
# one nobody would ever register.
def test_a_second_window_shape_is_representable_without_altering_the_first():
    """Requirement 3: run-relative time is a different *shape*, not an extra
    field. Registering a second basis leaves the absolute one untouched."""
    register_window_basis(
        WindowBasis(
            name="sample_basis",
            summary="A window expressed as offsets from a run.",
            fields=("run", "from_offset", "to_offset"),
        )
    )
    register_kind(
        ChartKind(
            name="campaign",
            summary="s",
            requires=frozenset({"channels", "window", "transform", "title"}),
        )
    )
    doc = {
        "schema_version": "1.0",
        "kind": "campaign",
        "site": "alpha",
        "channels": ["ch-1"],
        "window": {
            "basis": "sample_basis",
            "run": "r-1",
            "from_offset": "0s",
            "to_offset": "4h",
        },
        "transform": {"bucket": "1m", "agg": "mean"},
        "title": "One campaign",
    }
    spec = parse_document(doc)
    assert spec.window.basis == "sample_basis"
    assert spec.window.fields["run"] == "r-1"
    assert "from" not in spec.window.fields

    # And the absolute form is entirely unaffected.
    assert parse_json(NUCLEUS_JSON).to_json() == NUCLEUS_JSON


def test_an_unregistered_window_basis_fails_naming_the_registered_ones():
    doc = nucleus_document()
    doc["window"] = {"basis": "sample_basis", "run": "r-1"}
    with pytest.raises(UnregisteredWindowError) as exc:
        parse_document(doc)
    assert "sample_basis" in str(exc.value)
    assert "absolute" in str(exc.value)


def test_a_window_basis_cannot_claim_the_basis_tag_as_a_field():
    with pytest.raises(RegistrationError):
        register_window_basis(WindowBasis(name="odd", summary="s", fields=("basis",)))


def test_a_window_basis_carries_its_own_value_checks():
    """The absolute basis' timestamp rules do not apply to another shape, so
    each basis supplies its own validator rather than the parser branching."""

    def _check(fields):
        if not fields["run"].startswith("r-"):
            raise SpecFormatError("window [sample_basis]: 'run' must start with 'r-'")

    register_window_basis(
        WindowBasis(
            name="sample_basis",
            summary="s",
            fields=("run",),
            validate=_check,
        )
    )
    register_kind(ChartKind(name="campaign", summary="s", requires=frozenset({"window"})))
    doc = {
        "schema_version": "1.0",
        "kind": "campaign",
        "site": "alpha",
        "window": {"basis": "sample_basis", "run": "nope"},
    }
    with pytest.raises(SpecFormatError) as exc:
        parse_document(doc)
    assert "must start with" in str(exc.value)


def test_absolute_window_requires_an_offset_bearing_timestamp():
    doc = nucleus_document()
    doc["window"]["from"] = "2026-07-15T00:00:00"
    with pytest.raises(SpecFormatError) as exc:
        parse_document(doc)
    assert "offset" in str(exc.value).lower()


def test_absolute_window_rejects_a_timestamp_that_is_not_a_timestamp():
    doc = nucleus_document()
    doc["window"]["to"] = "next tuesday"
    with pytest.raises(SpecFormatError):
        parse_document(doc)


def test_absolute_window_requires_from_before_to():
    doc = nucleus_document()
    doc["window"]["from"], doc["window"]["to"] = doc["window"]["to"], doc["window"]["from"]
    with pytest.raises(SpecFormatError) as exc:
        parse_document(doc)
    assert "from" in str(exc.value) and "to" in str(exc.value)


def test_a_window_missing_a_required_field_fails():
    doc = nucleus_document()
    del doc["window"]["to"]
    with pytest.raises(SpecFormatError) as exc:
        parse_document(doc)
    assert "to" in str(exc.value)


def test_window_equality_is_by_value():
    a = Window(fields={"from": "2026-07-15T00:00:00+00:00", "to": "2026-07-15T04:00:00+00:00"})
    b = Window(fields={"from": "2026-07-15T00:00:00+00:00", "to": "2026-07-15T04:00:00+00:00"})
    assert a == b


# ---------------------------------------------------------------------------
# Field-level value rules.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channels", [[], ["ch-1", "ch-1"], ["ch-1", ""], ["ch-1", 7], "ch-1"])
def test_channels_must_be_a_non_empty_list_of_distinct_non_empty_strings(channels):
    doc = nucleus_document()
    doc["channels"] = channels
    with pytest.raises(SpecFormatError):
        parse_document(doc)


def test_a_duplicate_channel_names_itself():
    doc = nucleus_document()
    doc["channels"] = ["ch-1", "ch-1"]
    with pytest.raises(SpecFormatError) as exc:
        parse_document(doc)
    assert "ch-1" in str(exc.value)


@pytest.mark.parametrize("bucket", ["", "1", "m", "0s", "1 m", "five minutes", "1M", 60])
def test_transform_bucket_must_be_a_duration(bucket):
    doc = nucleus_document()
    doc["transform"]["bucket"] = bucket
    with pytest.raises(SpecFormatError) as exc:
        parse_document(doc)
    assert "bucket" in str(exc.value)


@pytest.mark.parametrize("bucket", ["500ms", "1s", "30s", "1m", "15m", "1h", "1d"])
def test_transform_bucket_accepts_the_documented_duration_units(bucket):
    doc = nucleus_document()
    doc["transform"]["bucket"] = bucket
    assert parse_document(doc).transform.bucket == bucket


@pytest.mark.parametrize("agg", ["", "Mean", "5", "mean()", None])
def test_transform_agg_must_be_a_lowercase_identifier(agg):
    doc = nucleus_document()
    doc["transform"]["agg"] = agg
    with pytest.raises(SpecFormatError):
        parse_document(doc)


def test_agg_vocabulary_is_deliberately_open():
    """Axiom cannot know which aggregations a given store implements, so the
    schema checks the shape and leaves the vocabulary to the renderer."""
    doc = nucleus_document()
    doc["transform"]["agg"] = "p95"
    assert parse_document(doc).transform.agg == "p95"


@pytest.mark.parametrize("site", ["", "   ", " alpha", "alpha ", 7, None])
def test_site_must_be_a_non_empty_string_without_surrounding_space(site):
    doc = nucleus_document()
    doc["site"] = site
    with pytest.raises(SpecFormatError):
        parse_document(doc)


def test_title_must_be_a_non_empty_string():
    doc = nucleus_document()
    doc["title"] = "   "
    with pytest.raises(SpecFormatError):
        parse_document(doc)


def test_a_document_must_be_a_mapping():
    with pytest.raises(SpecFormatError):
        parse_document(["not", "a", "mapping"])


def test_parse_json_rejects_text_that_is_not_json():
    with pytest.raises(SpecFormatError):
        parse_json("{not json")


# ---------------------------------------------------------------------------
# The line that must hold.
# ---------------------------------------------------------------------------

SOURCE_MODULES = ("chart_spec.py", "channel_catalog.py", "chart_validation.py")

# Word-boundary matched, case-insensitive. Generic domain VOCABULARY only.
#
# Real site, node and facility names are deliberately absent. The public-mirror
# guard (tests/test_mirror.py) already forbids them across the whole public
# surface, so listing one here would be duplicate coverage, and naming a real
# node in a source file is exactly what that guard fails on. This list caught
# nothing the mirror would not; the mirror caught this list.
DOMAIN_NOUNS = (
    "nuclear",
    "reactor",
    "triga",
    "netl",
    "coolant",
    "thermocouple",
    "salt",
    "fuel",
    "isotope",
    "irradiation",
    "criticality",
    "pile",
    "rod",
    "assembly",
    "detector",
)


def _source_dir() -> Path:
    return Path(cs.__file__).parent


def test_the_schema_names_no_domain_noun():
    """The schema knows `site` and `channel` as field names carrying opaque
    values. It must never learn what a value means."""
    for module in SOURCE_MODULES:
        text = (_source_dir() / module).read_text(encoding="utf-8")
        for noun in DOMAIN_NOUNS:
            assert not re.search(rf"\b{noun}s?\b", text, re.IGNORECASE), (
                f"{module} names the domain noun {noun!r}; the schema carries "
                "domain values, it does not know domain vocabulary"
            )


def test_the_schema_imports_nothing_that_touches_a_database():
    forbidden = ("sqlalchemy", "psycopg", "axiom.infra.db", "asyncpg", "alembic")
    for module in SOURCE_MODULES:
        text = (_source_dir() / module).read_text(encoding="utf-8")
        for name in forbidden:
            assert name not in text, f"{module} reaches for {name!r}; the schema is inert"


class TestReservedFieldNames:
    """Names claimed for known future work are refused BY NAME, not merely as unknown.

    Reserving costs nothing and prevents the one expensive outcome: an
    unrelated field taking the name first, forcing a rename across a
    contract two sessions have agreed.
    """

    def test_a_reserved_field_is_refused(self) -> None:
        doc = nucleus_document()
        doc["marks"] = [{"kind": "band", "from": 0, "to": 1}]
        with pytest.raises(UnknownFieldError):
            parse_document(doc)

    def test_the_refusal_says_reserved_not_merely_unknown(self) -> None:
        doc = nucleus_document()
        doc["device"] = "pump-3"
        with pytest.raises(UnknownFieldError) as excinfo:
            parse_document(doc)
        message = str(excinfo.value)
        assert "reserved" in message.lower()
        assert "device" in message
        # It names the whole reserved set, so a reader learns what else is claimed.
        assert "marks" in message

    def test_an_ordinary_unknown_field_is_still_refused_as_unknown(self) -> None:
        doc = nucleus_document()
        doc["colour"] = "blue"
        with pytest.raises(UnknownFieldError) as excinfo:
            parse_document(doc)
        assert "reserved" not in str(excinfo.value).lower()


class TestInvisibleCharacters:
    """A character nobody can see still changes what a spec IS.

    A model fills this schema, and a generation pipeline can embed zero-width
    characters in returned text. One inside a title yields a spec that reads
    identically to another, compares unequal, and cites differently. Refusing
    beats stripping: stripping edits a caller's text behind their back.
    """

    @pytest.mark.parametrize(
        "codepoint,what",
        [
            (0x200B, "zero width space"),
            (0x200D, "zero width joiner"),
            (0xFEFF, "byte order mark"),
            (0xE0041, "tag character, the watermarking kind"),
        ],
    )
    def test_an_invisible_character_in_a_title_is_refused(self, codepoint, what) -> None:
        doc = nucleus_document()
        doc["title"] = f"Salt temperatures{chr(codepoint)} July"
        with pytest.raises(SpecFormatError) as excinfo:
            parse_document(doc)
        message = str(excinfo.value)
        assert f"U+{codepoint:04X}" in message, "the failure names the codepoint"
        assert "invisible" in message.lower()

    def test_the_failure_says_where_in_the_string(self) -> None:
        doc = nucleus_document()
        doc["title"] = f"ab{chr(0x200B)}cd"
        with pytest.raises(SpecFormatError) as excinfo:
            parse_document(doc)
        assert "position 2" in str(excinfo.value)

    @pytest.mark.parametrize(
        "title",
        [
            "Réacteur température",  # accented Latin
            "反応炉の温度",  # CJK
            "Temperature 25 °C ± 0.5",  # symbols
            "Run 3 · baseline",  # a middle dot is visible and fine
        ],
    )
    def test_ordinary_international_text_still_passes(self, title) -> None:
        doc = nucleus_document()
        doc["title"] = title
        assert parse_document(doc).title == title

    def test_a_refused_title_never_becomes_a_silently_edited_one(self) -> None:
        # The alternative design was to strip. That would break the round-trip
        # guarantee in a subtler way, so this pins refusal over repair.
        doc = nucleus_document()
        doc["title"] = f"clean{chr(0x200B)}"
        with pytest.raises(SpecFormatError):
            parse_document(doc)


class TestRunRelativeWindow:
    """A window measured from the start of a run, not from a wall clock.

    Two runs of a campaign start at different moments and last different
    lengths. Overlaying them on absolute time compares nothing; overlaying
    them on time-since-start compares the thing the researcher cares about.
    The shape also outlives its own resolution: an absolute window stops
    meaning anything once the campaign is re-run, and this one does not.
    """

    def doc(self, **over):
        window = {"basis": "run_relative", "run": "vcu_30propTest07_24", "from": "+0h", "to": "+6h"}
        window.update(over.pop("window", {}))
        d = nucleus_document(**over)
        d["window"] = window
        return d

    def test_a_run_relative_window_parses(self) -> None:
        spec = parse_document(self.doc())
        assert spec.window.basis == "run_relative"
        assert spec.window.fields["run"] == "vcu_30propTest07_24"
        assert spec.window.fields["from"] == "+0h"

    def test_it_round_trips_and_keeps_its_tag(self) -> None:
        # Unlike the absolute shape, this basis is not the default, so the
        # tag must survive serialisation or the document changes meaning.
        doc = self.doc()
        spec = parse_document(doc)
        again = spec.to_document()
        assert again["window"]["basis"] == "run_relative"
        assert parse_document(again).spec_id == spec.spec_id

    def test_field_order_is_run_then_from_then_to(self) -> None:
        spec = parse_document(self.doc())
        assert list(spec.window.fields) == ["run", "from", "to"]

    def test_a_run_relative_spec_is_not_the_same_object_as_an_absolute_one(self) -> None:
        # Deliberate. The run-relative spec is a reusable analysis template
        # and still means something after the campaign is re-run; the
        # absolute one is a frozen citation. Different meanings, different
        # citable objects. A pin carries the bridge between them.
        relative = parse_document(self.doc())
        absolute = parse_document(nucleus_document())
        assert relative.spec_id != absolute.spec_id

    # --- the run reference -------------------------------------------------

    @pytest.mark.parametrize(
        "run",
        [
            "tamu_2025_06_19_2",
            "vcu_30propTest07_24",  # real names carry capitals
            "30propTest06_24",  # and start with a digit
            "2025_12_30_1",
            "r.1-a",
        ],
    )
    def test_real_run_identifiers_are_accepted(self, run) -> None:
        spec = parse_document(self.doc(window={"run": run}))
        assert spec.window.fields["run"] == run

    @pytest.mark.parametrize(
        "run,why",
        [
            ("has space", "whitespace"),
            ("a/b", "a path separator"),
            ("_leading", "a leading punctuation character"),
            ("x" * 65, "longer than the limit"),
            ("", "empty"),
        ],
    )
    def test_an_unusable_run_reference_is_refused(self, run, why) -> None:
        with pytest.raises(SpecFormatError):
            parse_document(self.doc(window={"run": run}))

    # --- the offsets -------------------------------------------------------

    @pytest.mark.parametrize(
        "frm,to", [("+0h", "+6h"), ("-30s", "+120s"), ("+0ms", "+1d"), ("-1d", "-1h")]
    )
    def test_signed_offsets_in_the_shared_unit_set(self, frm, to) -> None:
        spec = parse_document(self.doc(window={"from": frm, "to": to}))
        assert spec.window.fields["from"] == frm

    @pytest.mark.parametrize(
        "bad,why",
        [
            ("6h", "no sign, so the direction is a guess"),
            ("+6", "no unit"),
            ("+6w", "a unit the bucket grammar does not have"),
            ("+ 6h", "a space"),
            ("+06h", "a leading zero"),
            ("++6h", "two signs"),
        ],
    )
    def test_an_offset_that_is_not_spelled_the_agreed_way_is_refused(self, bad, why) -> None:
        with pytest.raises(SpecFormatError) as excinfo:
            parse_document(self.doc(window={"from": bad}))
        assert "from" in str(excinfo.value)

    def test_the_sign_is_required_even_on_zero(self) -> None:
        # "+0h" and "0h" would otherwise be two spellings of one offset, and
        # the spec is content-addressed: two spellings are two citable objects.
        with pytest.raises(SpecFormatError):
            parse_document(self.doc(window={"from": "0h"}))

    def test_from_must_precede_to_across_units(self) -> None:
        with pytest.raises(SpecFormatError) as excinfo:
            parse_document(self.doc(window={"from": "+2h", "to": "+30m"}))
        assert "before" in str(excinfo.value)

    @pytest.mark.parametrize(
        "frm,to",
        [
            ("+500ms", "+1s"),
            ("+90s", "+2m"),
            ("+90m", "+2h"),
            ("+25h", "+2d"),
        ],
    )
    def test_each_unit_converts_against_its_neighbour(self, frm, to) -> None:
        # Each pair is ordered only if BOTH units convert correctly, and each
        # one straddles a unit boundary (90s really is less than 2m). A unit
        # table where two entries collide, or one is off by its factor, makes
        # one of these compare backwards and refuse a legitimate window.
        # Without this, corrupting the table passes every other test here.
        spec = parse_document(self.doc(window={"from": frm, "to": to}))
        assert spec.window.fields["to"] == to

    def test_an_empty_window_is_refused(self) -> None:
        with pytest.raises(SpecFormatError):
            parse_document(self.doc(window={"from": "+1h", "to": "+1h"}))

    def test_a_window_spanning_zero_is_ordinary(self) -> None:
        # The pulse case: anchor in the middle, look before and after.
        spec = parse_document(self.doc(window={"from": "-30s", "to": "+120s"}))
        assert spec.window.fields["to"] == "+120s"

    # --- what the schema deliberately does NOT decide ----------------------

    def test_a_window_longer_than_any_run_still_parses(self) -> None:
        # The schema cannot know how long a run lasted, so it must not
        # pretend to. Clipping to the run's extent, and showing short
        # coverage as coverage, is the resolver's contract, not the
        # document's. A spec that fails here would fail for every run of a
        # campaign because one run was short.
        spec = parse_document(self.doc(window={"from": "+0h", "to": "+6h"}))
        assert spec.window.fields["to"] == "+6h"

    def test_the_basis_carries_no_anchor_field(self) -> None:
        # The anchor is the run's start, implicitly. Event-relative
        # alignment (a pulse trigger, say) is a DIFFERENT shape that
        # registers under its own name later; this one does not claim the
        # general case it only half covers.
        assert "anchor" not in lookup_window_basis("run_relative").fields
        with pytest.raises(SpecFormatError):
            parse_document(self.doc(window={"anchor": "pulse"}))

    def test_the_name_leaves_room_for_a_second_relative_shape(self) -> None:
        assert "run_relative" in registered_window_bases()
        assert "relative" not in registered_window_bases()

    def test_the_absolute_shape_is_untouched(self) -> None:
        spec = parse_document(nucleus_document())
        assert spec.window.basis == "absolute"
        assert "basis" not in spec.to_document()["window"]
