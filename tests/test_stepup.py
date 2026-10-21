# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""ENF-2: step-up — hybrid (interactive elevates inline, headless defers) +
session-persistent elevation."""

from __future__ import annotations

import pytest

from axiom.infra.principal import PrincipalContext
from axiom.infra.stepup import StepUpRequired, clear_step_up, step_up
from axiom.vega.identity.custody import InMemoryCustody


def setup_function():
    clear_step_up()


def test_already_meeting_the_floor_is_a_noop():
    p = PrincipalContext("@ben:example-org", "sso", assured=True)
    assert step_up("attested", current=p) is p          # already higher


def test_attested_elevates_inline_when_interactive():
    open_p = PrincipalContext("@ben:local", "open")
    elevated = step_up("attested", current=open_p, interactive=True, custody=InMemoryCustody())
    assert elevated.posture == "attested" and elevated.assured is True


def test_attested_defers_when_headless():
    open_p = PrincipalContext("@ben:local", "open")
    with pytest.raises(StepUpRequired) as exc:
        step_up("attested", current=open_p, interactive=False)
    assert "axi identity init" in exc.value.remediation


def test_sso_defers_with_login_remediation():
    open_p = PrincipalContext("@ben:local", "open")
    with pytest.raises(StepUpRequired) as exc:
        step_up("sso", current=open_p, interactive=True)
    assert "axi auth login" in exc.value.remediation


def test_elevation_is_session_persistent():
    open_p = PrincipalContext("@ben:local", "open")
    first = step_up("attested", current=open_p, interactive=True, custody=InMemoryCustody())
    # Second call (even headless now) reuses the cached elevation — no re-prompt.
    second = step_up("attested", current=open_p, interactive=False)
    assert second is first
