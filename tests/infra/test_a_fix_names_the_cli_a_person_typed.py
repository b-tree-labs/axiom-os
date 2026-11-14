# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A precondition's fix names the command line the person is actually using.

A refusal is shown to whoever called, through whatever distribution they
installed. A fix written as ``axi instrument video verify`` sends a person running a
consumer's CLI to a command that is not on their PATH. ``{cli}`` in a fix is
filled with the active branding's CLI name, the same way ``{name}`` is filled
from the call.
"""

from __future__ import annotations

import pytest

from axiom.infra import branding
from axiom.infra.skill_declarations import fill_fix


@pytest.fixture
def acme():
    branding.register(branding.BrandingConfig(cli_name="acme", product_name="Acme"))
    try:
        yield
    finally:
        branding.reset()


def test_cli_is_the_active_branding(acme):
    assert (
        fill_fix("run `{cli} instrument video verify {name}`", {"name": "cam-1"})
        == "run `acme instrument video verify cam-1`"
    )


def test_the_default_distribution_still_says_axi():
    branding.reset()
    assert fill_fix("`{cli} vault list`", {}) == "`axi vault list`"


def test_a_call_cannot_rename_the_cli(acme):
    """``cli`` is not a parameter a caller supplies; a call that passes one does not reword the fix."""
    assert fill_fix("`{cli} x`", {"cli": "rm -rf"}) == "`acme x`"


def test_unknown_names_are_still_left_as_written(acme):
    assert fill_fix("`{cli} x {nope}`", {}) == "`acme x {nope}`"
