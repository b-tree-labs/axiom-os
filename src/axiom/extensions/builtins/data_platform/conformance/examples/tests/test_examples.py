# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""The worked normalizers, and the guarantee that every pathway agrees.

Two things are under test here.

The normalizers themselves, called as plain functions, because that is how their
author writes and debugs them and it needs no infrastructure at all.

And the property that matters once more than one surface exists: **direct
Python, the skill function, and the real ``conform_rows`` walk must produce
byte-identical canonical rows.** Three ways to reach one behaviour is a
convenience; three implementations of one behaviour is a bug waiting for the
day they disagree, and the disagreement always surfaces as data rather than as
an error.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.data_platform.conformance import (
    NormalizerRegistry,
    conform_rows,
)
from axiom.extensions.builtins.data_platform.conformance.examples.one_reading import (
    SCHEMA_REF as ONE_REF,
)
from axiom.extensions.builtins.data_platform.conformance.examples.one_reading import (
    one_reading,
)
from axiom.extensions.builtins.data_platform.conformance.examples.wide_frame import (
    SCHEMA_REF as FRAME_REF,
)
from axiom.extensions.builtins.data_platform.conformance.examples.wide_frame import (
    wide_frame,
)
from axiom.extensions.builtins.data_platform.skills import conform_try

TS = "2026-09-08T12:00:00+00:00"


def _record(schema_ref: str, row: dict, row_hash: str = "h1") -> dict:
    """A bronze ``_rows`` line, in the shape the edge writes."""
    return {
        "source_name": "example-connector",
        "item_id": "i1",
        "schema_ref": schema_ref,
        "row_hash": row_hash,
        "row": row,
        "tier": "bronze",
        "disposition": "allow",
        "raw_sha256": "r",
        "fetched_at": TS,
    }


# ---------------------------------------------------------------------------
# The normalizers, as plain functions
# ---------------------------------------------------------------------------


def test_one_reading_yields_one_signal():
    rows = list(
        one_reading(_record(ONE_REF, {"channel": "inlet", "ts": TS, "value": 21.5, "unit": "degC"}))
    )

    assert len(rows) == 1
    assert rows[0]["channel"] == "inlet"
    assert rows[0]["value"] == 21.5
    assert rows[0]["unit"] == "degC"


def test_a_missing_value_raises_rather_than_emitting_a_hole():
    """A null in silver is indistinguishable from a real reading of nothing.

    Raising costs one record, counted in the funnel's ``errored``. Emitting a
    null costs the ability to tell later whether the instrument was quiet or the
    normalizer was wrong.
    """
    with pytest.raises(KeyError):
        list(one_reading(_record(ONE_REF, {"channel": "inlet", "ts": TS})))


def test_a_wide_frame_fans_out_to_one_row_per_channel():
    rows = list(
        wide_frame(_record(FRAME_REF, {"ts": TS, "temp_a": 20.0, "temp_b": 21.0, "flow": 3.5}))
    )

    assert [r["channel"] for r in rows] == ["temperature.a", "temperature.b", "flow.primary"]
    assert {r["unit"] for r in rows} == {"degC", "L/min"}


def test_an_absent_channel_does_not_discard_the_present_ones():
    """Instruments drop in and out, and a raise would take the whole record.

    ``conform_rows`` skips the entire record when a normalizer raises, so
    raising on one optional missing channel would silently cost the others.
    """
    rows = list(wide_frame(_record(FRAME_REF, {"ts": TS, "temp_a": 20.0})))

    assert [r["channel"] for r in rows] == ["temperature.a"]


def test_a_duplicate_channel_raises_instead_of_vanishing():
    """The trap the primary key would otherwise resolve by silent deletion."""
    from axiom.extensions.builtins.data_platform.conformance.examples import wide_frame as mod

    original = dict(mod.CHANNELS)
    mod.CHANNELS["temp_b"] = "temperature.a"  # a collision, as a rename would cause
    try:
        with pytest.raises(ValueError, match="emitted twice"):
            list(mod.wide_frame(_record(FRAME_REF, {"ts": TS, "temp_a": 1.0, "temp_b": 2.0})))
    finally:
        mod.CHANNELS.clear()
        mod.CHANNELS.update(original)


# ---------------------------------------------------------------------------
# One spine, three pathways
# ---------------------------------------------------------------------------


def _registry() -> NormalizerRegistry:
    registry = NormalizerRegistry()
    registry.register(ONE_REF, one_reading)
    registry.register(FRAME_REF, wide_frame)
    return registry


