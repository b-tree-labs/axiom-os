# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A logbook declaration is validated when it loads, so a bad logbook fails at
start-up rather than at the moment someone tries to sign (spec-attestation,
Logbook declaration)."""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.extensions.builtins.attest.logbooks import LogbookError, load_logbook, parse_logbook

DEMO = Path(__file__).parent / "logbooks" / "demo_log.toml"


def test_loads_the_demo_logbook():
    logbook = load_logbook(DEMO)
    assert logbook.id == "demo_log" and logbook.version == "1"
    rc = logbook.type("ROUND_CHECK")
    assert rc.meanings == ("performed",)
    assert rc.roles == ("operator", "senior_operator")
    assert rc.field("reading").observe is True
    assert rc.posture == "sso"  # inherited from the logbook
    assert logbook.type("NOTE").posture == "attested"  # the type's own floor


def _logbook(**overrides) -> dict:
    data = {
        "logbook": {"id": "b", "version": "1", "display": "B"},
        "type": [{"id": "T", "meanings": ["authored"], "roles": ["r"], "fields": []}],
    }
    data.update(overrides)
    return data


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (_logbook(logbook={"id": "Bad-Id", "version": "1", "display": "B"}), "bus token"),
        (_logbook(logbook={"id": "b", "display": "B"}), "version"),
        (_logbook(type=[{"id": "T", "meanings": ["waved"], "roles": ["r"]}]), "meaning"),
        (_logbook(type=[{"id": "T", "meanings": [], "roles": ["r"]}]), "meaning"),
        (_logbook(type=[{"id": "T", "meanings": ["authored"], "roles": []}]), "role"),
        (
            _logbook(type=[{"id": "T", "meanings": ["authored"], "roles": ["node_admin"]}]),
            "administration",
        ),
        (
            _logbook(
                type=[
                    {"id": "T", "meanings": ["authored"], "roles": ["r"]},
                    {"id": "T", "meanings": ["authored"], "roles": ["r"]},
                ]
            ),
            "duplicate type",
        ),
        (
            _logbook(
                type=[
                    {
                        "id": "T",
                        "meanings": ["authored"],
                        "roles": ["r"],
                        "fields": [{"id": "a", "type": "text"}, {"id": "a", "type": "text"}],
                    }
                ]
            ),
            "duplicate field",
        ),
        (
            _logbook(
                type=[
                    {
                        "id": "T",
                        "meanings": ["authored"],
                        "roles": ["r"],
                        "fields": [{"id": "a", "type": "hologram"}],
                    }
                ]
            ),
            "field type",
        ),
        (
            _logbook(
                logbook={
                    "id": "b",
                    "version": "1",
                    "display": "B",
                    "assurance": {"posture": "open"},
                }
            ),
            "posture",
        ),
        (
            _logbook(
                logbook={
                    "id": "b",
                    "version": "1",
                    "display": "B",
                    "assurance": {"posture": "service"},
                }
            ),
            "posture",
        ),
        (_logbook(type=[]), "at least one type"),
    ],
)
def test_rejects_invalid_logbooks(data, message):
    with pytest.raises(LogbookError, match=message):
        parse_logbook(data)


def test_unknown_type_lookup_names_the_logbook():
    with pytest.raises(LogbookError, match="demo_log"):
        load_logbook(DEMO).type("NOPE")


# -- confirmation policy (ADR-144) ----------------------------------------------


def test_confirm_policy_is_read_from_the_type():
    c = load_logbook(DEMO).type("ROUND_CHECK").confirm
    assert c.modalities_any == ("screen", "cli", "voice")
    assert c.modalities_all == ()
    assert c.voice_confirm_allowed is True
    assert c.timeout_seconds == 600
    assert c.read_back == "on_voice_origin"


def test_confirm_policy_defaults_are_conservative():
    c = load_logbook(DEMO).type("NOTE").confirm
    assert (
        c.voice_confirm_allowed is False
    )  # a spoken "confirm" signs only where a logbook allows it
    assert c.timeout_seconds == 900
    assert c.read_back == "on_voice_origin"
    assert "screen" in c.modalities_any and "cli" in c.modalities_any


def _with_confirm(confirm: dict) -> dict:
    return _logbook(
        type=[{"id": "T", "meanings": ["authored"], "roles": ["r"], "confirm": confirm}]
    )


@pytest.mark.parametrize(
    ("confirm", "message"),
    [
        ({"modalities_any": ["telepathy"]}, "modality"),
        ({"modalities_any": ["screen"], "modalities_all": ["voice"]}, "modalities_all"),
        ({"timeout": "soon"}, "timeout"),
        ({"timeout": "0s"}, "timeout"),
        ({"read_back": "sometimes"}, "read_back"),
        ({"modalities_any": []}, "modality"),
    ],
)
def test_rejects_invalid_confirm_policies(confirm, message):
    with pytest.raises(LogbookError, match=message):
        parse_logbook(_with_confirm(confirm))


# -- assurance, devices and presence (ADR-146) -------------------------------------


def _type(**extra) -> dict:
    return _logbook(type=[{"id": "T", "meanings": ["authored"], "roles": ["r"], **extra}])


def test_signing_requirements_default_to_a_session_on_any_device_but_a_phone():
    t = parse_logbook(_type()).type("T")
    assert t.fresh_within_seconds is None
    assert t.sign_devices == ("personal", "kiosk", "tablet")
    assert t.presence is None


def test_fresh_within_is_read_from_the_type_or_inherited_from_the_logbook():
    t = parse_logbook(_type(assurance={"posture": "sso", "fresh_within": "5m"})).type("T")
    assert t.fresh_within_seconds == 300
    data = _type()
    data["logbook"]["assurance"] = {"posture": "sso", "fresh_within": "2m"}
    assert parse_logbook(data).type("T").fresh_within_seconds == 120


def test_devices_and_presence_are_read_from_the_type():
    t = parse_logbook(_type(devices={"sign": ["kiosk"]}, presence="control_room")).type("T")
    assert t.sign_devices == ("kiosk",)
    assert t.presence == "control_room"


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"devices": {"sign": ["toaster"]}}, "device class"),
        ({"devices": {"sign": []}}, "device class"),
        ({"presence": "Control Room"}, "presence"),
        ({"assurance": {"posture": "sso", "fresh_within": "later"}}, "fresh_within"),
    ],
)
def test_rejects_invalid_signing_requirements(extra, message):
    with pytest.raises(LogbookError, match=message):
        parse_logbook(_type(**extra))


# -- nothing a logbook declares is silently dropped ------------------------------------


def test_an_unknown_key_is_refused_not_ignored():
    with pytest.raises(LogbookError, match="unknown key.*rolse"):
        parse_logbook(
            _logbook(type=[{"id": "T", "meanings": ["authored"], "roles": ["r"], "rolse": ["x"]}])
        )
    with pytest.raises(LogbookError, match="unknown key.*colour"):
        parse_logbook({**_logbook(), "colour": "red"})
    with pytest.raises(LogbookError, match="unknown key.*obsrve"):
        parse_logbook(
            _logbook(type=[{"id": "T", "meanings": ["authored"], "roles": ["r"],
                         "fields": [{"id": "a", "type": "text", "obsrve": True}]}])
        )  # fmt: skip


def test_keys_for_later_phases_are_accepted_and_reported_as_not_yet_enforced():
    data = _logbook(
        type=[
            {
                "id": "T",
                "meanings": ["authored"],
                "roles": ["r"],
                "cosign": {"roles": ["r"]},
                "voice": {"phrases": ["t"]},
                "fields": [{"id": "a", "type": "readings", "from_site": "instruments"}],
            }
        ]  # fmt: skip
    )
    data["seal"] = {"by_roles": ["r"]}
    logbook = parse_logbook(data)
    assert set(logbook.not_yet_enforced) == {"seal", "T.cosign", "T.voice"}


# -- units and choices (a quantity names its unit) ------------------------------------


def _field(**f) -> dict:
    return _logbook(
        type=[{"id": "T", "meanings": ["performed"], "roles": ["r"], "fields": [{"id": "a", **f}]}]
    )


def test_a_quantity_names_its_unit_and_a_choice_its_choices():
    t = parse_logbook(_field(type="quantity", unit="kW")).type("T")
    assert t.field("a").unit == "kW"
    c = parse_logbook(_field(type="choice", choices=["in", "out"])).type("T")
    assert c.field("a").choices == ("in", "out")


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ({"type": "quantity"}, "unit"),
        ({"type": "quantity", "unit": ""}, "unit"),
        ({"type": "choice"}, "choices"),
        ({"type": "choice", "choices": ["a", "a"]}, "choices"),
        ({"type": "text", "choices": ["a"]}, "choices"),
        ({"type": "text", "unit": "kW"}, "unit"),
    ],
)
def test_units_and_choices_are_checked(field, message):
    with pytest.raises(LogbookError, match=message):
        parse_logbook(_field(**field))


# -- a manifest may declare a logbook (AEOS schema) ---------------------------------------


def _manifest(logbook: dict) -> dict:
    return {
        "extension": {
            "name": "x",
            "version": "0.1.0",
            "description": "d",
            "license": "Apache-2.0",
            "owner": "o",
            "aeos_version": "0.1.0",
            "builtin": True,
            "compatibility": {"python": ">= 3.11", "axiom": ">= 0.61.0"},
            "provides": [logbook],
        }
    }


def test_the_manifest_schema_admits_a_logbook_declaration():
    from axiom_tests._manifest import build_validator, validate_manifest

    ok = {
        "kind": "logbook",
        "id": "ops_logbook",
        "file": "logbook/ops_logbook.toml",
        "display": "Ops",
    }
    assert validate_manifest(_manifest(ok), validator=build_validator()) == []


@pytest.mark.parametrize(
    "logbook",
    [
        {"kind": "logbook", "file": "logbook/x.toml"},
        {"kind": "logbook", "id": "Bad-Id", "file": "logbook/x.toml"},
        {"kind": "logbook", "id": "x"},
        {"kind": "logbook", "id": "x", "file": "b.toml", "colour": "red"},
    ],
)
def test_the_manifest_schema_refuses_a_malformed_logbook_declaration(logbook):
    from axiom_tests._manifest import build_validator, validate_manifest

    assert validate_manifest(_manifest(logbook), validator=build_validator())
