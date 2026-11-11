# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fourteen optional prompts in a row is not a choice.

The setup wizard walked every registered connection in turn and ran each one's
interactive flow. Fourteen of them, every one optional, with no way out of the
sequence. A colleague went through it during onboarding on 2026-10-01 and said
the connections were "either too early, irrelevant or he was unsure if he
needed it", and that the process was arduous and unfriendly.

Three separate problems, and a fix for one is not a fix for the others:

- **Too early.** Nothing in the list is needed to use the platform. The phase
  opened "You can skip any for now", which reads as *you will have to do this
  eventually*.
- **Irrelevant.** Fourteen products at equal weight, when somebody who came to
  read data from a site node needs none of them.
- **Unsure if he needed it.** Each prompt named a product and never said what
  it was for.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from axiom.setup.connection_groups import group_purpose, plan_groups


def _conn(name, category, kind="api", credential_type="api_key"):
    return SimpleNamespace(
        name=name,
        display_name=name.title(),
        category=category,
        kind=kind,
        credential_type=credential_type,
        required=False,
    )


FOURTEEN = [
    _conn("github", "code"),
    _conn("gitlab", "code"),
    _conn("postgresql", "data"),
    _conn("anthropic", "llm"),
    _conn("huggingface", "llm"),
    _conn("llm-provider", "llm", credential_type="none"),
    _conn("ollama", "llm", kind="cli", credential_type="none"),
    _conn("openai", "llm"),
    _conn("qwen-selfhosted", "llm"),
    _conn("box", "storage", credential_type="oauth2"),
    _conn("onedrive", "storage"),
    _conn("pack-server", "storage"),
    _conn("mmdc", "tools", kind="cli", credential_type="none"),
    _conn("pandoc", "tools", kind="cli", credential_type="none"),
]


def test_fourteen_connections_become_five_questions():
    """The count is the complaint. Five categories is four keystrokes to clear
    what you do not want, against fourteen interactive flows."""
    groups = plan_groups(FOURTEEN, is_done=lambda _c: False)
    assert len(groups) == 5
    assert sum(len(g.pending) for g in groups) == len(FOURTEEN), "a connection went missing"


def test_the_model_question_comes_first():
    """Somebody who stops reading after the first line should have seen the one
    that mattered."""
    groups = plan_groups(FOURTEEN, is_done=lambda _c: False)
    assert [g.category for g in groups] == ["llm", "code", "data", "storage", "tools"]


def test_every_group_says_what_it_is_for():
    """"Unsure if he needed it" is answerable, and the answer belongs beside the
    question rather than behind a command."""
    for group in plan_groups(FOURTEEN, is_done=lambda _c: False):
        assert group.purpose
        assert group.purpose != group.category, (
            f"{group.category}'s purpose just repeats its name, which is what the "
            f"prompt already said"
        )
        # Several words, because a one-word purpose is a synonym rather than an
        # explanation.
        assert len(group.purpose.split()) >= 3, group.purpose


def test_a_category_nobody_wrote_a_purpose_for_is_still_offered():
    """The listing is built from the registry, so silence in the purpose table
    must not drop a connection out of the wizard."""
    groups = plan_groups([_conn("something", "newkind")], is_done=lambda _c: False)
    assert len(groups) == 1
    assert groups[0].purpose
    assert "newkind" in group_purpose("newkind")


def test_what_is_already_set_up_is_reported_and_not_re_asked():
    done = {"pandoc", "ollama"}
    groups = plan_groups(FOURTEEN, is_done=lambda c: c.name in done)
    pending = {c.name for g in groups for c in g.pending}
    settled = {c.name for g in groups for c in g.done}
    assert settled == done
    assert not (pending & done)


def test_a_group_with_nothing_left_is_not_offered():
    groups = plan_groups(FOURTEEN, is_done=lambda c: c.category == "tools")
    tools = next(g for g in groups if g.category == "tools")
    assert tools.is_empty
    assert [c.name for c in tools.done] == ["mmdc", "pandoc"]
    assert not any(g.is_empty for g in groups if g.category != "tools")


def test_a_question_that_cannot_be_answered_is_not_an_answer():
    """Reading a keychain can fail. A connection whose state is unknown is
    offered, because offering something already configured wastes a keystroke
    and skipping something unconfigured loses it silently."""

    def _explodes(conn):
        raise OSError("keychain locked")

    groups = plan_groups(FOURTEEN, is_done=_explodes)
    assert sum(len(g.pending) for g in groups) == len(FOURTEEN)
    assert not any(g.done for g in groups)


# ---------------------------------------------------------------------------
# Through the wizard: what it says and what it does with each answer.
# ---------------------------------------------------------------------------


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    from axiom.setup import wizard as wizard_mod

    w = wizard_mod.SetupWizard.__new__(wizard_mod.SetupWizard)
    w.root = tmp_path
    w.state = SimpleNamespace(credentials_configured={})
    monkeypatch.setattr(wizard_mod, "save_state", lambda *_a, **_k: None)
    return w


