# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``/memory``: the chat reads and writes the user's memory through the one serving door (ADR-168, ADR-087 D7).

Everything runs on the real composition, the real recall index and the real gate over the test seam; the
only stand-ins are the agent shell (a session and a provider list) because a full ChatAgent needs a model.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.llm.gateway import LLMProvider
from axiom.memory.fragment import SourceOrigin, create_fragment
from axiom.vega.federation.policy import (
    ClassificationStamp,
    ExportControl,
    VisibilityHorizon,
)

ME = "@ada:lab"
LOCAL = LLMProvider(name="local-llm", endpoint="http://localhost:8000/v1", model="m1")
LOCAL_EC = LLMProvider(
    name="local-ec", endpoint="http://localhost:8001/v1", model="m4", routing_tier="export_controlled"
)
CLOUD = LLMProvider(name="cloud-llm", endpoint="https://api.openai.com/v1", model="m2")
CLOUD_EC = LLMProvider(
    name="ec-llm", endpoint="https://api.openai.com/v1", model="m3", routing_tier="export_controlled"
)


@pytest.fixture(autouse=True)
def _commands_loaded():
    from axiom.extensions.builtins.chat import commands_ext

    commands_ext.load_all()


def _agent(provider=LOCAL, *, principal=ME):
    session = SimpleNamespace(principal_id=principal, session_id="sess-1", context={}, messages=[])
    gateway = SimpleNamespace(last_selected=provider, providers=[provider], _provider_override=None)
    return SimpleNamespace(session=session, gateway=gateway)


def _run(agent, line):
    ctx = CommandContext(agent=agent, store=None)
    out = REGISTRY.dispatch(line, ctx)
    assert isinstance(out, str), out
    return out


def _composition():
    from axiom.extensions.builtins.chat.memory_wiring import _build_user_composition

    return _build_user_composition()


def _write(comp, text, *, ctype="semantic", origin=None, principal=ME):
    return comp.write(
        content={"summary": text},
        cognitive_type=ctype,
        principal_id=principal,
        agents={"seed"},
        resources=set(),
        accountable_human_id=principal,
        origin=origin,
    )


def _write_classified(comp, text, stamp):
    frag = create_fragment({"summary": text}, "semantic", ME, {"seed"}, set())
    frag = dataclasses.replace(
        frag,
        classification=stamp,
        visibility=VisibilityHorizon.PEERS_DECLARED,
        provenance=dataclasses.replace(frag.provenance, accountable_human_id=ME),
    )
    comp.artifact_registry.register(kind="fragment", name=frag.id, data=frag.to_dict())
    comp.recall_index.index_fragment(frag)
    return frag


def test_the_chat_composition_can_recall():
    # The finding this command starts from: chat's composition had no recall index.
    assert _composition().recall_index is not None


def test_search_serves_a_matching_fragment_through_the_gate():
    comp = _composition()
    frag = _write(comp, "the quarterly budget review is on the third floor")
    out = _run(_agent(), "/memory search quarterly budget")
    assert frag.id in out
    assert "quarterly budget review" in out
    assert "1 served" in out


def test_search_without_a_query_prints_usage():
    assert "/memory search <query>" in _run(_agent(), "/memory search")


def test_search_shows_import_provenance_and_denies_other_accounts_without_their_content():
    comp = _composition()
    mine = SourceOrigin("codex", ME, "ref-1", datetime.now(UTC).isoformat())
    theirs = SourceOrigin("claude-code", "someone-else", "ref-2", datetime.now(UTC).isoformat())
    served = _write(comp, "migration playbook for the cluster", origin=mine)
    denied = _write(comp, "migration playbook secret sauce for the other account", origin=theirs)
    out = _run(_agent(), "/memory search migration playbook")
    assert served.id in out
    assert "codex" in out  # origin harness
    assert denied.id not in out
    assert "secret sauce" not in out
    assert "cross_account" in out and "1 withheld" in out


def test_vault_and_secret_shaped_fragments_are_denied_with_reasons_never_content():
    comp = _composition()
    _write(comp, "deploy notes vault entry about deploy", ctype="vault")
    _write(comp, "deploy notes: api_key = hunter2hunter2hunter2")
    out = _run(_agent(), "/memory search deploy notes")
    assert "hunter2" not in out
    assert "vault entry" not in out
    assert "0 served" in out
    assert "vault" in out and "secret_routed_to_vault" in out


def test_a_remote_provider_gets_default_scoped_memory_denied_by_tier():
    comp = _composition()
    frag = _write(comp, "internal roadmap notes for the offsite")
    out = _run(_agent(CLOUD), "/memory search roadmap offsite")
    assert frag.id not in out
    assert "tier_mismatch" in out


