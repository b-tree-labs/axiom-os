# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RIVET C1: a missing app identity is said, not swallowed (#2341).

From 2026-11-22 the RIVET app token could not be minted. Auto-revert crashed quietly (main
stayed red for hours with no incident) and the lander ran without its identity with a green
step, because the token step had ``continue-on-error: true`` and nothing read its outcome.
The key has since been replaced; this is the code half: say so, and never go quiet again.

Acceptance ids:

  C1-2341-A  The lander's run summary says "running without the RIVET identity: <reason>" in
             its first lines when the token step failed, and says nothing when it succeeded.
  C1-2341-B  The reason names what is wrong when it can be told (no app id, no key) and where
             to read the forge's own error when it cannot.
  C1-2341-C  In the auto-revert workflow a failed mint runs an incident step that files or
             updates ONE issue, using only the job token, and the run still ends red.
  C1-2341-D  The incident is one issue: found by its exact title, commented on when it exists.
  C1-2341-E  The lander workflow reads the token step's outcome and hands it to the summary,
             and the step that mints the token comes before the step that reports it.
  C1-2341-F  The secret is not touched and never printed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[6]
SCRIPTS = ROOT / ".github" / "scripts"
LANDER_YML = ROOT / ".github" / "workflows" / "rivet-lander.yml"
REVERT_YML = ROOT / ".github" / "workflows" / "loop-auto-revert.yml"
POSTURE = "This run can merge: mode = warn, land = true, kill switch AXIOM_LANDER_KILL off, no release lock."


def _load(name: str):
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(ROOT / "actions" / "rivet-land" / "scripts"))
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        del sys.path[:2]


def _steps(path: Path, job: str) -> list[dict]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"][job]["steps"]


def _index(steps: list[dict], needle: str) -> int:
    return next(i for i, step in enumerate(steps) if needle in str(step.get("name", "")) or needle in str(step.get("uses", "")))


# --- A / B: the notice -------------------------------------------------------------------


def test_c1_2341_a_a_failed_mint_is_said_in_plain_words():
    identity = _load("rivet_identity")
    said = identity.notice({"RIVET_APP_TOKEN_OUTCOME": "failure", "RIVET_APP_ID": "123", "RIVET_APP_KEY_SET": "true"})
    assert said.startswith("running without the RIVET identity: ")
    assert "CI that never ran cannot be started" in said and "nothing can be reverted" in said


def test_c1_2341_a_a_good_mint_or_a_step_that_did_not_run_says_nothing():
    identity = _load("rivet_identity")
    for outcome in ("success", "skipped", ""):
        assert identity.notice({"RIVET_APP_TOKEN_OUTCOME": outcome}) == ""


def test_c1_2341_b_the_reason_names_a_missing_app_id_or_key():
    identity = _load("rivet_identity")
    no_id = identity.notice({"RIVET_APP_TOKEN_OUTCOME": "failure", "RIVET_APP_ID": "", "RIVET_APP_KEY_SET": "true"})
    no_key = identity.notice({"RIVET_APP_TOKEN_OUTCOME": "failure", "RIVET_APP_ID": "1", "RIVET_APP_KEY_SET": "false"})
    other = identity.notice({"RIVET_APP_TOKEN_OUTCOME": "failure", "RIVET_APP_ID": "1", "RIVET_APP_KEY_SET": "true"})
    assert "RIVET_APP_ID is not set" in no_id
    assert "RIVET_APP_PRIVATE_KEY is not set" in no_key
    assert "RIVET app token" in other and "log" in other


def test_c1_2341_a_the_run_summary_leads_with_the_posture_then_the_missing_identity(tmp_path, monkeypatch, capsys):
    warn = _load("rivet_warn_comment")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"posture": {"can_merge": True, "line": POSTURE}, "ready": []}), encoding="utf-8")
    monkeypatch.setenv("RIVET_REPORT_JSON", str(report))
    monkeypatch.setenv("RIVET_REPORT_PATH", str(tmp_path / "none.txt"))
    monkeypatch.setenv("RIVET_APP_TOKEN_OUTCOME", "failure")
    monkeypatch.setenv("RIVET_APP_ID", "1")
    monkeypatch.setenv("RIVET_APP_KEY_SET", "true")
    assert warn.main(["--summary"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == POSTURE
    assert lines[1].startswith("running without the RIVET identity: ")
    assert "## RIVET lander" in lines[:5]


def test_c1_2341_a_with_the_identity_the_summary_has_no_such_line(tmp_path, monkeypatch, capsys):
    warn = _load("rivet_warn_comment")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"posture": {"can_merge": True, "line": POSTURE}, "ready": []}), encoding="utf-8")
    monkeypatch.setenv("RIVET_REPORT_JSON", str(report))
    monkeypatch.setenv("RIVET_REPORT_PATH", str(tmp_path / "none.txt"))
    monkeypatch.setenv("RIVET_APP_TOKEN_OUTCOME", "success")
    warn.main(["--summary"])
    assert "without the RIVET identity" not in capsys.readouterr().out