def _answers(monkeypatch, *replies):
    from axiom.setup import renderer

    queue = list(replies)
    monkeypatch.setattr(
        renderer, "prompt_text", lambda *_a, **_k: queue.pop(0) if queue else ""
    )
    return queue


def test_enter_declines_everything_and_sets_nothing_up(wizard, monkeypatch, capsys):
    """The way out that did not exist. One keystroke, and it must not configure
    anything."""
    _answers(monkeypatch, "")
    walked = []
    groups = [g for g in plan_groups(FOURTEEN, is_done=lambda _c: False) if not g.is_empty]
    wizard._offer_connection_groups(groups, lambda n, _r: walked.append(n), None, "axi")
    assert walked == []
    out = capsys.readouterr().out
    assert "connect <name>" in out, "nothing says how to come back to these"


def test_naming_one_group_sets_up_only_that_group(wizard, monkeypatch):
    _answers(monkeypatch, "code", "")
    walked = []
    groups = [g for g in plan_groups(FOURTEEN, is_done=lambda _c: False) if not g.is_empty]
    wizard._offer_connection_groups(groups, lambda n, _r: walked.append(n), None, "axi")
    assert walked == ["github", "gitlab"]


def test_all_sets_up_everything_and_stops_asking(wizard, monkeypatch):
    _answers(monkeypatch, "all")
    walked = []
    groups = [g for g in plan_groups(FOURTEEN, is_done=lambda _c: False) if not g.is_empty]
    wizard._offer_connection_groups(groups, lambda n, _r: walked.append(n), None, "axi")
    assert len(walked) == len(FOURTEEN)


def test_a_group_name_nobody_recognises_lists_the_real_ones(wizard, monkeypatch, capsys):
    _answers(monkeypatch, "slack", "")
    walked = []
    groups = [g for g in plan_groups(FOURTEEN, is_done=lambda _c: False) if not g.is_empty]
    wizard._offer_connection_groups(groups, lambda n, _r: walked.append(n), None, "axi")
    out = capsys.readouterr().out
    assert "slack" in out
    assert "llm" in out and "code" in out
    assert walked == [], "a typo must not set anything up"


def test_the_offer_returns_until_they_decline(wizard, monkeypatch):
    """Somebody who wants two groups should not have to re-run the wizard."""
    _answers(monkeypatch, "code", "data", "")
    walked = []
    groups = [g for g in plan_groups(FOURTEEN, is_done=lambda _c: False) if not g.is_empty]
    wizard._offer_connection_groups(groups, lambda n, _r: walked.append(n), None, "axi")
    assert walked == ["github", "gitlab", "postgresql"]


def test_taking_every_group_one_at_a_time_ends_without_another_question(wizard, monkeypatch):
    """The loop must terminate when nothing is left, rather than asking about an
    empty list."""
    asked = []

    from axiom.setup import renderer

    def _prompt(*_a, **_k):
        asked.append(1)
        return "all"

    monkeypatch.setattr(renderer, "prompt_text", _prompt)
    groups = [g for g in plan_groups(FOURTEEN, is_done=lambda _c: False) if not g.is_empty]
    wizard._offer_connection_groups(groups, lambda _n, _r: None, None, "axi")
    assert len(asked) == 1


# ---------------------------------------------------------------------------
# The last line of setup is the one people remember.
# ---------------------------------------------------------------------------


def test_a_machine_with_nothing_to_index_is_not_told_something_failed(tmp_path):
    """A fresh machine has no `docs/` and no reason to. Reporting that red put a
    failure on the last line of onboarding for every colleague who ran it."""
    from axiom.setup.tester import ChannelTester

    result = ChannelTester(tmp_path).test_local_docs()
    assert result.passed is True
    assert result.skipped is True
    assert "nothing to index" in result.message


def test_an_empty_docs_directory_is_the_same_kind_of_absence(tmp_path):
    from axiom.setup.tester import ChannelTester

    (tmp_path / "docs").mkdir()
    result = ChannelTester(tmp_path).test_local_docs()
    assert result.passed is True and result.skipped is True


def test_documents_that_exist_are_still_counted(tmp_path):
    """The negative control. A check that can only skip proves nothing."""
    from axiom.setup.tester import ChannelTester

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "one.md").write_text("# one", encoding="utf-8")
    (docs / "two.md").write_text("# two", encoding="utf-8")
    result = ChannelTester(tmp_path).test_local_docs()
    assert result.passed is True
    assert result.skipped is False
    assert "2 documents" in result.message


def test_the_skip_says_what_would_change_it(tmp_path):
    """A skip that does not say how to stop skipping is a dead end."""
    from axiom.setup.tester import ChannelTester

    said = ChannelTester(tmp_path).test_local_docs().message
    assert "docs/" in said
    assert "markdown" in said.lower()