def test_controlled_items_are_withheld_from_a_provider_the_tier_policy_does_not_admit():
    comp = _composition()
    frag = _write_classified(
        comp,
        "calibration procedure handbook",
        ClassificationStamp(export_control=ExportControl(itar=True)),
    )
    # LOCAL's routing tier is "any": tier_policy.admits() does not clear it for controlled content.
    out = _run(_agent(LOCAL), "/memory search calibration handbook")
    assert frag.id not in out and "calibration procedure" not in out
    assert "0 served" in out
    assert "1 withheld" in out and "tier policy" in out


def test_controlled_items_are_shown_to_a_provider_cleared_for_them():
    comp = _composition()
    frag = _write_classified(
        comp,
        "calibration procedure handbook",
        ClassificationStamp(export_control=ExportControl(itar=True)),
    )
    out = _run(_agent(LOCAL_EC), "/memory search calibration handbook")
    assert frag.id in out
    assert "1 served" in out


def test_search_on_unreachable_database_says_so_without_a_stack_trace(monkeypatch):
    from axiom.memory.pg_store import MemoryStoreUnavailable

    def boom(*a, **k):
        raise MemoryStoreUnavailable("Postgres is the runtime database (ADR-174); start it first")

    monkeypatch.setattr("axiom.extensions.builtins.chat.memory_wiring._build_user_composition", boom)
    out = _run(_agent(), "/memory search anything")
    assert "Postgres is the runtime database" in out
    assert "Traceback" not in out


def test_show_serves_one_fragment_by_id_and_by_unique_prefix():
    comp = _composition()
    frag = _write(comp, "the lab notebook lives in the shared drive")
    assert "lab notebook" in _run(_agent(), f"/memory show {frag.id}")
    assert "lab notebook" in _run(_agent(), f"/memory show {frag.id[:8]}")


def test_show_a_denied_fragment_names_the_reason_and_not_the_content():
    comp = _composition()
    other = SourceOrigin("claude-code", "someone-else", "r", datetime.now(UTC).isoformat())
    frag = _write(comp, "private thing from another account", origin=other)
    out = _run(_agent(), f"/memory show {frag.id}")
    assert "private thing" not in out
    assert "cross_account" in out


def test_show_unknown_id_says_not_found():
    assert "no memory" in _run(_agent(), "/memory show nope-nope").lower()


def test_recent_lists_newest_first_through_the_gate():
    comp = _composition()
    _write(comp, "first note")
    _write(comp, "second note")
    _write(comp, "vaulted note", ctype="vault")
    out = _run(_agent(), "/memory recent 5")
    assert out.index("second note") < out.index("first note")
    assert "vaulted note" not in out
    assert "vault" in out


def test_add_writes_a_fragment_labelled_with_the_session_and_it_is_recallable():
    out = _run(_agent(), "/memory add the staging cluster is called boreal")
    assert "added" in out.lower()
    comp = _composition()
    frags = [
        a for a in comp.artifact_registry.list(kind="fragment")
        if "boreal" in str(a.data.get("content"))
    ]
    assert len(frags) == 1
    prov = frags[0].data["provenance"]
    assert prov["session_id"] == "sess-1"
    assert prov["principal_id"] == ME
    assert "boreal" in _run(_agent(), "/memory search staging cluster boreal")


def test_add_refuses_secret_shaped_text():
    out = _run(_agent(), "/memory add token = ghp_abcdefghijklmnopqrstuvwxyz0123")
    assert "credential" in out.lower() and "not remembered" in out.lower()
    assert not [a for a in _composition().artifact_registry.list(kind="fragment")]


def test_forget_requires_yes_then_tombstones_and_leaves_recall():
    comp = _composition()
    frag = _write(comp, "temporary scratch idea about heliotrope")
    refused = _run(_agent(), f"/memory forget {frag.id}")
    assert "--yes" in refused
    assert "heliotrope" in _run(_agent(), "/memory search heliotrope")
    done = _run(_agent(), f"/memory forget {frag.id} --yes")
    assert "forgot" in done.lower()
    assert frag.id not in _run(_agent(), "/memory search heliotrope")


def test_no_principal_says_how_to_fix_it(monkeypatch):
    monkeypatch.setattr(
        "axiom.memory.session_capture.resolve_principal_id",
        lambda explicit: (_ for _ in ()).throw(ValueError("principal_id required")),
    )
    out = _run(_agent(principal=""), "/memory search x")
    assert "principal" in out.lower()
    assert "Traceback" not in out


def test_the_command_is_registered_with_subcommands():
    cmd = REGISTRY.get("/memory")
    assert cmd is not None
    assert {"search", "show", "recent", "add", "forget"} <= set(cmd.subcommands)