# --- C / D: the incident --------------------------------------------------------------------


class _Gh:
    """A recording ``gh``: serves the open-issue search and records every write."""

    def __init__(self, open_issues=()) -> None:
        self.open_issues = list(open_issues)
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str:
        self.calls.append(list(args))
        if args[:2] == ["issue", "list"]:
            return json.dumps(self.open_issues)
        return ""

    def writes(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["issue", verb]]


def test_c1_2341_d_the_first_failure_opens_one_incident_naming_the_cause_and_the_run():
    identity = _load("rivet_identity")
    gh = _Gh()
    identity.file_incident("o/r", "A JSON web token could not be decoded", "https://example.test/runs/9", gh=gh)
    created = gh.writes("create")
    assert len(created) == 1 and gh.writes("comment") == []
    call = created[0]
    assert call[call.index("--title") + 1] == identity.TITLE
    body = call[call.index("--body") + 1]
    assert "could not be decoded" in body and "https://example.test/runs/9" in body
    assert "revert" in body and "RIVET_APP_PRIVATE_KEY" in body


def test_c1_2341_d_the_next_failure_comments_on_the_same_incident():
    identity = _load("rivet_identity")
    gh = _Gh(open_issues=[{"number": 41, "title": identity.TITLE}])
    identity.file_incident("o/r", "still failing", "https://example.test/runs/10", gh=gh)
    assert gh.writes("create") == []
    commented = gh.writes("comment")
    assert len(commented) == 1 and commented[0][2] == "41"


def test_c1_2341_d_an_issue_with_a_similar_title_is_not_the_incident():
    identity = _load("rivet_identity")
    gh = _Gh(open_issues=[{"number": 7, "title": identity.TITLE + " and more"}])
    identity.file_incident("o/r", "x", "u", gh=gh)
    assert len(gh.writes("create")) == 1


def test_c1_2341_f_a_token_in_the_reason_is_not_printed(monkeypatch):
    identity = _load("rivet_identity")
    monkeypatch.setenv("GH_TOKEN", "ghs_supersecretvalue")
    gh = _Gh()
    identity.file_incident("o/r", "denied for ghs_supersecretvalue", "u", gh=gh)
    assert "ghs_supersecretvalue" not in json.dumps(gh.calls)


# --- C / E: the workflows ---------------------------------------------------------------------


def test_c1_2341_c_a_failed_mint_in_auto_revert_files_the_incident_and_the_run_stays_red():
    steps = _steps(REVERT_YML, "auto-revert")
    mint, incident = _index(steps, "RIVET app token"), _index(steps, "watcher is down")
    assert incident == mint + 1, "the incident step follows the mint immediately"
    step = steps[incident]
    assert "steps.rivet.outcome == 'failure'" in step["if"] and "failure()" in step["if"]
    assert "rivet_identity.py" in step["run"] and "incident" in step["run"]
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}", "the incident needs no app key"
    assert "continue-on-error" not in steps[mint], "the mint failure must fail the run, not be swallowed"
    assert "checkout" in str(steps[incident + 1].get("uses", "")), "nothing after it depends on the identity"


def test_c1_2341_c_the_incident_step_does_not_need_a_checkout():
    """The mint fails before checkout, so the script is fetched from the workflow's own commit."""
    step = _steps(REVERT_YML, "auto-revert")[1]
    assert "contents/.github/scripts/rivet_identity.py" in step["run"] and "gh api" in step["run"]


def test_c1_2341_e_the_lander_hands_the_token_outcome_to_the_summary_and_mints_first():
    steps = _steps(LANDER_YML, "land")
    mint, summary = _index(steps, "RIVET app token"), _index(steps, "Summarise who must act")
    assert mint < summary
    env = steps[summary]["env"]
    assert env["RIVET_APP_TOKEN_OUTCOME"] == "${{ steps.rivet.outcome }}"
    assert "RIVET_APP_ID" in env and "RIVET_APP_KEY_SET" in env
    assert steps[mint].get("continue-on-error") is True, "landing without the identity stays possible, and is now said"


def test_c1_2341_e_the_lander_report_step_sees_the_kill_switch_and_the_lock():
    steps = _steps(LANDER_YML, "land")
    env = steps[_index(steps, "Lander report")]["env"]
    assert "AXIOM_LANDER_KILL" in env and "AXIOM_RELEASE_LOCK" in env


def test_c1_2341_f_no_workflow_writes_the_secret():
    for path in (LANDER_YML, REVERT_YML):
        text = path.read_text(encoding="utf-8")
        assert "gh secret set" not in text
