# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who arrival is for.

Walking stage 0 found the digest refusing to send with no recipient —
correctly — and nothing anywhere able to name one. A manifest-declared
schedule carries no params either, so the audience has to be declared
state rather than an argument.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.receipts.audience import (
    DEFAULT_SCHEDULE,
    Audience,
    audience_path,
    load_audience,
    no_audience_declared,
    save_audience,
    validate_audience,
)


def test_nothing_declared_is_not_an_empty_audience(tmp_path):
    """None means "nobody has said", which is a different fact from "the
    audience is empty" and wants a different sentence."""
    assert load_audience(state_dir=tmp_path) is None


def test_a_declaration_survives_a_round_trip(tmp_path):
    saved = Audience(
        recipients=("@robin", "@sam:local"),
        schedule="0 6 * * *",
        where="https://node.example/receipts/",
        site="local",
    )
    save_audience(saved, state_dir=tmp_path)
    loaded = load_audience(state_dir=tmp_path)
    assert loaded.recipients == ("@robin", "@sam:local")
    assert loaded.schedule == "0 6 * * *"
    assert loaded.where == "https://node.example/receipts/"
    assert loaded.site == "local"
    assert loaded.enabled is True


def test_one_recipient_written_without_brackets_is_still_one_recipient(tmp_path):
    """TOML makes this easy to get wrong, and reading a string as a list of
    characters would send a digest to "@", "r", "o"..."""
    path = audience_path(state_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[digest_audience]\nrecipients = "@robin"\n', encoding="utf-8")
    assert load_audience(state_dir=tmp_path).recipients == ("@robin",)


def test_an_unknown_key_is_kept_rather_than_dropped(tmp_path):
    """A field this version does not know about was written by something
    that did. Dropping it silently is worse than carrying it."""
    path = audience_path(state_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[digest_audience]\nrecipients = ["@robin"]\nquiet_hours = "22-06"\n', encoding="utf-8"
    )
    assert load_audience(state_dir=tmp_path).extra == {"quiet_hours": "22-06"}


def test_a_missing_schedule_falls_back_to_the_documented_default(tmp_path):
    path = audience_path(state_dir=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[digest_audience]\nrecipients = ["@robin"]\n', encoding="utf-8")
    assert load_audience(state_dir=tmp_path).schedule == DEFAULT_SCHEDULE


def test_the_default_schedule_is_a_cadence_this_platform_can_parse():
    """A default that does not parse is a default that fails on first use,
    and `cron:0 7 * * *` — the obvious-looking spelling — does not."""
    from axiom.extensions.builtins.schedule.formats import parse

    assert parse(DEFAULT_SCHEDULE) is not None


@pytest.mark.parametrize(
    "audience,expected",
    [
        (Audience(recipients=(), enabled=True), "recipients is empty"),
        (Audience(recipients=("robin",)), "principals are written @name"),
        (Audience(recipients=("@robin",), schedule=""), "schedule is empty"),
        (Audience(recipients=("@robin",), schedule="every tuesday"), "can parse"),
        (Audience(recipients=("@robin",), where="node.example"), "a link the digest can print"),
    ],
)
def test_validation_reports_each_problem_as_a_sentence(audience, expected):
    errors = validate_audience(audience)
    assert any(expected in e for e in errors), errors


def test_a_disabled_declaration_with_no_recipients_is_not_an_error():
    """Turning it off is what somebody going on leave wants, and is not the
    same as deleting who the audience is."""
    assert validate_audience(Audience(recipients=(), enabled=False)) == []


def test_a_valid_declaration_reports_nothing():
    """The validator must be capable of passing."""
    assert validate_audience(Audience(recipients=("@robin",))) == []


def test_the_refusal_names_the_file_and_the_table(tmp_path):
    """ "recipient is required" tells somebody they are stuck. This tells
    them what to write and where."""
    said = no_audience_declared(state_dir=tmp_path)
    assert str(audience_path(state_dir=tmp_path)) in said
    assert "digest_audience" in said
    assert "recipients" in said
