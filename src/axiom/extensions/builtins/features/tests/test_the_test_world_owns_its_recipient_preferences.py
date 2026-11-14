"""The features test world owns every store an alert touches (#1940).

``send()`` reads the process-wide recipient preference store. The world pinned
the inbox and the incident store and left that one alone, so whatever an earlier
test in the same worker left behind (a store on a disposed engine, or the
machine's database) answered here, and a rollout's notification was dropped
with ``OperationalError``."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from axiom.extensions.builtins.notifications import preferences


class _LeftBehind:
    """What another test's teardown can leave as the process default."""

    def get(self, recipient):
        raise OperationalError("select", {}, Exception("the engine was disposed"))


@pytest.fixture
def a_store_left_behind(monkeypatch):
    # Through monkeypatch, so it is undone in order with the world's own patch:
    # a hand-rolled restore here ran first and the world then put this back.
    monkeypatch.setattr(preferences, "_DEFAULT_STORE", _LeftBehind())


def test_the_world_replaces_a_preference_store_left_by_another_test(a_store_left_behind, modes):
    store = preferences.default_store()
    assert isinstance(store, preferences.InMemoryRecipientPreferenceStore)
    assert store.get("@anyone") is None


def test_a_rollout_alert_is_sent_whatever_another_test_left(a_store_left_behind, modes):
    from axiom.extensions.builtins.features import progressive

    progressive._send("The rollout moved on.", "why", intent="features.stage_advance")
    assert len(modes["inbox"].all()) == 1


def test_the_world_leaves_nothing_of_its_own_behind(a_store_left_behind, request):
    left = preferences._DEFAULT_STORE
    request.getfixturevalue("modes")
    assert preferences._DEFAULT_STORE is not left


def test_these_tests_leave_no_store_behind_either():
    assert not isinstance(preferences._DEFAULT_STORE, _LeftBehind)
