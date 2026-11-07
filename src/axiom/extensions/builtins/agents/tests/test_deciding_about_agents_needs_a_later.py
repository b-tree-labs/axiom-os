# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Somebody new to agents has to be able to answer this, including "not yet".

A colleague met this prompt during onboarding on 2026-10-01, had never run a
background agent before, and could not tell what either answer would do:

    Enable which? [a]ll / [n]one / numbers e.g. 1,3 / ?N for details:

He pressed Enter. Three gaps made that the only reasonable move and the worst
informed one.

**There was no "not yet".** `none` records a permanent opt-out and stops the
offer. Somebody who wants to think about it is offered a choice between
installing an operating-system service and closing the subject, and neither is
what they mean.

**Enter was undocumented.** It raised, the caller printed "No changes", and
nothing said whether the subject was closed or would come back. Pressing the
key that looks safest left him unable to tell what he had just decided — which
is the same as not having decided, except that he thinks he has.

**The undo was never named.** Installing a service that survives reboots is a
reasonable thing to agree to only if you know how to stop it, and the prompt
that asks for it is the place to say so.

The names alone were also not enough to decide on. `TIDY, SCAN, PRESS` tells
somebody who has never seen them nothing about what would start running on
their machine.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.agents import consent

CANDIDATES = ["tidy", "scan", "press"]


def test_later_is_a_distinct_answer_from_opting_out():
    """The answer somebody new actually wants, and the one that was missing."""
    with pytest.raises(consent.Deferred):
        consent.parse_register_selection("later", CANDIDATES)
    with pytest.raises(consent.Deferred):
        consent.parse_register_selection("l", CANDIDATES)


def test_pressing_enter_means_later_rather_than_an_error():
    """Enter is what people press, so it has to mean the safe thing on purpose
    rather than fall through to a parse error that happens to be harmless."""
    with pytest.raises(consent.Deferred):
        consent.parse_register_selection("", CANDIDATES)
    with pytest.raises(consent.Deferred):
        consent.parse_register_selection("   ", CANDIDATES)


def test_deferring_is_still_a_valueerror_so_existing_callers_are_unchanged():
    """`Deferred` subclasses `ValueError` because the callers that already
    catch it and change nothing are doing the right thing. This lets the one
    caller that wants to say something truer tell the two apart."""
    assert issubclass(consent.Deferred, ValueError)


def test_later_records_nothing_at_all():
    """Deferring must not write a decision. A recorded "later" that reads as a
    decision on the next run is the bug this is preventing."""
    with pytest.raises(consent.Deferred):
        consent.parse_register_selection("later", CANDIDATES)


def test_none_still_means_do_not_ask_again():
    """Unchanged, and still distinct. Somebody who has decided they do not want
    this should not be asked every week."""
    enabled, opted_out = consent.parse_register_selection("none", CANDIDATES)
    assert enabled == []
    assert opted_out is True


def test_all_and_numbers_still_work():
    assert consent.parse_register_selection("all", CANDIDATES) == (CANDIDATES, False)
    assert consent.parse_register_selection("1,3", CANDIDATES) == (["tidy", "press"], False)


def test_a_wrong_answer_is_still_an_error_and_not_a_deferral():
    """Deferring on a typo would silently change nothing while reading as a
    deliberate choice."""
    with pytest.raises(ValueError) as refused:
        consent.parse_register_selection("yes please", CANDIDATES)
    assert not isinstance(refused.value, consent.Deferred)
    with pytest.raises(ValueError) as refused:
        consent.parse_register_selection("9", CANDIDATES)
    assert not isinstance(refused.value, consent.Deferred)


# ---------------------------------------------------------------------------
# What the prompt says
# ---------------------------------------------------------------------------


def _candidates():
    """Two agents shaped like the real ones, without discovery."""
    from types import SimpleNamespace

    def one(name, desc):
        return SimpleNamespace(
            name=name,
            description=desc,
            agent=SimpleNamespace(heartbeat_interval=3600, is_registrable=True),
        )

    return [one("tidy", "Keeps the workspace tidy"), one("scan", "Watches for drift")]


def _answering(monkeypatch, answer: str) -> list[str]:
    """Replace `input` with a faithful fake: it records the prompt, the way the
    real one shows it. A fake that discards the prompt makes every assertion
    about the prompt's wording vacuous."""
    seen: list[str] = []

    def fake(prompt: str = "") -> str:
        seen.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", fake)
    return seen


def test_the_prompt_offers_later_and_says_what_enter_does(monkeypatch, capsys):
    from axiom.extensions.builtins.agents import cli as agents_cli

    asked = _answering(monkeypatch, "later")
    with pytest.raises(consent.Deferred):
        agents_cli._interactive_select(_candidates())
    assert asked, "the prompt was never shown"
    question = asked[0]
    assert "later" in question.lower(), "the answer somebody new wants is not offered"
    assert "Enter" in question, "the key somebody will press has to be documented"
    assert "never ask again" in question, (
        "`none` is a permanent decision and has to read like one beside `later`"
    )


def test_the_prompt_names_the_undo(monkeypatch, capsys):
    """Agreeing to a service that survives reboots is reasonable only if you
    know how to stop it, and this is the place that asks."""
    from axiom.extensions.builtins.agents import cli as agents_cli

    _answering(monkeypatch, "later")
    with pytest.raises(consent.Deferred):
        agents_cli._interactive_select(_candidates())
    shown = capsys.readouterr().out
    assert "agents stop" in shown or "agents register --none" in shown, (
        "nothing in the prompt says how to undo what it is asking for"
    )


def test_each_agent_is_described_and_not_only_named(monkeypatch, capsys):
    """`TIDY, SCAN, PRESS` tells somebody who has never seen them nothing."""
    from axiom.extensions.builtins.agents import cli as agents_cli

    _answering(monkeypatch, "later")
    with pytest.raises(consent.Deferred):
        agents_cli._interactive_select(_candidates())
    shown = capsys.readouterr().out
    assert "Keeps the workspace tidy" in shown
    assert "Watches for drift" in shown


def test_deferring_says_the_offer_will_come_back(monkeypatch, capsys):
    """The thing he could not tell. Whichever way the answer goes, the surface
    has to state what it did."""
    from axiom.extensions.builtins.agents import cli as agents_cli

    _answering(monkeypatch, "")
    rc = agents_cli.run_interactive_registration_over(_candidates())
    assert rc == 0
    said = capsys.readouterr().out.lower()
    assert "comes back" in said or "again" in said, "nothing says the offer returns"
    assert "nothing changed" in said, "nothing says what the answer did"
