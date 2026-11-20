# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Seeing the prompt, and where each piece of it came from.

Diagnosing today's two prompt defects meant dumping the composed prompt from a
Python one-liner and reading 8,000 characters to find one stale sentence and
one extension overreaching. The composer has tracked `source` per fragment the
whole time; there was simply no way to look at it.

`render_provenance` is that view: which layer, which fragment, who contributed
it, how many tokens it costs. An override shows as `local-override`, so an
experiment never looks like shipped behaviour.
"""

from __future__ import annotations

from axiom.infra.prompt_cli import render_provenance


class _Contribution:
    def __init__(self, layer, name, source, tokens):
        self.layer = layer
        self.name = name
        self.source = source
        self.tokens = tokens
        self.content = "x" * tokens
        self.required = True


def _contributions():
    return [
        _Contribution("identity", "persona", "axiom", 120),
        _Contribution("capabilities", "registered_tools", "registry", 400),
        _Contribution("capabilities", "model_corral_role", "example_ext.catalog", 90),
    ]


def test_it_names_every_fragment_and_its_source():
    out = render_provenance(_contributions())

    assert "registered_tools" in out
    assert "example_ext.catalog" in out


def test_it_groups_by_layer():
    out = render_provenance(_contributions())

    assert "identity" in out
    assert "capabilities" in out


def test_it_reports_the_token_cost():
    """The question behind an override is usually "is this worth its tokens"."""
    out = render_provenance(_contributions())

    assert "400" in out


def test_an_override_is_visible_as_an_override():
    """An experiment must never be mistaken for shipped behaviour."""
    contributions = _contributions()
    contributions[1].source = "local-override"

    out = render_provenance(contributions)

    assert "local-override" in out


def test_nothing_composed_says_so_rather_than_printing_an_empty_table():
    out = render_provenance([])

    assert out.strip()
    assert "no" in out.lower()
