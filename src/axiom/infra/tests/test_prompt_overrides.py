# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Local overrides, so someone can try a different prompt without a release.

Two things went wrong today that this exists to make cheap. The base preamble
described a product that no longer existed, and an extension asserted a closed
tool list that made every other extension invisible. Both were one sentence of
text, both shipped, and neither could be tried differently without editing
installed source.

An override is local and per-fragment: it replaces one named contribution and
leaves the rest of the cascade alone, so an experiment does not mean forking
the whole prompt. Setting one to an empty string silences that fragment, which
is how you test whether a piece is carrying its weight.

Precedence is the point. A local override is the most specific thing there is,
so it wins over the platform default, a shipped example, and anything an
extension contributed — including an extension that thinks it speaks for the
whole system.
"""

from __future__ import annotations

from axiom.infra.prompt_overrides import (
    clear_override,
    list_overrides,
    load_overrides,
    set_override,
)


def test_an_override_round_trips(tmp_path):
    set_override("registered_tools", "just these tools", state_dir=tmp_path)

    assert load_overrides(state_dir=tmp_path)["registered_tools"] == "just these tools"


def test_nothing_set_is_an_empty_mapping(tmp_path):
    """Negative control: no overrides must not mean 'everything overridden'."""
    assert load_overrides(state_dir=tmp_path) == {}


def test_an_empty_override_silences_a_fragment(tmp_path):
    """Distinct from absent: "" means remove this, not "no override set"."""
    set_override("model_corral_next_steps", "", state_dir=tmp_path)

    overrides = load_overrides(state_dir=tmp_path)

    assert "model_corral_next_steps" in overrides
    assert overrides["model_corral_next_steps"] == ""


def test_clearing_restores_the_cascade(tmp_path):
    set_override("registered_tools", "x", state_dir=tmp_path)

    clear_override("registered_tools", state_dir=tmp_path)

    assert "registered_tools" not in load_overrides(state_dir=tmp_path)


def test_clearing_one_leaves_the_others(tmp_path):
    set_override("a", "1", state_dir=tmp_path)
    set_override("b", "2", state_dir=tmp_path)

    clear_override("a", state_dir=tmp_path)

    assert list(load_overrides(state_dir=tmp_path)) == ["b"]


def test_a_corrupt_override_file_does_not_break_the_prompt(tmp_path):
    """Fail toward the shipped prompt. A prompt that will not build is a chat
    that will not start, which is worse than an experiment being ignored."""
    (tmp_path / "prompt-overrides.json").write_text("{ not json")

    assert load_overrides(state_dir=tmp_path) == {}


def test_listing_reports_what_is_set(tmp_path):
    set_override("one", "x", state_dir=tmp_path)

    assert list_overrides(state_dir=tmp_path) == {"one": "x"}


# --- applying them ----------------------------------------------------------


def test_an_override_replaces_the_contribution_it_names():
    from axiom.infra.prompt_composer import PromptComposer
    from axiom.infra.prompt_overrides import apply_overrides

    composer = PromptComposer()
    composer.add("capabilities", name="registered_tools", content="original", source="registry")

    apply_overrides(composer, {"registered_tools": "mine"})

    contributions = {c.name: c.content for c in composer.debug()}
    assert contributions["registered_tools"] == "mine"


def test_an_override_for_an_absent_fragment_is_ignored():
    """Negative control: a typo must not inject an unnamed block into the
    prompt, which would be an override that silently becomes a new voice."""
    from axiom.infra.prompt_composer import PromptComposer
    from axiom.infra.prompt_overrides import apply_overrides

    composer = PromptComposer()
    composer.add("capabilities", name="real", content="original", source="registry")

    apply_overrides(composer, {"typo": "mine"})

    names = {c.name for c in composer.debug()}
    assert names == {"real"}


def test_an_empty_override_removes_the_fragment():
    from axiom.infra.prompt_composer import PromptComposer
    from axiom.infra.prompt_overrides import apply_overrides

    composer = PromptComposer()
    composer.add("capabilities", name="noisy", content="lots of words", source="ext")

    apply_overrides(composer, {"noisy": ""})

    assert [c.name for c in composer.debug()] == []


def test_applying_nothing_changes_nothing():
    from axiom.infra.prompt_composer import PromptComposer
    from axiom.infra.prompt_overrides import apply_overrides

    composer = PromptComposer()
    composer.add("capabilities", name="kept", content="original", source="registry")

    apply_overrides(composer, {})

    assert [c.content for c in composer.debug()] == ["original"]
