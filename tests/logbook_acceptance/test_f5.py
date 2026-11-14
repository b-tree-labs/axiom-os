# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit F5: ``log`` is the command noun for logs (ADR-243).

Ticket: docs/working/logbook-units/F5.md in the product repo that holds the plan. A person types
``axi log list`` and ``axi log demo show``; the system log is ``axi log platform ...``; every old
``axi attest ...`` form and the old ``axi log tail|verify|...`` forward to the new form with one
stderr line. Code keeps ``logbook``: no skill, MCP tool or route is renamed.
"""

from __future__ import annotations

import io
import json
import logging
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from axiom.extensions.builtins.attest import cli as attest_cli
from axiom.extensions.builtins.attest import log_cli, registry, service
from axiom.extensions.builtins.attest.logbooks import LogbookError, parse_logbook
from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext

SITE = "site-a"


def _decl(lid="x_log", handle=None):
    head = {"id": lid, "version": "1"}
    if handle is not None:
        head["handle"] = handle
    return {
        "logbook": head,
        "type": [{"id": "NOTE", "meanings": ["authored"], "roles": ["operator"]}],
    }


def _run(fn, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def demo_only():
    from axiom.extensions.builtins.attest.tests.conftest import DEMO_LOGBOOK

    registry.reset()
    registry.register_file(DEMO_LOGBOOK)
    yield
    registry.reset()


@pytest.fixture
def cli_ctx(monkeypatch, roles):
    """``log_cli`` with a scripted prompt: ``answers`` None is no person at the prompt."""
    from axiom.extensions.builtins.attest.skills import bind_default

    state = {"answers": None}

    def build(prompt_wanted: bool = True):
        replies = state["answers"]

        def prompt(_text):
            if not replies:
                raise EOFError
            return replies.pop(0)

        return SkillContext(
            registry=bind_default(),
            state_dir=roles,
            logger=logging.getLogger("test.f5"),
            user_prompt=prompt if replies is not None else None,
            principal=PrincipalContext(handle="@op1:site-a", posture="sso", assured=True),
        )

    monkeypatch.setattr(log_cli, "_build_ctx", build)
    return state


# -- LB-F5-A5: handles -----------------------------------------------------------------------


@pytest.mark.acceptance("LB-F5-A5")
def test_lb_f5_a5_a_handle_defaults_to_the_id_and_may_be_declared():
    assert parse_logbook(_decl("ops")).handle == "ops"
    assert parse_logbook(_decl("x_log", handle="xl")).handle == "xl"
    # an id with an underscore reads with a hyphen, since a handle is what a person types
    assert parse_logbook(_decl("x_log")).handle == "x-log"


@pytest.mark.acceptance("LB-F5-A5")
@pytest.mark.parametrize("bad", ["Demo", "9lives", "a" * 25, "has space", "under_score", ""])
def test_lb_f5_a5_a_malformed_handle_is_refused(bad):
    with pytest.raises(LogbookError, match="handle"):
        parse_logbook(_decl(handle=bad))


@pytest.mark.acceptance("LB-F5-A5")
@pytest.mark.parametrize("word", ["platform", "list", "waiting", "anchor", "tail", "verify", "help"])
def test_lb_f5_a5_a_reserved_handle_is_refused(word):
    with pytest.raises(LogbookError, match=f"reserved.*{word}|{word}.*reserved"):
        parse_logbook(_decl(handle=word))
    # the same word as an id's default handle is refused too
    with pytest.raises(LogbookError, match="reserved"):
        parse_logbook(_decl(word))


@pytest.mark.acceptance("LB-F5-A5")
def test_lb_f5_a5_a_duplicate_handle_is_refused_with_the_clash_named(demo_only):
    other = parse_logbook(_decl("other_log", handle="demo"), source="other.toml")
    with pytest.raises(LogbookError, match="demo.*demo_log|demo_log.*demo"):
        registry.register(other)


@pytest.mark.acceptance("LB-F5-A5")
def test_lb_f5_a5_every_reserved_word_is_one_the_cli_owns():
    from axiom.extensions.builtins.attest.logbooks import RESERVED_HANDLES

    assert set(log_cli.NOUN_VERBS) | {"platform"} | set(log_cli.OLD_PLATFORM_VERBS) <= RESERVED_HANDLES


@pytest.mark.acceptance("LB-F5-A5")
def test_lb_f5_a5_an_unknown_handle_exits_2_with_the_nearest_match(demo_only):
    rc, _out, err = _run(log_cli.main, ["dem", "show"])
    assert rc == 2
    assert "demo" in err and "dem" in err


@pytest.mark.acceptance("LB-F5-A5")
def test_lb_f5_a5_a_logbook_id_where_a_handle_belongs_names_the_handle(demo_only):
    rc, _out, err = _run(log_cli.main, ["demo_log", "show"])
    assert rc == 2
    assert "log demo show" in err


# -- LB-F5-A6: the verb-to-skill map and Tab --------------------------------------------------


def _annotated():
    import argparse

    from axiom import axiom_cli
    from axiom.cli import command_space as cs
    from axiom.cli import completion_index as ci
    from axiom.cli.command_space import load_specs

    top = argparse.ArgumentParser(prog="axi")
    sub = top.add_subparsers(dest="subcommand")
    noun = log_cli.get_parser()
    node = sub.add_parser("log", help="Logs", description=noun.description)
    axiom_cli._copy_subparsers(noun, node)
    axiom_cli._copy_top_level_args(noun, node)
    return cs.annotate(ci.build_doc(top, {}), load_specs(), maps={"log": dict(log_cli.SKILL_FOR_VERB)})


APPENDIX_A = {
    "list": "attest.logbook_list",
    "waiting": "attest.drafts",
    "validate": "attest.logbook_validate",
    "anchor": "attest.anchor",
    "role grant": "attest.role_grant",
    "role revoke": "attest.role_revoke",
    "role list": "attest.role_list",
    "device enroll": "attest.device_enroll",
    "device reclaim": "attest.device_reclaim",
    "device retire": "attest.device_retire",
    "device list": "attest.device_list",
    "location init": "attest.location_init",
    "location code": "attest.location_code",
    "demo about": "attest.logbook_list",
    "demo show": "attest.show",
    "demo intervals": "attest.intervals",
    "demo due": "attest.obligations",
    "demo verify": "attest.verify",
    "demo new": "attest.new",
    "demo sign": "attest.sign",
    "demo propose": "attest.propose",
    "demo supplement": "attest.supplement",
    "demo acknowledge": "attest.ack",
    "demo cosign": "attest.cosign",
    "demo export": "attest.export",
    "demo export-preview": "attest.export_preview",
    "demo seal": "attest.seal",
    "demo seal-preview": "attest.seal_preview",
    "timeline": "journal.tail",
    "platform tail": "log.tail",
    "platform verify": "log.verify",
    "platform stats": "log.stats",
    "platform export": "log.export",
}


@pytest.mark.acceptance("LB-F5-A6")
def test_lb_f5_a6_every_log_verb_path_maps_to_its_skill(demo_only):
    mapped = log_cli.SKILL_FOR_VERB
    for path, skill in APPENDIX_A.items():
        assert mapped.get(path) == skill, path
    tree = _annotated()["commands"][0]["tree"]
    assert tree["subs"]["demo"]["subs"]["show"]["skill"] == "attest.show"
    assert tree["subs"]["platform"]["subs"]["tail"]["skill"] == "log.tail"


@pytest.mark.acceptance("LB-F5-A6")
def test_lb_f5_a6_tab_completes_the_handle_after_log(demo_only):
    from axiom.cli import command_space as cs

    space = cs.from_doc(_annotated())
    log = space.find(["log"])
    assert "demo" in log.children
    assert {"show", "verify", "due", "about"} <= set(log.children["demo"].children)
    # an old platform verb is hidden from the tree, never offered
    assert "tail" not in log.children


@pytest.mark.acceptance("LB-F5-A6")
def test_lb_f5_a6_the_log_value_kind_is_filled_from_the_list_skill(demo_only):
    from axiom.cli import completion_values
    from axiom.extensions.builtins.attest import completion_values as attest_values

    assert "log" in completion_values.KINDS
    found = attest_values.collect(completion_values.ProviderContext(binding={}, deadline=1e18))
    assert found["log"] == ["demo"]


# -- LB-F5-A7: no skill or tool renamed; no sign path for software ---------------------------

#: The attest skills on main before F5, with their surfaces.
SKILLS_BEFORE = {
    "attest.logbook_list": ("cli", "mcp", "agent_tool"),
    "attest.logbook_validate": ("cli",),
    "attest.show": ("cli", "mcp", "agent_tool"),
    "attest.verify": ("cli", "mcp", "agent_tool"),
    "attest.export": ("cli",),
    "attest.new": ("cli",),
    "attest.sign": ("cli",),
    "attest.propose": ("cli", "mcp", "agent_tool"),
    "attest.drafts": ("cli", "mcp", "agent_tool"),
}


@pytest.mark.acceptance("LB-F5-A7")
def test_lb_f5_a7_no_skill_or_tool_name_changed_and_none_signs_for_software():
    from axiom.extensions.builtins.attest.skills import SPECS

    have = {s.name: tuple(s.surfaces) for s in SPECS}
    for name, surfaces in SKILLS_BEFORE.items():
        assert have.get(name) == surfaces, name
    signing = {"attest.new", "attest.sign", "attest.supplement", "attest.ack", "attest.cosign"}
    for s in SPECS:
        if s.name in signing:
            assert not {"mcp", "agent_tool"} & set(s.surfaces), s.name
    # every verb's skill is a registered one
    names = set(have) | {"log.tail", "log.verify", "log.stats", "log.export", "log.backend", "log.sinks",
                         "journal.tail"}  # fmt: skip
    assert set(log_cli.SKILL_FOR_VERB.values()) <= names


@pytest.mark.acceptance("LB-F5-A7")
def test_lb_f5_a7_descriptions_say_log():
    import re

    from axiom.extensions.builtins.attest.skills import SPECS

    for s in SPECS:
        prose = re.sub(r"`[^`]*`", "", s.description)  # a code name in backticks may say logbook
        assert "logbook" not in prose.lower(), s.name


# -- LB-F5-A8: the system log verbs run through skills ---------------------------------------


@pytest.mark.acceptance("LB-F5-A8")
@pytest.mark.parametrize(
    "verb,skill",
    [("tail", "log.tail"), ("verify", "log.verify"), ("stats", "log.stats"),
     ("export", "log.export"), ("backend", "log.backend"), ("sinks", "log.sinks")],
)  # fmt: skip
def test_lb_f5_a8_platform_verbs_run_through_their_skill(monkeypatch, verb, skill):
    from axiom.extensions.builtins.log import cli as platform_cli
    from axiom.infra.skills import SkillResult

    called = []

    def fake(registry, name, params, ctx, *, surface):
        called.append((name, surface))
        return SkillResult(ok=True, value={
            "records": [], "end": 0, "ok": True, "name": "x", "sinks": [],
            "backend": "x", "files": {}, "routing_events": None,
        })  # fmt: skip

    monkeypatch.setattr(platform_cli, "invoke_capability", fake)
    rc, _out, _err = _run(log_cli.main, ["platform", verb])
    assert rc == 0
    assert called == [(skill, "cli")]


@pytest.mark.acceptance("LB-F5-A8")
def test_lb_f5_a8_the_platform_skills_are_registered_read_only_on_the_cli():
    from axiom.extensions.builtins.log.skills import SPECS

    have = {s.name: s for s in SPECS}
    assert set(have) == {"log.tail", "log.verify", "log.stats", "log.export", "log.backend", "log.sinks"}
    for s in have.values():
        assert tuple(s.surfaces) == ("cli",) and s.side_effects is False


@pytest.mark.acceptance("LB-F5-A8")
def test_lb_f5_a8_the_tail_skill_reads_the_routing_events(monkeypatch, tmp_path):
    from axiom.extensions.builtins.log.skills import platform

    path = tmp_path / "routing_events.jsonl"
    path.write_text(
        "\n".join(json.dumps({"ts": f"2026-11-1{i}T00:00:00Z", "provider": p}) for i, p in enumerate("abc"))
        + "\nnot json\n"
    )
    monkeypatch.setattr(platform, "routing_events_path", lambda: path)
    r = platform.tail({"n": 2}, None)
    assert r.ok and [x["provider"] for x in r.value["records"]] == ["b", "c"]
    r = platform.tail({"provider": "a"}, None)
    assert [x["provider"] for x in r.value["records"]] == ["a"]


# -- LB-F5-A4: the old system log verbs forward ----------------------------------------------


@pytest.mark.acceptance("LB-F5-A4")
@pytest.mark.parametrize("verb", ["tail", "verify", "stats", "backend", "sinks", "export"])
def test_lb_f5_a4_an_old_platform_verb_forwards_with_one_line(monkeypatch, verb):
    monkeypatch.delenv("AXI_NO_DEPRECATION_WARNING", raising=False)
    seen = []
    monkeypatch.setattr(log_cli, "_platform", lambda argv: seen.append(argv) or 0)
    rc, _out, err = _run(log_cli.main, [verb, "--x"])
    assert rc == 0 and seen == [[verb, "--x"]]
    lines = err.strip().splitlines()
    assert len(lines) == 1 and f"log platform {verb}" in lines[0]
    monkeypatch.setenv("AXI_NO_DEPRECATION_WARNING", "1")
    _rc, _out, err = _run(log_cli.main, [verb])
    assert err == ""


@pytest.mark.acceptance("LB-F5-A4")
def test_lb_f5_a4_an_old_tail_of_a_lane_forwards_to_the_timeline(monkeypatch):
    """J3 put the Timeline's Tail on `log tail --lane`; its new form is `log timeline --lane`."""
    monkeypatch.delenv("AXI_NO_DEPRECATION_WARNING", raising=False)
    seen = []
    monkeypatch.setattr(log_cli, "_timeline", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(log_cli, "_platform", lambda argv: pytest.fail("not Platform events"))
    rc, _out, err = _run(log_cli.main, ["tail", "--lane", "platform", "-f"])
    assert rc == 0 and seen == [["--lane", "platform", "-f"]]
    lines = err.strip().splitlines()
    assert len(lines) == 1 and "log timeline" in lines[0]


@pytest.mark.acceptance("LB-F5-A4")
@pytest.mark.parametrize("verb", ["tail", "trace"])
def test_lb_f5_a4_after_removal_an_old_platform_verb_fails_and_reaches_no_log(monkeypatch, verb):
    monkeypatch.setattr(log_cli, "FORWARDERS_REMOVED_IN", "v0.1.0")
    seen = []
    monkeypatch.setattr(log_cli, "_platform", lambda argv: seen.append(argv) or 0)
    monkeypatch.setattr(log_cli, "_per_log", lambda *a: seen.append(a) or 0)
    rc, _out, err = _run(log_cli.main, [verb])
    assert rc == 2 and seen == []
    assert f"log platform {verb}" in err


# -- LB-F5-A3: the old attest forms forward --------------------------------------------------

#: prd-attestation §6b "Moving from today's commands", plus the verbs units added under
#: ``attest`` before F5 (L1-L4, W3, X2), which this unit moves.
OLD_FORMS = [
    (["logbook", "list"], ["list"]),
    (["logbook", "list", "--json"], ["list", "--json"]),
    (["logbook", "validate", "f.toml"], ["validate", "f.toml"]),
    (["new", "--logbook", "demo_log", "--type", "NOTE", "--meaning", "authored", "--title", "t"],
     ["demo", "new", "NOTE", "--meaning", "authored", "--title", "t"]),
    (["show", "--logbook", "demo_log"], ["demo", "show"]),
    (["show", "abc", "--logbook", "demo_log", "--limit", "3"], ["demo", "show", "abc", "--limit", "3"]),
    (["verify", "--logbook", "demo_log", "--json"], ["demo", "verify", "--json"]),
    (["obligations", "--logbook", "demo_log"], ["demo", "due"]),
    (["obligations"], ["due"]),
    (["export", "--logbook", "demo_log", "--out", "d"], ["demo", "export", "--out", "d"]),
    (["anchor"], ["anchor"]),
    (["device", "list"], ["device", "list"]),
    (["role", "grant", "@a:b", "operator"], ["role", "grant", "@a:b", "operator"]),
    (["location", "code", "desk"], ["location", "code", "desk"]),
    (["drafts"], ["waiting"]),
    (["drafts", "--logbook", "demo_log", "--limit", "5"], ["waiting", "--log", "demo", "--limit", "5"]),
    (["propose", "--logbook", "demo_log", "--type", "NOTE", "--meaning", "authored", "--title", "t",
      "--for", "@a:b"],
     ["demo", "propose", "NOTE", "--meaning", "authored", "--title", "t", "--for", "@a:b"]),
    (["intervals", "--logbook", "demo_log"], ["demo", "intervals"]),
    (["unacknowledged", "--logbook", "demo_log"], ["demo", "unacknowledged"]),
    (["cosign-pending", "--logbook", "demo_log"], ["demo", "cosign-pending"]),
    (["access", "--logbook", "demo_log"], ["demo", "access"]),
    (["access"], ["access"]),
    (["verify", "--all"], ["verify-all"]),
    (["seal", "--logbook", "demo_log", "--summary", "s"], ["demo", "seal", "--summary", "s"]),
    (["export", "--logbook", "demo_log", "--format", "csv", "--type", "NOTE"],
     ["demo", "export", "--format", "csv", "--type", "NOTE"]),
    (["export-preview", "--logbook", "demo_log", "--format", "csv"],
     ["demo", "export-preview", "--format", "csv"]),
    (["seal-preview", "--logbook", "demo_log", "--to-seq", "4"], ["demo", "seal-preview", "--to-seq", "4"]),
]  # fmt: skip


@pytest.mark.acceptance("LB-F5-A3")
@pytest.mark.parametrize("old,new", OLD_FORMS)
def test_lb_f5_a3_every_old_attest_form_translates_to_its_new_form(demo_only, old, new):
    assert attest_cli.translate(old) == new


@pytest.mark.acceptance("LB-F5-A3")
def test_lb_f5_a3_an_old_form_runs_its_new_form_with_one_line(demo_only, monkeypatch):
    monkeypatch.delenv("AXI_NO_DEPRECATION_WARNING", raising=False)
    seen = []
    monkeypatch.setattr(log_cli, "main", lambda argv: seen.append(argv) or 1)
    rc, _out, err = _run(attest_cli.main, ["verify", "--logbook", "demo_log"])
    assert rc == 1 and seen == [["demo", "verify"]]
    lines = err.strip().splitlines()
    assert len(lines) == 1
    assert '"axi attest verify" is now "axi log demo verify"' in lines[0]
    assert attest_cli.FORWARDERS_REMOVED_IN in lines[0]
    monkeypatch.setenv("AXI_NO_DEPRECATION_WARNING", "1")
    _rc, _out, err = _run(attest_cli.main, ["verify", "--logbook", "demo_log"])
    assert err == ""


@pytest.mark.acceptance("LB-F5-A3")
def test_lb_f5_a3_a_held_entry_keeps_its_old_exit_code(demo_only, monkeypatch):
    """``axi attest new`` answered hold exited 0 and with nobody at the prompt 1; the new form
    exits 3 for both, and the old form keeps its codes."""
    def held(argv):
        log_cli.LAST_UNSIGNED["why"] = "answered"
        return log_cli.NOTHING_SIGNED

    def nobody(argv):
        log_cli.LAST_UNSIGNED["why"] = "nobody"
        return log_cli.NOTHING_SIGNED

    argv = ["new", "--logbook", "demo_log", "--type", "NOTE", "--meaning", "authored", "--title", "t"]
    monkeypatch.setattr(log_cli, "main", held)
    assert _run(attest_cli.main, argv)[0] == 0
    monkeypatch.setattr(log_cli, "main", nobody)
    assert _run(attest_cli.main, argv)[0] == 1


@pytest.mark.acceptance("LB-F5-A3")
def test_lb_f5_a3_an_unknown_old_verb_is_a_usage_error(demo_only):
    rc, _out, _err = _run(attest_cli.main, ["frobnicate"])
    assert rc == 2


# -- LB-F5-A1 and A2: the same answers, and signing only at the prompt (Postgres) ------------


def _json(fn, argv):
    rc, out, err = _run(fn, argv)
    return rc, json.loads(out), err


@pytest.mark.acceptance("LB-F5-A1")
def test_lb_f5_a1_reads_return_the_same_json_as_the_skills(attest_db, bound_signer, roles, cli_ctx):
    cli_ctx["answers"] = ["sign"]
    rc, _o, _e = _run(log_cli.main, ["demo", "new", "NOTE", "--site", SITE, "--title", "a note",
                                     "--field", "text=hello"])  # fmt: skip
    assert rc == 0, _e
    ctx = log_cli._build_ctx()

    def skill(name, params):
        r = ctx.registry.invoke(name, params, ctx)
        return {"ok": r.ok, "value": json.loads(json.dumps(r.value, default=str)), "errors": r.errors}

    rc, listed, _ = _json(log_cli.main, ["list", "--json"])
    assert rc == 0 and listed == skill("attest.logbook_list", {})
    rc, about, _ = _json(log_cli.main, ["demo", "about", "--json"])
    assert rc == 0
    assert about["value"]["logbooks"] == [b for b in listed["value"]["logbooks"] if b["id"] == "demo_log"]
    for argv, name in (
        (["demo", "show", "--site", SITE, "--json"], "attest.show"),
        (["demo", "verify", "--site", SITE, "--json"], "attest.verify"),
        (["demo", "due", "--site", SITE, "--json"], "attest.obligations"),
    ):
        rc, got, _ = _json(log_cli.main, argv)
        want = skill(name, {"site": SITE, "logbook": "demo_log", "limit": 20} if name == "attest.show"
                     else {"site": SITE, "logbook": "demo_log"})  # fmt: skip
        assert rc == 0, argv
        assert got == want, argv


@pytest.mark.acceptance("LB-F5-A1")
def test_lb_f5_a1_a_skill_accepts_the_handle_where_it_accepts_the_id(attest_db, bound_signer, roles, cli_ctx):
    ctx = log_cli._build_ctx()
    by_id = ctx.registry.invoke("attest.verify", {"site": SITE, "logbook": "demo_log"}, ctx)
    by_handle = ctx.registry.invoke("attest.verify", {"site": SITE, "logbook": "demo"}, ctx)
    assert by_id.ok and by_handle.ok
    assert by_handle.value["logbook"] == "demo_log"
    assert by_handle.value["checked"] == by_id.value["checked"]


NEW = ["demo", "new", "ROUND_CHECK", "--site", SITE, "--title", "Round check",
       "--field", "reading=ok", "--field", "walkdown=true"]  # fmt: skip


@pytest.mark.acceptance("LB-F5-A2")
def test_lb_f5_a2_new_signs_only_with_sign_at_the_prompt(attest_db, bound_signer, roles, cli_ctx):
    cli_ctx["answers"] = ["sign"]
    rc, out, err = _run(log_cli.main, NEW)
    assert rc == 0, err
    assert len(service.records(SITE, "demo_log")) == 1
    assert "next:" in out


@pytest.mark.acceptance("LB-F5-A2")
@pytest.mark.parametrize("answers", [["hold"], ["ask"], [], None])
def test_lb_f5_a2_held_asked_or_unanswered_exits_3_and_signs_nothing(
    attest_db, bound_signer, roles, cli_ctx, answers
):
    cli_ctx["answers"] = answers
    rc, _out, _err = _run(log_cli.main, NEW)
    assert rc == log_cli.NOTHING_SIGNED == 3
    assert service.records(SITE, "demo_log") == []


@pytest.mark.acceptance("LB-F5-A2")
def test_lb_f5_a2_sign_a_draft_exits_3_on_hold_and_0_on_sign(attest_db, bound_signer, roles, cli_ctx):
    draft = service.create_draft(
        site_id=SITE, logbook="demo_log", entry_type="NOTE", meaning="authored",
        content={"title": "t", "fields": {"text": "x"}}, origin="human", for_principal="@op1:site-a",
    )  # fmt: skip
    cli_ctx["answers"] = ["hold"]
    rc, _o, _e = _run(log_cli.main, ["demo", "sign", draft])
    assert rc == 3 and service.records(SITE, "demo_log") == []
    cli_ctx["answers"] = ["sign"]
    rc, _o, err = _run(log_cli.main, ["demo", "sign", draft])
    assert rc == 0, err
    assert len(service.records(SITE, "demo_log")) == 1


@pytest.mark.acceptance("LB-F5-A2")
def test_lb_f5_a2_sign_refuses_a_draft_of_another_log(attest_db, bound_signer, roles, cli_ctx, tmp_path):
    other = tmp_path / "other.toml"
    other.write_text(
        '[logbook]\nid = "other_log"\nversion = "1"\nhandle = "other"\n'
        '[[type]]\nid = "NOTE"\nmeanings = ["authored"]\nroles = ["operator"]\n'
        'fields = [{ id = "text", type = "text" }]\n'
    )
    registry.register_file(Path(other))
    draft = service.create_draft(
        site_id=SITE, logbook="demo_log", entry_type="NOTE", meaning="authored",
        content={"title": "t", "fields": {"text": "x"}}, origin="human", for_principal="@op1:site-a",
    )  # fmt: skip
    cli_ctx["answers"] = ["sign"]
    rc, _o, err = _run(log_cli.main, ["other", "sign", draft])
    assert rc == 1 and "log demo sign" in err
    assert service.records(SITE, "demo_log") == []


@pytest.mark.acceptance("LB-F5-A3")
def test_lb_f5_a3_attest_sign_reads_the_drafts_log(attest_db, bound_signer, roles, monkeypatch):
    draft = service.create_draft(
        site_id=SITE, logbook="demo_log", entry_type="NOTE", meaning="authored",
        content={"title": "t", "fields": {"text": "x"}}, origin="human", for_principal="@op1:site-a",
    )  # fmt: skip
    assert attest_cli.translate(["sign", draft, "--json"]) == ["demo", "sign", draft, "--json"]


@pytest.mark.acceptance("LB-F5-A3")
def test_lb_f5_a3_an_entry_named_by_id_forwards_to_its_own_log(attest_db, bound_signer, roles, cli_ctx):
    cli_ctx["answers"] = ["sign"]
    rc, _o, err = _run(log_cli.main, NEW)
    assert rc == 0, err
    rid = service.records(SITE, "demo_log")[0]["attestation_id"]
    assert attest_cli.translate(["ack", rid]) == ["demo", "acknowledge", rid]
    assert attest_cli.translate(["cosign", rid, "--json"]) == ["demo", "cosign", rid, "--json"]
    assert attest_cli.translate(["supplement", rid, "--reason", "addition"]) == [
        "demo", "supplement", rid, "--reason", "addition",
    ]  # fmt: skip
    assert attest_cli.translate(["show", rid]) == ["demo", "show", rid]
    # an unknown id has no new form: a usage error, never a guess
    assert attest_cli.translate(["ack", "no-such-id"]) is None


@pytest.mark.acceptance("LB-F5-A7")
def test_lb_f5_a7_one_extension_owns_the_log_noun():
    """A product never registers a second ``log`` noun (ADR-243 decision 5)."""
    import tomllib

    from axiom.extensions.discovery import discover_extensions

    owners = []
    for ext in discover_extensions():
        data = tomllib.loads(Path(ext.manifest_path).read_text(encoding="utf-8"))
        for p in (data.get("extension") or {}).get("provides") or []:
            if p.get("kind") == "cmd" and p.get("noun") == "log":
                owners.append(ext.name)
    assert owners == ["attest"]


@pytest.mark.acceptance("LB-F5-A1")
def test_lb_f5_a1_the_export_verbs_take_the_l6_options():
    """L6 merged its export forms under `attest export`; they move to `log <log> export`."""
    p = log_cli.log_parser("demo")
    a = p.parse_args(["export", "--format", "csv", "--format", "text", "--form", "packet", "--type", "NOTE",
                      "--from", "2026-11-01", "--no-supplements"])  # fmt: skip
    assert a.format == ["csv", "text"] and a.form == ["packet"] and a.entry_type == ["NOTE"]
    assert getattr(a, "from") == "2026-11-01" and a.include_supplements is False and a.out is None
    b = p.parse_args(["export-preview", "--format", "jsonl", "-q", "words"])
    assert b.format == "jsonl" and b.q == "words"
