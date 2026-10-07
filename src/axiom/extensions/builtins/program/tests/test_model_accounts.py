# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The identity model: a person carries an optional external-account map.

Program membership is a PRINCIPAL, not a tracker seat. A person may declare
``accounts`` — ``{gitlab: <user>|null, github: <user>|null, …}`` — and the
feeders attribute external activity back to a principal through it. The model
validates the shape, carries it losslessly, and exposes the forward and
reverse lookups the feeders need, plus the deputy and lane-lead the proxy-
assignee rule falls back to.
"""

from __future__ import annotations

import copy

import pytest

from axiom.extensions.builtins.program.model import (
    ProgramData,
    ProgramValidationError,
    load_program,
    validate_program,
)
from axiom.extensions.builtins.program.tests.conftest import GENERIC_PROGRAM


def _with_accounts() -> dict:
    data = copy.deepcopy(GENERIC_PROGRAM)
    data["program"]["deputy"] = "@casey:example-org"
    data["lanes"][0]["lead"] = "@dana:example-org"  # alpha lane lead
    data["people"][0]["accounts"] = {"gitlab": "casey42", "github": "casey-gh"}
    data["people"][1]["accounts"] = {"gitlab": "dana7", "github": None}
    # rowan (people[2]) declares NO accounts map at all.
    return data


class TestAccountsValidation:
    def test_a_valid_accounts_map_passes(self):
        assert validate_program(_with_accounts()) == []

    def test_accounts_must_be_an_object(self):
        data = _with_accounts()
        data["people"][0]["accounts"] = ["gitlab", "casey42"]
        errors = validate_program(data)
        assert any("accounts" in e and "object" in e for e in errors)

    def test_an_account_value_must_be_a_string_or_null(self):
        data = _with_accounts()
        data["people"][0]["accounts"]["gitlab"] = 42
        errors = validate_program(data)
        assert any("accounts.gitlab" in e for e in errors)

    def test_an_unknown_system_key_is_carried_not_rejected(self):
        # The account systems are the deployment's data, not a closed set.
        data = _with_accounts()
        data["people"][0]["accounts"]["some-forge"] = "casey-x"
        assert validate_program(data) == []

    def test_a_person_may_omit_accounts_entirely(self):
        data = _with_accounts()
        assert "accounts" not in data["people"][2]
        assert validate_program(data) == []


class TestAccountAccessors:
    @pytest.fixture
    def program(self) -> ProgramData:
        return ProgramData(raw=_with_accounts())

    def test_account_forward_lookup(self, program):
        assert program.account("@casey:example-org", "gitlab") == "casey42"
        assert program.account("@dana:example-org", "github") is None  # explicit null
        assert program.account("@rowan:example-org", "gitlab") is None  # no map
        assert program.account("@nobody:example-org", "gitlab") is None  # no person

    def test_principal_for_account_reverse_lookup(self, program):
        assert program.principal_for_account("gitlab", "casey42") == "@casey:example-org"
        assert program.principal_for_account("gitlab", "dana7") == "@dana:example-org"
        assert program.principal_for_account("github", "casey-gh") == "@casey:example-org"
        assert program.principal_for_account("gitlab", "ghost") is None

    def test_reverse_lookup_ignores_null_accounts(self, program):
        # dana's github is explicitly null — it must not reverse-map.
        assert program.principal_for_account("github", "") is None
        assert program.principal_for_account("github", None) is None  # type: ignore[arg-type]

    def test_deputy_accessor(self, program):
        assert program.deputy() == "@casey:example-org"

    def test_lane_lead_accessor(self, program):
        assert program.lane_lead("alpha") == "@dana:example-org"
        assert program.lane_lead("beta") is None  # no lead declared
        assert program.lane_lead("nope") is None  # no such lane


class TestLosslessRoundTrip:
    def test_accounts_survive_a_save_load_round_trip(self, tmp_path):
        from axiom.extensions.builtins.program.model import save_program

        data = ProgramData(raw=_with_accounts())
        path = tmp_path / "data.json"
        save_program(data, path)
        reloaded = load_program(path)
        assert reloaded.account("@casey:example-org", "gitlab") == "casey42"
        assert reloaded.lane_lead("alpha") == "@dana:example-org"


class TestInvalidAccountsRefusesLikeAnyDefect:
    def test_load_raises_on_a_bad_accounts_map(self, tmp_path):
        import json

        data = _with_accounts()
        data["people"][0]["accounts"] = "casey42"
        path = tmp_path / "data.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ProgramValidationError):
            load_program(path)
