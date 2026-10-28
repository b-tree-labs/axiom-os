# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The node knows itself: fast summary, live profile, first-person
prompt, and the chat-tool binding — the class where the agent waffles
about its own configuration ("if such a function existed…") must be
impossible."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.status import selfknowledge as sk


@pytest.fixture(autouse=True)
def _fresh():
    sk.reset_summary_cache()
    yield
    sk.reset_summary_cache()


def test_summary_is_cheap_by_construction_and_cached(monkeypatch):
    calls = {"n": 0}
    real = sk._extensions

    def counting():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(sk, "_extensions", counting)
    first = sk.node_summary(now=100.0)
    again = sk.node_summary(now=102.0)  # within TTL: served from cache
    assert first is again
    assert calls["n"] == 1
    later = sk.node_summary(now=110.0)  # past TTL: recomposed (fresh)
    assert calls["n"] == 2
    assert later["version"]["distribution"]
    assert later["version"]["source_path"].endswith("__init__.py")
    assert "status" in later["extensions"] and "chat" in later["extensions"]


def test_federation_answers_honestly_when_unconfigured(monkeypatch):
    monkeypatch.setattr(
        sk, "_federation", lambda: {"configured": False, "peers": [], "identity": None}
    )
    frag = sk.self_prompt_fragment()
    assert "I am not federated with any peer or site right now" in frag["content"]


def test_prompt_fragment_speaks_first_person_with_fresh_facts(monkeypatch):
    monkeypatch.setattr(
        sk,
        "_federation",
        lambda: {
            "configured": True,
            "peers": [{"name": "peer-a:owner", "node_id": "0e37", "url": None}],
            "identity": None,
        },
    )
    monkeypatch.setenv("AXIOM_SITE", "local")
    sk.reset_summary_cache()
    frag = sk.self_prompt_fragment()
    assert frag["layer"] == "capabilities"
    assert "I am federated with 1 peer(s): peer-a:owner." in frag["content"]
    # The negative case must be answerable too, not inferred.
    assert "not federated with it" in frag["content"]
    assert "My site scope is 'local'." in frag["content"]
    assert "FIRST PERSON" in frag["content"]
    assert "node_describe" in frag["content"]  # the drill-down is named
    # The voice rule itself is stated (never third-person self-reference).
    assert "never refer to myself as 'the system'" in frag["content"]


def test_profile_sections_are_live_and_guarded():
    profile = sk.node_profile()
    sections = profile["sections"]
    # Live registry derivations:
    assert "routes" in sections and "chat_tools" in sections
    routes = sections["routes"]
    if isinstance(routes, list):  # http importable here
        prefixes = {r["prefix"] for r in routes}
        assert "/gate" in prefixes
    # The self-knowledge tool must itself be among the chat tools —
    # the always-fresh binding this whole feature rides.
    tools = sections["chat_tools"]
    assert isinstance(tools, list)
    assert "node_describe" in tools
    # RAG unconfigured here answers honestly, never raises.
    assert sections["rag"].get("configured") in (False, True) or "unavailable" in sections["rag"]


def test_profile_section_narrowing_and_unknown():
    one = sk.node_profile("node")
    assert set(one["sections"]) == {"node"}
    from axiom.extensions.builtins.status.skills.describe import run

    bad = run({"section": "nope"})
    assert not bad.ok and "unknown section" in bad.errors[0]


def test_db_target_never_leaks_credentials(monkeypatch):
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://user:secretpw@dbhost:5432/axiom")
    got = sk.node_profile("db")["sections"]["db"]["target"]
    assert "secretpw" not in got and "user" not in got
    assert got == "dbhost:5432/axiom"
