# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET C1: `mode` is the report posture, `land` says whether anything merges (#2238).

On 2026-11-21 two pull requests merged while the config said ``mode = "warn"``, because the
landing step never read it. ``mode`` keeps meaning the report posture. ``land`` is the
explicit switch, default true so a repository that relies on landing changes nothing.

Acceptance ids:

  C1-2238-A  ``land`` is a boolean in the config, default true; anything else is refused.
  C1-2238-B  ``land = false`` lands nothing, however green the pull request is.
  C1-2238-C  The default config still lands a green pull request, exactly as before.
  C1-2238-D  The first line of the text report says whether this run can merge and why: mode,
             land, kill switch, release lock.
  C1-2238-E  The same line is the first line of the run summary and a key of the JSON report.
  C1-2238-F  This repository's own config states ``land`` and explains it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from axiom.extensions.builtins.release.fixes import Diagnosis, Fix
from axiom.extensions.builtins.release.lander import skill as lander_skill
from axiom.extensions.builtins.release.lander import verdict
from axiom.extensions.builtins.release.lander.actor import run_actor
from axiom.extensions.builtins.release.lander.config import LanderConfig, parse_config
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import InMemoryActionStore
from axiom.infra.skills import SkillContext, SkillRegistry

REPO = "owner/name"
HEAD = "c" * 40
ROOT = Path(__file__).resolve().parents[6]
RAW = 'mode = "warn"\n[raci]\napprovers = ["@ada:local"]\nauto_allowed = ["merge"]\n'


# --- A: the schema ---------------------------------------------------------------------


def test_c1_2238_a_land_defaults_to_true():
    assert parse_config(RAW).land is True
    assert LanderConfig().land is True


def test_c1_2238_a_land_false_is_read():
    assert parse_config("land = false\n" + RAW).land is False


@pytest.mark.parametrize("bad", ['land = "no"', "land = 0", "land = 1", 'land = ""'])
def test_c1_2238_a_land_must_be_a_boolean(bad):
    with pytest.raises(LanderRefused, match="land must be true or false"):
        parse_config(bad + "\n" + RAW)


# --- B / C: the act pass ----------------------------------------------------------------


class _Effects:
    def __init__(self) -> None:
        self.landed: list[int] = []

    def head(self, number):
        return HEAD

    def apply(self, number, fix, head_sha):  # pragma: no cover
        raise AssertionError("nothing is applied")

    def rerun(self, number, sha):
        return "passing"

    def land(self, number, sha, method):
        self.landed.append(number)

    def close(self, number, sha):  # pragma: no cover
        raise AssertionError("nothing is closed")

    def add_label(self, number, label):
        pass

    def remove_label(self, number, label):
        pass


class _Fixes:
    name = "fake"

    def diagnose(self, pull):
        return Diagnosis("clear", "green", pull.files)

    def draft(self, diagnosis, pull):
        return Fix(summary="land", kind="land")


def _green() -> OpenPull:
    return OpenPull(number=7, title="t", head_sha=HEAD, head_ref="b", files=("src/a.py",), checks="passing", mergeable=True)


def _act(tmp_path, config):
    effects = _Effects()
    result = run_actor(
        [_green()], repo=REPO, config=config, state_dir=tmp_path, effects=effects,
        gate=ApprovalGate(InMemoryActionStore()), provider=_Fixes(), environ={},
    )
    return effects, result


def test_c1_2238_c_the_default_config_still_lands_a_green_pull_request(tmp_path):
    effects, result = _act(tmp_path, parse_config(RAW))
    assert effects.landed == [7]
    assert result.outcomes[0].status == "landed"


def test_c1_2238_b_land_false_lands_nothing(tmp_path):
    effects, result = _act(tmp_path, parse_config("land = false\n" + RAW))
    assert effects.landed == []
    assert result.outcomes[0].status != "landed"
    assert "land = false" in result.outcomes[0].reason


