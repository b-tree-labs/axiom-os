# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``whoami`` names the PERSON, and takes its parameters from any transport.

Two defects, found side by side. The report's only ``display_name`` was the
node's ("laptop:ben"), so neither the CLI nor an MCP client could say who the
person is — while the web chip could. And the MCP schema types ``sections`` and
``all`` as strings, so ``sections="you"`` became the unknown sections
``['y', 'o', 'u']`` and ``all="false"`` read as true.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.whoami import sections
from axiom.extensions.builtins.whoami.skills import _as_bool, _as_list


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (None, []),
        ("", []),
        ("you", ["you"]),
        ("you,access", ["you", "access"]),
        ("you access", ["you", "access"]),
        ('["you", "access"]', ["you", "access"]),
        (["you", "access"], ["you", "access"]),
    ],
)
def test_sections_arrive_as_a_list_from_any_transport(raw, want):
    assert _as_list(raw) == want


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("false", False),
        ("False", False),
        ("0", False),
        ("", False),
        (None, False),
        ("true", True),
        ("1", True),
        (True, True),
        (False, False),
    ],
)
def test_all_is_not_true_because_it_is_a_nonempty_string(raw, want):
    assert _as_bool(raw) is want


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Booth, Benjamin", "Benjamin Booth"),
        ("Benjamin Booth", "Benjamin Booth"),
        ("  Benjamin   Booth ", "Benjamin Booth"),
        ("Martin Luther King, Jr.", "Martin Luther King, Jr."),
        ("A, B, C", "A, B, C"),
    ],
)
def test_a_directory_name_reads_as_spoken(raw, want):
    assert sections._natural_name(raw) == want


def test_the_gate_account_names_the_owner(tmp_path, monkeypatch):
    accounts = tmp_path / "users.json"
    accounts.write_text(
        json.dumps(
            [
                {"email": "someone@else.org", "name": "Not Them"},
                {"email": "ada@example.org", "name": "Lovelace, Ada"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AXIOM_GATE_USERS_FILE", str(accounts))
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore.get",
        lambda self, key, default=None: "",
    )
    assert sections._person_name(["ADA@example.org"]) == (
        "Ada Lovelace",
        "gate account ada@example.org",
    )


def test_settings_win_over_the_gate(tmp_path, monkeypatch):
    accounts = tmp_path / "users.json"
    accounts.write_text(
        json.dumps([{"email": "ada@example.org", "name": "Gate Name"}]), encoding="utf-8"
    )
    monkeypatch.setenv("AXIOM_GATE_USERS_FILE", str(accounts))
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore.get",
        lambda self, key, default=None: "Ada Lovelace" if key == "user.name" else default,
    )
    assert sections._person_name(["ada@example.org"]) == ("Ada Lovelace", "settings user.name")


def test_no_name_anywhere_is_none_not_a_guess(monkeypatch):
    monkeypatch.delenv("AXIOM_GATE_USERS_FILE", raising=False)
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore.get",
        lambda self, key, default=None: "",
    )
    assert sections._person_name(["nobody@example.org"]) is None
