# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Every interactive command offers a newer release first, once a day, then runs on it.

The offer existed (``offer_update_interactively``) and only ``chat`` used it;
every other command printed one line after the fact. On 2026-10-06 a new user
spent an afternoon on an install six releases behind. Now the first command a
person types on a day a newer release is out asks first, Enter takes it, and
the command they typed then runs on the new code instead of the old code
already loaded. Nobody is asked when nobody is there, ``update`` and help are
never interrupted, a checkout is told to pull, and declining is not asked again
until tomorrow or a newer release.
"""

from __future__ import annotations

from datetime import date

import pytest

from axiom.extensions.builtins.update import offer


class _Info:
    def __init__(self, current="1.0.0", available="1.1.0", is_newer=True):
        self.current, self.available, self.is_newer = current, available, is_newer


@pytest.fixture
def env(tmp_path, monkeypatch):
    calls = {"offered": 0, "resumed": None, "answer": "y"}

    def fake_offer(**kw):
        calls["offered"] += 1
        choice = offer.resolve_choice(calls["answer"])
        if choice == "upgrade" and kw.get("run_update"):
            kw["run_update"]()
        return choice

    monkeypatch.setattr(offer, "offer_update_interactively", fake_offer)
    monkeypatch.setattr(offer, "_update_then_resume", lambda argv: calls.__setitem__("resumed", argv))
    monkeypatch.delenv("AXIOM_DISABLE_UPDATE_NUDGE", raising=False)
    calls["state"] = tmp_path
    return calls


def _entry(env, *, argv=("bench", "list"), interactive=True, editable=False, info=None, today=date(2026, 10, 6)):
    return offer.offer_at_entry(
        list(argv),
        interactive=interactive,
        info=info or _Info(),
        editable=editable,
        state_dir=env["state"],
        today=today,
    )


def test_the_first_command_asks_and_an_upgrade_resumes_that_command(env):
    assert _entry(env) == "upgrade"
    assert env["offered"] == 1 and env["resumed"] == ["bench", "list"]


def test_declining_is_not_asked_again_today(env):
    env["answer"] = "n"
    assert _entry(env) == "not_now"
    assert _entry(env) == "asked_today" and env["offered"] == 1


def test_it_asks_again_tomorrow(env):
    env["answer"] = "n"
    _entry(env)
    assert _entry(env, today=date(2026, 10, 7)) == "not_now" and env["offered"] == 2


def test_a_newer_release_than_the_one_declined_asks_again(env):
    env["answer"] = "n"
    _entry(env)
    assert _entry(env, info=_Info(available="1.2.0")) == "not_now" and env["offered"] == 2


@pytest.mark.parametrize("argv", [("update",), ("update", "--check"), ("--help",), ("help",), ("--version",), ()])
def test_update_and_help_are_never_interrupted(env, argv):
    assert _entry(env, argv=argv) == "notice" and env["offered"] == 0


def test_nobody_is_asked_when_nobody_is_there(env):
    assert _entry(env, interactive=False) == "notice" and env["offered"] == 0


def test_a_checkout_is_told_to_pull(env):
    assert _entry(env, editable=True) == "notice" and env["offered"] == 0


def test_nothing_when_current(env):
    assert _entry(env, info=_Info(is_newer=False)) == "none" and env["offered"] == 0


def test_the_switch_still_turns_it_off(env, monkeypatch):
    monkeypatch.setenv("AXIOM_DISABLE_UPDATE_NUDGE", "1")
    assert _entry(env) == "notice" and env["offered"] == 0
