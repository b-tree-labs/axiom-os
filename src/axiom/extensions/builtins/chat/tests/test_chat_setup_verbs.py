# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Chat can run the setup verbs: doctor, binding status and update. It confirms before any change.

"Every step can be done in plain English" needs the verbs under it. ``doctor.run`` says what is wrong
and the fix, ``binding.status`` answers "am I connected to the site?", ``update.check`` is the dry run
and ``update.apply`` changes the install, so it is a write that the approval gate puts to the person.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat import tools as chat_tools
from axiom.extensions.builtins.data_platform import read_target
from axiom.infra import serving_mode
from axiom.infra.orchestrator.actions import ActionCategory

FACE = "https://node.example.org"


class _Settings:
    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    for name in ("DATABASE_URL", "DP1_RAG_DSN", "AXIOM_DB_URL", serving_mode.SAMPLE_DATA_ENV):
        monkeypatch.delenv(name, raising=False)
    values: dict = {}
    monkeypatch.setattr(
        "axiom.extensions.builtins.settings.store.SettingsStore", lambda: _Settings(values)
    )
    monkeypatch.setattr(read_target, "holds_medallion", lambda: False)
    return state, values


def _bind(state) -> None:
    (state / read_target.STATE_FILENAME).write_text(
        json.dumps(
            {
                "current": "alpha-site",
                "bindings": {"alpha-site": {"site": "alpha-site", "face_url": FACE}},
            }
        ),
        encoding="utf-8",
    )


def _call(tool: str, params: dict | None = None) -> dict:
    return chat_tools.execute_tool(tool, params or {})


# ---- the tools are there, and the one that changes something is a write ---------------------


def test_every_chat_offers_the_setup_verbs():
    tools = chat_tools.get_all_tools()
    assert {"doctor__run", "binding__status", "update__check", "update__apply"} <= set(tools)


def test_only_update_apply_is_a_write():
    tools = chat_tools.get_all_tools()
    assert tools["update__apply"].category == ActionCategory.WRITE
    for name in ("doctor__run", "binding__status", "update__check"):
        assert tools[name].category == ActionCategory.READ, name


def test_the_setting_withholds_them(isolated):
    _state, values = isolated
    values[chat_tools.SETUP_VERBS_SETTING] = False
    assert not {"doctor__run", "binding__status", "update__check", "update__apply"} & set(
        chat_tools.get_all_tools()
    )


# ---- binding.status: "am I connected to the site?" ------------------------------------------


def test_not_bound_says_so_and_how_to_bind():
    value = _call("binding__status")["value"]
    assert value["mode"] == "local" and value["reachable"] is None
    assert "Not connected to a site" in value["text"]
    assert "site bind" in value["fix"]


def test_bound_and_reachable(isolated, monkeypatch):
    _bind(isolated[0])
    monkeypatch.setattr(
        serving_mode,
        "probe",
        lambda mode=None, **kw: serving_mode.NodeProbe(reachable=True, version="1.2.3"),
    )
    value = _call("binding__status")["value"]
    assert value["mode"] == "bound" and value["reachable"] is True
    assert value["text"].startswith("Connected to alpha-site at node.example.org.")
    assert "version 1.2.3" in value["text"]
    assert "alpha-site via node.example.org" in value["text"]  # where the data comes from, in words


def test_bound_but_the_node_does_not_answer(isolated, monkeypatch):
    _bind(isolated[0])
    monkeypatch.setattr(
        serving_mode,
        "probe",
        lambda mode=None, **kw: serving_mode.NodeProbe(
            reachable=False, offline=True, error="alpha-site at node.example.org: offline."
        ),
    )
    value = _call("binding__status")["value"]
    assert value["reachable"] is False
    assert "did not answer" in value["text"]
    assert "VPN" in value["fix"]


def test_test_mode_is_said_not_hidden(isolated, monkeypatch):
    _bind(isolated[0])
    monkeypatch.setenv(serving_mode.SAMPLE_DATA_ENV, "1")
    value = _call("binding__status")["value"]
    assert value["mode"] == "test"
    assert value["text"].startswith("TEST MODE")


