# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A guide that says "or ask chat" is tested: its prompts are read, covered, routed and (on demand) asked."""

from __future__ import annotations

import textwrap

import pytest

from axiom.evals import documented_prompts as dp

GUIDE = textwrap.dedent(
    """\
    # Part 1

    ## Step 3: Verify the binding

    ```sh
    neut site whoami
    ```

    > **Or ask `neut chat`:** "am I connected to the site?" or “which site am I bound to?”

    ## Step 4: Read data

    > **Or ask neut chat:** "show me example-loop fluid temp yesterday"

    A quoted "sentence" in ordinary prose is not a documented prompt.
    > Someone else's quote: "not a prompt"
    """
)

CATALOG = textwrap.dedent(
    """\
    [[prompt]]
    text = "am I connected to the site?"
    source = "setup"
    route = "binding__status"

    [[prompt]]
    text = "which site am I bound to?"
    source = "setup"
    route = ["binding__status"]

    [[prompt]]
    text = "show me example-loop fluid temp yesterday"
    source = "data"
    route = ["data__show"]
    """
)


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(CATALOG, encoding="utf-8")
    return dp.load_catalog(path)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))

    class _S:
        def get(self, key, default=None):
            return default

    monkeypatch.setattr("axiom.extensions.builtins.settings.store.SettingsStore", lambda: _S())
    monkeypatch.delenv("AXIOM_SAMPLE_DATA", raising=False)


# ---- reading --------------------------------------------------------------------------------


def test_prompts_are_read_with_their_step_and_line():
    found = dp.extract_prompts(GUIDE)
    assert [p.prompt for p in found] == [
        "am I connected to the site?",
        "which site am I bound to?",
        "show me example-loop fluid temp yesterday",
    ]
    assert found[0].step == "Step 3: Verify the binding"
    assert found[2].step == "Step 4: Read data"
    assert all(p.line > 0 for p in found)


def test_only_marked_lines_count():
    assert dp.extract_prompts('Say "hello" to chat.\n> a quote: "hi"\n') == []


