# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Storing a credential without an expiry is where the blind spot is created.

Every downstream guard — the expiry audit, the rotation pass, the heartbeat
finding — reads `expires_at`. A credential stored without one is invisible to
all of them simultaneously, and stays invisible forever: there is no later event
that supplies the missing field.

That is exactly how a live token went dark. Six credentials on this install had
no expiry recorded, and the first symptom was an HTTP 401.

The write is still allowed — some credentials genuinely never expire, and
blocking the store would just push people to work around it. But it must not be
silent, because silence is what made this cost six months.
"""

from __future__ import annotations

import logging

import pytest

from axiom.extensions.builtins.secrets.skills import set as set_skill
from axiom.infra.skills import SkillContext, default_registry


@pytest.fixture
def ctx(tmp_path):
    return SkillContext(registry=default_registry(), state_dir=tmp_path,
                        logger=logging.getLogger("t"))


def test_storing_without_an_expiry_warns(ctx):
    result = set_skill.run({"name": "no-expiry-cred", "value": "v"}, ctx)
    assert result.ok, "the write must still succeed"
    joined = " ".join(result.actions_taken)
    assert "no expiry" in joined.lower()
    assert "audit" in joined.lower(), "the warning must say what is lost"


def test_storing_with_an_expiry_does_not_nag(ctx):
    """Negative control. A warning that fires every time is one people filter
    out, and then it is not a warning."""
    result = set_skill.run(
        {"name": "dated-cred", "value": "v", "expires_at": "2027-01-01"}, ctx)
    assert result.ok
    assert "no expiry" not in " ".join(result.actions_taken).lower()


def test_the_warning_names_the_command_that_fixes_it(ctx):
    result = set_skill.run({"name": "no-expiry-cred", "value": "v"}, ctx)
    assert "reconcile" in " ".join(result.actions_taken).lower()