def test_the_probe_can_be_skipped(isolated, monkeypatch):
    _bind(isolated[0])

    def boom(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("probed")

    monkeypatch.setattr(serving_mode, "probe", boom)
    value = _call("binding__status", {"probe": False})["value"]
    assert value["reachable"] is None and "not checked" in value["text"]


# ---- doctor.run: what is wrong, and the fix ------------------------------------------------


def test_doctor_names_each_problem_with_its_fix(monkeypatch):
    monkeypatch.setattr(
        "axiom.axiom_cli._gather_diagnostics",
        lambda: {
            "checks": [
                {"name": "Python", "ok": True, "status": "3.12"},
                {
                    "name": "Entry Point",
                    "ok": False,
                    "status": "not on PATH",
                    "fix": "uv tool update-shell",
                },
            ]
        },
    )
    result = _call("doctor__run")
    assert result["ok"]
    value = result["value"]
    assert value["healthy"] is False and value["issues"] == 1
    assert "fix for Entry Point: uv tool update-shell" in value["text"]
    assert value["text"].startswith("1 thing(s) need attention.")


def test_doctor_healthy(monkeypatch):
    monkeypatch.setattr(
        "axiom.axiom_cli._gather_diagnostics",
        lambda: {"checks": [{"name": "Python", "ok": True, "status": "3.12"}]},
    )
    value = _call("doctor__run")["value"]
    assert value["healthy"] is True
    assert value["text"].startswith("Everything checked is healthy.")


def test_doctor_calls_no_model_and_changes_nothing(monkeypatch):
    monkeypatch.setattr(
        "axiom.axiom_cli._gather_diagnostics",
        lambda: {"checks": [{"name": "x", "ok": False, "status": "bad"}]},
    )
    monkeypatch.setattr(
        "axiom.axiom_cli._llm_diagnose", MagicMock(side_effect=AssertionError("model called"))
    )
    monkeypatch.setattr(
        "axiom.axiom_cli._auto_fix_issues", MagicMock(side_effect=AssertionError("fixed"))
    )
    assert _call("doctor__run")["ok"]


# ---- update: the check is a dry run; the apply is confirmed ----------------------------------


def _updater(monkeypatch, *, changed: bool):
    from axiom.extensions.builtins.update import cli as update_cli

    calls = {"check": 0, "apply": 0}

    class Fake:
        def __init__(self, repo_root=None, dry_run=False):
            self.dry_run = dry_run
            self.results = []

        def check_updates(self):
            calls["check"] += 1
            return [update_cli.UpdateResult("deps", True, "Packages would be updated", changed)]

        def update_all(self, pull=False):
            calls["apply"] += 1
            assert not self.dry_run
            return [update_cli.UpdateResult("deps", True, "Updated", True)]

    monkeypatch.setattr(update_cli, "Updater", Fake)
    return calls


def test_update_check_installs_nothing(monkeypatch):
    calls = _updater(monkeypatch, changed=True)
    value = _call("update__check")["value"]
    assert calls == {"check": 1, "apply": 0}
    assert value["applied"] is False and value["up_to_date"] is False
    assert "would change something" in value["text"]


def test_update_check_when_current(monkeypatch):
    _updater(monkeypatch, changed=False)
    value = _call("update__check")["value"]
    assert value["up_to_date"] is True and value["text"].startswith("Nothing to update.")


def _agent_with_prompt(answer: str):
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.bus import EventBus
    from axiom.infra.gateway import Gateway
    from axiom.infra.orchestrator.session import Session

    gw = MagicMock(spec=Gateway)
    gw.available = True
    gw.active_provider = MagicMock()
    gw.active_provider.name = "test"
    gw.active_provider.model = "test-model"
    agent = ChatAgent(gateway=gw, bus=EventBus(), session=Session())
    render = MagicMock()
    render.render_approval_prompt.return_value = answer
    agent.set_render_provider(render)
    return agent, render


def _ask_for_update():
    from axiom.infra.gateway import CompletionResponse, ToolUseBlock

    return CompletionResponse(
        text="",
        provider="test",
        success=True,
        tool_use=[ToolUseBlock(tool_id="t1", name="update__apply", input={})],
    )


def test_update_apply_asks_and_a_no_changes_nothing(monkeypatch):
    calls = _updater(monkeypatch, changed=True)
    agent, render = _agent_with_prompt("r")
    results = agent._process_tool_calls(_ask_for_update())
    render.render_approval_prompt.assert_called_once()
    assert "Rejected" in results[0][2]["error"]
    assert calls["apply"] == 0


def test_update_apply_runs_once_the_person_says_yes(monkeypatch):
    calls = _updater(monkeypatch, changed=True)
    agent, render = _agent_with_prompt("a")
    results = agent._process_tool_calls(_ask_for_update())
    render.render_approval_prompt.assert_called_once()
    assert calls["apply"] == 1
    assert results[0][2]["ok"] is True


def test_a_headless_chat_never_applies_an_update(monkeypatch):
    """No operator, no change: the default non-interactive policy refuses the write."""
    from axiom.extensions.builtins.chat.approval_policy import NonInteractiveApprovalPolicy

    calls = _updater(monkeypatch, changed=True)
    agent, _render = _agent_with_prompt("a")
    agent.set_approval_policy(NonInteractiveApprovalPolicy())
    results = agent._process_tool_calls(_ask_for_update())
    assert "error" in results[0][2] and calls["apply"] == 0


# ---- asking chat to update: confirmed here, whoever the model is -----------------------------


def _self_agent(answer: str):
    agent, render = _agent_with_prompt(answer)
    return agent, render


def test_asking_to_update_shows_the_check_then_asks_and_a_no_changes_nothing(monkeypatch):
    calls = _updater(monkeypatch, changed=True)
    agent, render = _self_agent("r")
    reply = agent.turn("update neut to the newest version", stream=False)
    render.render_approval_prompt.assert_called_once()
    assert calls == {"check": 1, "apply": 0}
    assert reply.startswith("Not updated.")
    shown = [c.args[1] for c in render.render_message.call_args_list]
    assert any(
        "would change something" in m for m in shown
    )  # the person saw what it would do first


def test_asking_to_update_runs_it_once_the_person_says_yes(monkeypatch):
    calls = _updater(monkeypatch, changed=True)
    agent, render = _self_agent("a")
    reply = agent.turn("please upgrade my install", stream=False)
    render.render_approval_prompt.assert_called_once()
    assert calls["apply"] == 1 and reply.startswith("Updated.")


def test_a_headless_chat_asked_to_update_refuses(monkeypatch):
    from axiom.extensions.builtins.chat.approval_policy import NonInteractiveApprovalPolicy

    calls = _updater(monkeypatch, changed=True)
    agent, _render = _self_agent("a")
    agent.set_approval_policy(NonInteractiveApprovalPolicy())
    reply = agent.turn("update neut to the newest version", stream=False)
    assert reply.startswith("Not updated.") and calls["apply"] == 0


@pytest.mark.parametrize(
    "text",
    [
        "update the procedure to the latest version",
        "how do I update the report?",
        "is there a newer version of neut?",
    ],
)
def test_only_a_request_to_update_this_install_is_one(text):
    from axiom.extensions.builtins.status.selfanswer import wants_update

    assert not wants_update(text)


@pytest.mark.parametrize(
    "text", ["update neut to the newest version", "upgrade my install", "please update axi"]
)
def test_these_are_requests_to_update(text):
    from axiom.extensions.builtins.status.selfanswer import wants_update

    assert wants_update(text)


def test_check_my_setup_is_answered_from_the_doctor_with_no_model(monkeypatch):
    from axiom.extensions.builtins.status.selfanswer import answer_with_source

    monkeypatch.setattr(
        "axiom.axiom_cli._gather_diagnostics",
        lambda: {
            "checks": [
                {
                    "name": "Entry Point",
                    "ok": False,
                    "status": "not on PATH",
                    "fix": "uv tool update-shell",
                }
            ]
        },
    )
    answer, source = answer_with_source("check my setup, something is broken")
    assert source == "doctor.run" and "fix for Entry Point: uv tool update-shell" in answer


def test_is_there_a_newer_version_is_answered_from_the_check(monkeypatch):
    from axiom.extensions.builtins.status.selfanswer import answer_with_source

    _updater(monkeypatch, changed=True)
    answer, source = answer_with_source("is there a newer version of neut?")
    assert source == "update.check" and "would change something" in answer