def test_a_catalog_row_must_name_a_known_source(tmp_path):
    bad = tmp_path / "b.toml"
    bad.write_text('[[prompt]]\ntext = "x"\nsource = "vibes"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="source must be one of"):
        dp.load_catalog(bad)


def test_a_data_or_setup_prompt_must_name_its_tool(tmp_path):
    bad = tmp_path / "b.toml"
    bad.write_text('[[prompt]]\ntext = "x"\nsource = "data"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="must name the tool"):
        dp.load_catalog(bad)


# ---- coverage fails both ways ---------------------------------------------------------------


def test_coverage_is_clean_when_guide_and_catalog_agree(catalog):
    assert dp.coverage(dp.extract_prompts(GUIDE), catalog).ok


def test_a_documented_prompt_without_a_catalog_row_fails(catalog):
    guide = GUIDE + '\n> **Or ask neut chat:** "do a new thing"\n'
    cov = dp.coverage(dp.extract_prompts(guide), catalog)
    assert cov.undocumented == ["do a new thing"] and not cov.ok


def test_a_catalog_row_no_guide_documents_fails(catalog):
    cov = dp.coverage(
        dp.extract_prompts(GUIDE.replace("which site am I bound to?", "which site?")), catalog
    )
    assert "which site am I bound to?" in cov.stale and "which site?" in cov.undocumented


def test_matching_ignores_case_and_spacing(catalog):
    guide = GUIDE.replace("am I connected to the site?", "Am  I connected   to the site?")
    assert dp.coverage(dp.extract_prompts(guide), catalog).ok


# ---- routing, no model ----------------------------------------------------------------------


@pytest.fixture
def tools():
    return dp.offered_tools(bound=True)


def test_the_catalog_prompts_route(catalog, tools):
    for entry in catalog:
        verdict = dp.check_routing(entry, tools)
        assert verdict.ok, (entry.prompt, verdict.problems)


def test_a_prompt_that_cannot_reach_its_tool_is_reported(tools):
    entry = dp.CatalogEntry("am I connected to the site?", "setup", ("no_such__tool",))
    verdict = dp.check_routing(entry, tools)
    assert not verdict.ok and "offered tool" in verdict.problems[0]


def test_a_catalog_that_misclassifies_the_prompt_is_reported(tools):
    entry = dp.CatalogEntry("am I connected to the site?", "data", ("data__show",))
    assert "not a data question" in dp.check_routing(entry, tools).problems[0]


def test_an_update_request_is_not_a_data_question():
    """ "newest version" is a release, not a reading; "newest reading" still is."""
    from axiom.extensions.builtins.chat.bound_tools import is_data_question

    assert not is_data_question("update neut to the newest version")
    assert not is_data_question("is there a newer version of neut?")
    assert is_data_question("what is the newest reading?")


@pytest.mark.parametrize(
    "prompt",
    [
        "which sensors were hottest on oct 1",
        "what channels does my site record?",
        "which quantities can I ask about?",
        "show me example-loop fluid temp yesterday",
        "wat is lowest temp last week",  # broken English still reads as data
    ],
)
def test_these_read_as_data_questions(prompt):
    from axiom.extensions.builtins.chat.bound_tools import is_data_question

    assert is_data_question(prompt)


@pytest.mark.parametrize(
    "prompt",
    ["am I connected to the site?", "check my setup", "how is a pulse done on the reactor?"],
)
def test_these_do_not(prompt):
    from axiom.extensions.builtins.chat.bound_tools import is_data_question

    assert not is_data_question(prompt)


# ---- the on-demand evaluation ---------------------------------------------------------------

GOOD_FOOTER = "data: example-site via node.example.org · documents: node.example.org · model: m (REMOTE node.example.org)"


def _entries(catalog):
    return list(catalog)


def test_evaluate_passes_when_the_right_tool_ran_and_the_footer_names_sources(catalog):
    def ask(prompt):
        data = prompt.startswith("show")
        return {
            "answer": "ok",
            "answered": True,
            "verbs_called": ["data.show"] if data else ["binding.status"],
            "footer": GOOD_FOOTER,
            "measured": True if data else None,
        }

    report = dp.evaluate(_entries(catalog), ask)
    assert report.total == 3 and report.passed, report.scores


def test_evaluate_fails_a_data_answer_that_called_no_data_tool(catalog):
    def ask(prompt):
        return {
            "answer": "21 degrees",
            "answered": True,
            "verbs_called": [],
            "footer": GOOD_FOOTER + " · not a measurement: no data tool was called",
            "measured": False,
        }

    report = dp.evaluate(_entries(catalog), ask)
    assert not report.passed
    assert report.scores["tool"] == 0.0


def test_evaluate_fails_a_footer_that_guesses(catalog):
    def ask(prompt):
        return {
            "answer": "ok",
            "answered": True,
            "verbs_called": ["binding.status", "data.show"],
            "footer": "REMOTE? node.example.org · RAG: none",
        }

    assert dp.evaluate(_entries(catalog), ask).scores["source"] == 0.0


def test_evaluate_reports_a_crash_as_a_failure_not_an_exception(catalog):
    def ask(prompt):
        raise RuntimeError("node down")

    report = dp.evaluate(_entries(catalog), ask)
    assert not report.passed and report.scores["answered"] == 0.0


def test_a_documents_prompt_must_not_call_a_data_tool_and_must_cite_a_place():
    entry = dp.CatalogEntry("how is a pulse done?", "documents")
    answered = {"answer": "...", "answered": True, "verbs_called": [], "footer": GOOD_FOOTER}
    assert dp.score_tool(answered, entry) == 1.0 and dp.score_source(answered, entry) == 1.0
    wrong = dict(answered, verbs_called=["data.show"])
    assert dp.score_tool(wrong, entry) == 0.0
    nowhere = dict(
        answered,
        footer=GOOD_FOOTER.replace("documents: node.example.org", "documents: none configured (x)"),
    )
    assert dp.score_source(nowhere, entry) == 0.0


# ---- confirmation, and what counts as the right tool on a bound node ---------------------------


def test_a_prompt_that_confirms_must_reach_a_write_tool(tools):
    ok = dp.CatalogEntry(
        "update neut to the newest version", "setup", ("update__apply",), confirms=True
    )
    assert dp.check_routing(ok, tools).ok
    wrong = dp.CatalogEntry(
        "is there a newer version of neut?", "setup", ("update__check",), confirms=True
    )
    assert "would not ask" in dp.check_routing(wrong, tools).problems[0]


def test_a_data_answer_counts_when_any_data_read_ran_on_the_node():
    entry = dp.CatalogEntry("show me fluid temp", "data", ("data__show",))
    node_equivalent = {"verbs_called": ["node:reactor_status_series"], "measured": True}
    assert dp.score_tool(node_equivalent, entry) == 1.0
    assert dp.score_tool({"verbs_called": [], "measured": False}, entry) == 0.0
    assert dp.score_tool({"verbs_called": []}, entry) == 0.0


def test_a_reply_from_the_installs_own_facts_credits_the_verb_that_holds_them():
    entry = dp.CatalogEntry("am I connected to the site?", "setup", ("binding__status",))
    assert dp.score_tool({"verbs_called": ["binding.status"]}, entry) == 1.0
