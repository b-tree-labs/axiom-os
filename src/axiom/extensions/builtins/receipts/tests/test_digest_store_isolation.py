# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""#2134: a digest's send must not depend on what an earlier test on the worker left as the
process-wide recipient preference store. The first test leaks one (plain assignment, as the
real leakers did); the second must still get a clean world."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from axiom.extensions.builtins.notifications import preferences


class _LeftBehind:
    def get(self, recipient):
        raise OperationalError("select", {}, Exception("the engine was disposed"))


@pytest.fixture(scope="module")
def _leak():
    before = preferences._DEFAULT_STORE
    yield
    preferences._DEFAULT_STORE = before


def test_a_leaker_leaves_a_dead_store_behind(_leak):
    preferences._DEFAULT_STORE = _LeftBehind()


def test_the_next_test_gets_its_own_store_not_the_leaked_one():
    store = preferences.default_store()
    assert isinstance(store, preferences.InMemoryRecipientPreferenceStore)
    assert store.get("@ben") is None
