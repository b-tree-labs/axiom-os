# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Doctor says when the node's alerts reach no person (#1597).

Platform alerts go to ``@operator``. On a node where nobody holds that role and
``@operator`` has no recipient profile, every alert lands in an inbox nobody
reads, and every receipt said it succeeded. Doctor names that as a failure, with
the fix.
"""

from __future__ import annotations

from axiom.cli.doctor import CheckStatus, check_operator_route, default_checks
from axiom.extensions.builtins.notifications.preferences import (
    InMemoryRecipientPreferenceStore,
    RecipientChannel,
    RecipientProfile,
)


class _Settings:
    def __init__(self, holder=""):
        self.table = {"notifications.roles.operator": holder}

    def get(self, key, default=None):
        return self.table.get(key, default)


def _profiles(*handles):
    store = InMemoryRecipientPreferenceStore()
    for handle in handles:
        store.put(RecipientProfile(recipient=handle, channels=(RecipientChannel("inbox", handle),)))
    return store


def test_no_holder_and_no_profile_is_a_failure_with_the_fix():
    result = check_operator_route(settings=_Settings(), profiles=_profiles())
    assert result.status is CheckStatus.ERROR
    assert "no human route" in result.summary
    assert "@operator" in result.summary and "no profile" in result.summary
    assert "notifications.roles.operator" in (result.fix_hint or "")


def test_a_holder_with_a_profile_is_ok():
    result = check_operator_route(settings=_Settings("@person"), profiles=_profiles("@person"))
    assert result.status is CheckStatus.OK


def test_a_holder_without_a_profile_reaches_only_their_inbox():
    result = check_operator_route(settings=_Settings("@person"), profiles=_profiles())
    assert result.status is CheckStatus.WARNING
    assert "@person" in result.summary


def test_an_operator_profile_is_a_route():
    result = check_operator_route(settings=_Settings(), profiles=_profiles("@operator"))
    assert result.status is CheckStatus.OK


def test_the_check_is_part_of_doctor():
    assert any(c.fn is check_operator_route for c in default_checks())