def _via_conform_rows(tmp_path, record) -> list[dict]:
    """Path C: the real walk, over a real bronze tree."""
    rows_dir = tmp_path / "example-connector" / "_rows" / "2026-09-08"
    rows_dir.mkdir(parents=True)
    (rows_dir / "i1.jsonl").write_text(json.dumps(record) + "\n")

    collected: list[dict] = []
    conform_rows(
        tmp_path,
        _registry(),
        {"example-connector": "site-a"},
        upsert=collected.append,
    )
    return collected


def _via_skill(record, schema_ref, monkeypatch) -> list[dict]:
    """Path B: the skill function, which the CLI and MCP both wrap."""
    monkeypatch.setattr(conform_try, "_load_registry", lambda: (_registry(), ["examples"]))
    result = conform_try.run({"schema_ref": schema_ref, "record": record}, ctx=None)
    return result.value["rows"]


@pytest.mark.parametrize(
    "schema_ref,payload",
    [
        (ONE_REF, {"channel": "inlet", "ts": TS, "value": 21.5, "unit": "degC"}),
        (FRAME_REF, {"ts": TS, "temp_a": 20.0, "temp_b": 21.0, "flow": 3.5}),
    ],
)
def test_every_pathway_produces_the_same_canonical_rows(tmp_path, monkeypatch, schema_ref, payload):
    """Direct Python, the skill, and conform_rows must agree exactly.

    They are three doors onto one function, which is the design. This is the
    test that keeps it that way, because the day they diverge the symptom is
    wrong rows in silver rather than a stack trace, and nobody looks for a bug
    in the door they did not use.

    ``conform_rows`` additionally stamps ``site``, ``schema_ref`` and
    ``row_hash``, which the other two paths have no connector mapping to supply.
    Those are compared separately below rather than smuggled into the fixture.
    """
    record = _record(schema_ref, payload)
    registry = _registry()

    direct = [dict(r) for r in registry.get(schema_ref)(record)]
    skill = _via_skill(record, schema_ref, monkeypatch)
    walked = _via_conform_rows(tmp_path, record)

    assert direct == skill, "the skill must not reshape what the normalizer produced"

    stamped = [
        {k: v for k, v in row.items() if k not in ("site", "schema_ref", "row_hash")}
        for row in walked
    ]
    assert stamped == direct, "conform_rows must not reshape it either"


def test_conform_rows_is_the_only_path_that_stamps_platform_fields(tmp_path):
    """And it stamps them from the connector mapping, not from the normalizer.

    This is why a normalizer setting ``site`` itself is a warning: there would
    then be two sources for one field, and they would agree right up until a
    connector is remapped.
    """
    record = _record(ONE_REF, {"channel": "inlet", "ts": TS, "value": 1.0, "unit": "u"})
    walked = _via_conform_rows(tmp_path, record)

    assert walked[0]["site"] == "site-a"
    assert walked[0]["schema_ref"] == ONE_REF
    assert walked[0]["row_hash"] == "h1"


# ---------------------------------------------------------------------------
# What the skill adds on top of the spine
# ---------------------------------------------------------------------------


def test_the_skill_reports_a_duplicate_channel_rather_than_letting_silver_eat_it(monkeypatch):
    """The whole reason the dry-run exists.

    A normalizer without the defensive raise would yield two rows for one
    channel, the upsert would drop the second on conflict, and the funnel would
    still report two rows out.
    """

    def sloppy(record):
        yield {"stream": "s", "channel": "dup", "ts": TS, "value": 1.0, "unit": "u"}
        yield {"stream": "s", "channel": "dup", "ts": TS, "value": 2.0, "unit": "u"}

    registry = NormalizerRegistry()
    registry.register("sloppy/v1", sloppy)
    monkeypatch.setattr(conform_try, "_load_registry", lambda: (registry, ["x"]))

    result = conform_try.run(
        {"schema_ref": "sloppy/v1", "record": _record("sloppy/v1", {})}, ctx=None
    )

    assert result.ok is False
    assert any("emitted 2 times" in e for e in result.errors)
    assert any("ON CONFLICT DO NOTHING" in e for e in result.errors)


def test_the_skill_flags_a_naive_timestamp(monkeypatch):
    """silver.signals is timestamptz; a naive stamp is read as server-local."""
    registry = NormalizerRegistry()
    registry.register(
        "naive/v1",
        lambda rec: [
            {"stream": "s", "channel": "c", "ts": "2026-09-08T12:00:00", "value": 1.0, "unit": "u"}
        ],
    )
    monkeypatch.setattr(conform_try, "_load_registry", lambda: (registry, ["x"]))

    result = conform_try.run(
        {"schema_ref": "naive/v1", "record": _record("naive/v1", {})}, ctx=None
    )

    assert any("no timezone" in e for e in result.errors)