def test_c1_2238_b_land_false_lands_nothing_even_through_the_pinned_door(tmp_path):
    """The one function that calls the merge refuses on its own, not only the hold before it."""
    from axiom.extensions.builtins.release.lander import actor

    effects = _Effects()
    config = parse_config("land = false\n" + RAW)
    guarded = actor.guard_landing(effects, config)
    with pytest.raises(LanderRefused, match="land = false"):
        guarded.land(7, HEAD, "merge")
    assert effects.landed == []


# --- D / E: the first line ---------------------------------------------------------------


def _ctx(tmp_path):
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))


def _report(tmp_path, capsys, toml: str, monkeypatch, *, env=None, fmt="text"):
    for key in ("AXIOM_LANDER_KILL", "GITHUB_EVENT_NAME", "AXIOM_RELEASE_LOCK"):
        monkeypatch.delenv(key, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    path = tmp_path / "lander.toml"
    path.write_text(toml, encoding="utf-8")
    out = tmp_path / "report.json"
    result = lander_skill.land(
        {
            "repo": REPO, "config": str(path), "format": fmt, "json_out": str(out), "no_ci_lint": True,
            "pulls": [{"number": 7, "title": "t", "head_sha": HEAD, "head_ref": "b", "files": ["src/a.py"], "checks": "passing", "mergeable": True}],
        },
        _ctx(tmp_path),
    )
    assert result.ok
    return capsys.readouterr().out, json.loads(out.read_text(encoding="utf-8"))


def test_c1_2238_d_the_first_line_says_the_run_can_merge_and_why(tmp_path, capsys, monkeypatch):
    text, data = _report(tmp_path, capsys, RAW, monkeypatch)
    first = text.splitlines()[0]
    assert first.startswith("This run can merge")
    for part in ("mode = warn", "land = true", "kill switch AXIOM_LANDER_KILL off", "no release lock"):
        assert part in first
    assert data["posture"]["can_merge"] is True and data["posture"]["line"] == first


def test_c1_2238_d_land_false_says_it_cannot_merge_and_names_the_key(tmp_path, capsys, monkeypatch):
    text, data = _report(tmp_path, capsys, "land = false\n" + RAW, monkeypatch)
    first = text.splitlines()[0]
    assert first.startswith("This run cannot merge") and "land = false" in first
    assert data["posture"]["can_merge"] is False


def test_c1_2238_d_the_kill_switch_is_named(tmp_path, capsys, monkeypatch):
    text, data = _report(tmp_path, capsys, RAW, monkeypatch, env={"AXIOM_LANDER_KILL": "1"})
    first = text.splitlines()[0]
    assert first.startswith("This run cannot merge") and "AXIOM_LANDER_KILL=1" in first


def test_c1_2238_d_a_pull_request_run_only_reports(tmp_path, capsys, monkeypatch):
    text, _ = _report(tmp_path, capsys, RAW, monkeypatch, env={"GITHUB_EVENT_NAME": "pull_request"})
    first = text.splitlines()[0]
    assert first.startswith("This run cannot merge") and "pull-request run only reports" in first


def test_c1_2238_d_a_config_that_does_not_allow_merge_cannot_merge_without_approval(tmp_path, capsys, monkeypatch):
    text, _ = _report(tmp_path, capsys, 'mode = "warn"\n[raci]\napprovers = ["@ada:local"]\n', monkeypatch)
    first = text.splitlines()[0]
    assert first.startswith("This run cannot merge") and "raci.auto_allowed" in first


def test_c1_2238_e_the_summary_opens_with_the_same_line():
    line = "This run cannot merge: land = false (mode = warn, kill switch AXIOM_LANDER_KILL off, no release lock)."
    summary = verdict.summary_markdown({"posture": {"can_merge": False, "line": line}, "ready": []})
    assert summary.splitlines()[0] == line


def test_c1_2238_e_an_old_report_without_the_key_still_renders():
    assert verdict.summary_markdown({"ready": []}).splitlines()[0] == "## RIVET lander"


# --- F: this repository's own config ------------------------------------------------------


def test_c1_2238_f_this_repository_states_land_and_explains_it():
    text = (ROOT / ".axiom" / "release-lander.toml").read_text(encoding="utf-8")
    config = parse_config(text)
    assert config.land is True
    assert "\nland = true" in text
    assert "report posture" in text
