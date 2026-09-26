# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Offering the upgrade: one durable alert, and a question with a sane default.

Two things have to be true for this to be an improvement rather than a nag.

It must be announced through the notification channel, durably, so the person
sees it wherever they read alerts rather than only in the terminal that
happened to be open. That path is `notifications alert`, and it takes a
`dedup_key` — keyed to the version, so a release is announced once and not on
every launch. Without the key this becomes the thing people mute.

And the question must be answerable by pressing Enter. Someone who wants the
update should not have to type a word; someone who does not should not be
opted in by walking away, which is why the prompt is only asked when there is
a person at the terminal.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.update.offer import (
    UPGRADE_CHOICES,
    announce_update,
    is_version_skipped,
    offer_update_interactively,
    resolve_choice,
    should_offer_update,
    skip_version,
)


class _Alerts:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return {"ok": True}


def test_it_announces_through_the_notification_channel():
    alerts = _Alerts()

    announce_update(
        product="Neutron OS", current="1.4.1", available="1.11.1",
        notes="• Monitors stopped lying about delivery",
        recipient="@sam:netl", send=alerts,
    )

    assert len(alerts.calls) == 1
    sent = alerts.calls[0]
    assert sent["recipient"] == "@sam:netl"
    assert "1.11.1" in sent["summary"]


def test_the_alert_is_deduped_per_version():
    """A release is news once. Re-announcing every launch is how an operator
    learns to ignore the channel."""
    alerts = _Alerts()

    announce_update(
        product="Neutron OS", current="1.4.1", available="1.11.1",
        notes="", recipient="@sam:netl", send=alerts,
    )

    assert "1.11.1" in alerts.calls[0]["dedup_key"]


def test_a_different_version_is_a_different_announcement():
    """Negative control: the key must track the version, not be a constant that
    silences every future release."""
    alerts = _Alerts()
    for version in ("1.11.1", "1.12.0"):
        announce_update(
            product="Neutron OS", current="1.4.1", available=version,
            notes="", recipient="@sam:netl", send=alerts,
        )

    keys = {c["dedup_key"] for c in alerts.calls}
    assert len(keys) == 2


def test_a_failed_announcement_does_not_raise():
    """Startup path: a notification backend that is down must not stop the CLI."""

    def _broken(**kwargs):
        raise RuntimeError("inbox unreachable")

    announce_update(
        product="Neutron OS", current="1.0.0", available="1.1.0",
        notes="", recipient="@sam:netl", send=_broken,
    )


# --- the question -----------------------------------------------------------


def test_enter_means_yes():
    """The whole ask: the common answer costs one keystroke."""
    assert resolve_choice("") == "upgrade"


def test_whitespace_is_still_enter():
    assert resolve_choice("   ") == "upgrade"


@pytest.mark.parametrize("answer", ["y", "Y", "yes", "YES", "1"])
def test_explicit_yes(answer):
    assert resolve_choice(answer) == "upgrade"


@pytest.mark.parametrize("answer", ["n", "no", "N", "2"])
def test_no_declines_this_time(answer):
    assert resolve_choice(answer) == "not_now"


@pytest.mark.parametrize("answer", ["s", "skip", "3"])
def test_skip_silences_this_version(answer):
    assert resolve_choice(answer) == "skip_version"


def test_an_unrecognised_answer_does_not_silently_upgrade():
    """Negative control, and the important one: a stray keystroke must not
    install software. Unknown input re-asks rather than taking the default."""
    assert resolve_choice("maybe") is None


def test_the_choices_are_offered_with_yes_first():
    assert UPGRADE_CHOICES[0][0] == "upgrade"
    assert "Enter" in UPGRADE_CHOICES[0][1]


# --- when to offer at all ---------------------------------------------------
#
# The prompt is the part that can annoy people, so the guards matter more than
# the feature. It must never appear in a pipe, a serving surface, a subagent or
# CI, and "skip this version" has to mean skip until there is a new one — a
# decline that is forgotten by the next launch is not a decline.

def test_it_does_not_offer_without_a_terminal():
    """A pipe, a serving surface, CI: nobody is there to answer."""
    assert should_offer_update(is_newer=True, interactive=False, disabled=False) is False


def test_it_does_not_offer_when_there_is_nothing_newer():
    assert should_offer_update(is_newer=False, interactive=True, disabled=False) is False


def test_it_does_not_offer_when_the_nudge_is_switched_off():
    """AXIOM_DISABLE_UPDATE_NUDGE is an existing contract; honour it."""
    assert should_offer_update(is_newer=True, interactive=True, disabled=True) is False


def test_it_offers_when_there_is_a_person_and_something_to_offer():
    """Negative control: the guards must not refuse everything."""
    assert should_offer_update(is_newer=True, interactive=True, disabled=False) is True


def test_a_skipped_version_is_remembered_across_launches(tmp_path):
    skip_version("1.11.1", state_dir=tmp_path)

    assert is_version_skipped("1.11.1", state_dir=tmp_path) is True


def test_skipping_one_version_does_not_silence_the_next(tmp_path):
    """Negative control, and the point of keying it: a decline is about this
    release, not about ever being told again."""
    skip_version("1.11.1", state_dir=tmp_path)

    assert is_version_skipped("1.12.0", state_dir=tmp_path) is False


def test_an_unreadable_skip_file_does_not_silence_the_offer(tmp_path):
    """Fail toward telling the user. A corrupt file must not mean 'skip
    everything forever', which is silence that looks like agreement."""
    (tmp_path / "skipped-versions.json").write_text("{ not json")

    assert is_version_skipped("1.11.1", state_dir=tmp_path) is False


# --- the interactive flow ---------------------------------------------------

def test_enter_alone_runs_the_upgrade():
    ran = []
    choice = offer_update_interactively(
        product="P", current="1.0.0", available="1.1.0", repo="",
        ask=lambda _: "", run_update=lambda: ran.append(True),
    )

    assert choice == "upgrade"
    assert ran == [True]


def test_declining_does_not_run_the_upgrade():
    """Negative control: the default must not fire for someone who said no."""
    ran = []
    choice = offer_update_interactively(
        product="P", current="1.0.0", available="1.1.0", repo="",
        ask=lambda _: "n", run_update=lambda: ran.append(True),
    )

    assert choice == "not_now"
    assert ran == []


def test_an_unrecognised_answer_re_asks_rather_than_upgrading():
    asked = []

    def _ask(prompt):
        asked.append(prompt)
        return "what" if len(asked) < 2 else "n"

    choice = offer_update_interactively(
        product="P", current="1.0.0", available="1.1.0", repo="",
        ask=_ask, run_update=lambda: None,
    )

    assert len(asked) == 2
    assert choice == "not_now"


def test_it_gives_up_asking_rather_than_looping_forever():
    """A terminal that returns junk forever must not trap the session."""
    choice = offer_update_interactively(
        product="P", current="1.0.0", available="1.1.0", repo="",
        ask=lambda _: "???", run_update=lambda: None,
    )

    assert choice == "not_now"


def test_ctrl_c_is_a_decline_not_a_crash():
    def _interrupt(_):
        raise KeyboardInterrupt

    assert offer_update_interactively(
        product="P", current="1.0.0", available="1.1.0", repo="", ask=_interrupt
    ) == "not_now"


def test_a_broken_upgrade_does_not_take_the_session_down():
    """This sits in front of a chat starting; it must fail soft."""

    def _boom():
        raise RuntimeError("pip exploded")

    assert offer_update_interactively(
        product="P", current="1.0.0", available="1.1.0", repo="",
        ask=lambda _: "", run_update=_boom,
    ) == "not_now"