def test_listing_is_how_you_discover_your_entry_point_did_not_load(monkeypatch):
    monkeypatch.setattr(conform_try, "_load_registry", lambda: (_registry(), ["examples"]))

    result = conform_try.run({}, ctx=None)

    assert result.ok is True
    assert set(result.value["registered"]) == {ONE_REF, FRAME_REF}


def test_an_unknown_schema_ref_says_what_is_registered(monkeypatch):
    monkeypatch.setattr(conform_try, "_load_registry", lambda: (_registry(), ["examples"]))

    result = conform_try.run({"schema_ref": "nope/v1", "record": {}}, ctx=None)

    assert result.ok is False
    assert "portfolio_member" in result.errors[0]


# ---------------------------------------------------------------------------
# The CLI door exists, and reaches the same skill
# ---------------------------------------------------------------------------


def test_the_cli_verb_exists_and_maps_to_the_skill(capsys):
    """A RECIPE that documents a command nobody wired is worse than no RECIPE.

    ``main()`` maps a hyphenated verb to the snake_case skill name, so
    ``conform-try`` reaches ``data.conform_try``. This asserts the parser entry
    is actually there rather than trusting the mapping.
    """
    from axiom.extensions.builtins.data_platform import cli

    with pytest.raises(SystemExit):
        cli.main(["conform-try", "--help"])

    out = capsys.readouterr().out
    assert "--schema-ref" in out
    assert "--record" in out


def test_the_cli_lists_registered_normalizers():
    """The no-argument form, which is how you learn your entry point did not load."""
    from axiom.extensions.builtins.data_platform import cli

    assert cli.main(["--json", "conform-try"]) == 0


# ---------------------------------------------------------------------------
# Several spellings, one skill
# ---------------------------------------------------------------------------


def test_every_spelling_reaches_one_skill():
    """`conform-try` and `conform --dry-run` are aliases, not implementations.

    More than one way to say a thing is fine when the ways are cheap and
    predictable to maintain. That is only true while resolution happens in one
    table: the moment a second spelling grows its own code path, they can
    disagree, and the disagreement shows up as different output from what a
    reader believes is the same command.

    So this asserts on the resolver rather than on behaviour. If someone adds a
    third spelling, this test is where they declare it.
    """
    from argparse import Namespace

    from axiom.extensions.builtins.data_platform.cli import resolve_skill

    hyphenated, err = resolve_skill(Namespace(verb="conform-try"))
    assert (hyphenated, err) == ("conform_try", None)

    flagged, err = resolve_skill(Namespace(verb="conform", dry_run=True))
    assert (flagged, err) == ("conform_try", None)

    assert hyphenated == flagged, "both spellings must resolve to one skill name"


def test_the_bare_verb_says_where_the_real_pass_lives():
    """`conform` without --dry-run must not quietly do the dry run.

    Nor should it emit an argparse error. The honest answer names both doors:
    ``conform-try`` for the dry run, and ``conform-run`` for the real
    bronze-to-silver pass — saying so is cheaper than letting someone conclude
    the command is broken.
    """
    from argparse import Namespace

    from axiom.extensions.builtins.data_platform.cli import resolve_skill

    skill, err = resolve_skill(Namespace(verb="conform", dry_run=False))

    assert skill is None
    assert "conform-run" in err, "the error should name where the real pass lives"
    assert "conform-try" in err, "the error should name the spelling that does work"


def test_unaliased_verbs_still_map_by_hyphen_conversion():
    """The default path, which covers every verb that needs no alias."""
    from argparse import Namespace

    from axiom.extensions.builtins.data_platform.cli import resolve_skill

    assert resolve_skill(Namespace(verb="backup-validate")) == ("backup_validate", None)
    assert resolve_skill(Namespace(verb="list")) == ("list", None)


def test_both_cli_spellings_produce_identical_output(capsys):
    """End to end through argparse, not just the resolver.

    The resolver test above proves they name one skill. This proves nothing
    between argparse and the skill reshapes one spelling differently — the
    `--dry-run` flag, for instance, must not travel into the params dict for one
    spelling and not the other.
    """
    import json as _json

    from axiom.extensions.builtins.data_platform import cli

    record = _json.dumps({"row": {"ts": TS, "value": 1.0, "channel": "c", "unit": "u"}})

    assert cli.main(["--json", "conform-try", "--record", record]) == 0
    first = capsys.readouterr().out

    assert cli.main(["--json", "conform", "--dry-run", "--record", record]) == 0
    second = capsys.readouterr().out

    assert first == second, "two spellings of one command produced different output"
