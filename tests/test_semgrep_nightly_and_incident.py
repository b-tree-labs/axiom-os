# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Semgrep: a nightly whole-tree scan, one incident for a red main, secrets on docs-only pull requests.

Acceptance:
  NS-1 The trigger set is exactly push (main), pull_request (main), one nightly schedule at a quiet
       minute, and workflow_dispatch; no `paths` filter on any of them.
  NS-2 Schedule and dispatch never run the pull-request target script and scan the whole tree with
       the same image, packs and flags as main's push.
  IN-1 A red push, nightly or dispatch on main files or updates ONE incident that names the rule
       ids and files (read from the recorded results JSON) and links the run.
  IN-2 Only the incident job holds `issues: write`; the scan job is read-only; no workflow other
       than the incident job reverts, and loop-auto-revert does not listen to Semgrep.
  IN-3 The stale-incident rule closes that incident when the newest completed Semgrep run on main is
       green and later than its newest occurrence, and keeps it otherwise.
  DO-1 A docs-only pull request runs the secrets pack alone over its changed text files, and the
       workflow no longer path-filters pull requests. A mixed pull request is unchanged.
Recorded with read-only `gh api` (tests/semgrep_fixtures): a real failing run's results JSON, a real
green main run's results, the real file lists of a docs-only and a mixed pull request, and the
Semgrep runs on main. Synthetic pieces are marked.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
from pathlib import Path

import yaml

from axiom.extensions.builtins.release.ci_health import stale_incidents as SI
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "semgrep_fixtures"
WORKFLOW = ROOT / ".github" / "workflows" / "semgrep.yml"
TARGETS = ROOT / ".github" / "scripts" / "semgrep_pr_targets.sh"
INCIDENT = ROOT / ".github" / "scripts" / "semgrep_incident.py"


def _wf() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on() -> dict:
    data = _wf()
    return data.get("on", data.get(True))


def _incident_module():
    spec = importlib.util.spec_from_file_location("semgrep_incident", INCIDENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- NS: the trigger set and the nightly ----------------------------------------------------------------


def test_ns1_the_trigger_set_is_exactly_what_was_approved():
    on = _on()
    assert set(on) == {"pull_request", "push", "schedule", "workflow_dispatch"}
    assert on["push"]["branches"] == ["main"] and on["pull_request"]["branches"] == ["main"]
    for trigger in ("push", "pull_request"):
        assert "paths" not in on[trigger] and "paths-ignore" not in on[trigger]
    (entry,) = on["schedule"]
    minute, hour, *rest = entry["cron"].split()
    assert rest == ["*", "*", "*"] and minute.isdigit() and hour.isdigit()
    assert minute not in {"0", "30"}


def test_ns2_the_pull_request_script_runs_only_for_pull_requests():
    steps = _wf()["jobs"]["semgrep"]["steps"]
    pick = next(s for s in steps if s.get("id") == "targets")
    assert pick["if"] == "github.event_name == 'pull_request'"
    scan = next(s for s in steps if s.get("name") == "Scan")
    # With the pick step skipped its output is empty, and the scan falls back to the whole tree.
    assert scan["env"]["MODE"].endswith("|| 'all' }}")


def test_ns2_the_whole_tree_scan_keeps_its_image_packs_and_flags():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "semgrep/semgrep:1.182.0" in text
    for pack in ("p/python", "p/javascript", "p/typescript", "p/terraform", "p/secrets", "p/github-actions"):
        assert f"--config {pack}" in text
    assert "--severity=ERROR" in text and "--error" in text
    # The code packs are skipped only in secrets mode.
    assert 'if [ "${MODE}" != "secrets" ]' in text


# --- IN: the incident --------------------------------------------------------------------------------------


def test_in2_only_the_incident_job_may_write_issues_and_the_scan_job_is_read_only():
    wf = _wf()
    assert "issues" not in wf["permissions"]
    assert wf["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert "permissions" not in wf["jobs"]["semgrep"]
    assert wf["jobs"]["incident"]["permissions"] == {"contents": "read", "issues": "write"}
    assert wf["jobs"]["incident"]["needs"] == ["semgrep"]


def test_in2_the_incident_runs_for_a_failed_scan_on_main_only():
    cond = " ".join(_wf()["jobs"]["incident"]["if"].split())
    assert "needs.semgrep.result == 'failure'" in cond and "github.ref == 'refs/heads/main'" in cond
    assert "pull_request" not in cond


def test_in2_semgrep_never_reaches_a_revert():
    revert = yaml.safe_load((ROOT / ".github/workflows/loop-auto-revert.yml").read_text(encoding="utf-8"))
    on = revert.get("on", revert.get(True))
    assert on["workflow_run"]["workflows"] == ["CI", "Loop guards"]
    text = WORKFLOW.read_text(encoding="utf-8") + INCIDENT.read_text(encoding="utf-8")
    for word in ("git revert", "git push", "gh pr", "rerun", "gh run"):
        assert word not in text, word


class FakeGh:
    """A stand-in gh on PATH. Records each call and answers `issue list` from a file."""

    def __init__(self, tmp: Path, issues: list[dict]) -> None:
        self.log = tmp / "calls.jsonl"
        self.log.write_text("")
        (tmp / "issues.json").write_text(json.dumps(issues))
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        gh = bin_dir / "gh"
        gh.write_text(
            "#!/usr/bin/env python3\nimport json,sys,os\n"
            f"open({str(self.log)!r},'a').write(json.dumps(sys.argv[1:])+'\\n')\n"
            f"if sys.argv[1:3]==['issue','list']: print(open({str(tmp / 'issues.json')!r}).read())\n"
            "elif sys.argv[1:3]==['issue','create']: print('https://example.test/issues/1')\n"
        )
        gh.chmod(0o755)
        self.bin = bin_dir

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]


def _run_incident(results: str | None, issues: list[dict], event: str = "push") -> tuple[list[list[str]], int]:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        gh = FakeGh(tmp, issues)
        env = {
            "PATH": f"{gh.bin}:{os.environ['PATH']}",
            "REPO": "octo/widgets",
            "RUN_URL": "https://example.test/runs/99",
            "SHA": "0123456789abcdef0123456789abcdef01234567",
            "EVENT_NAME": event,
            "RESULTS": results or str(tmp / "missing.json"),
        }
        done = subprocess.run(["python3", "-I", str(INCIDENT)], env=env, capture_output=True, text=True, check=False)
        return gh.calls(), done.returncode


RED = str(FIX / "red_run_45360382305.json")


def test_in1_a_red_scan_opens_one_incident_naming_the_rule_ids_and_files_and_the_run():
    calls, rc = _run_incident(RED, [])
    assert rc == 0
    (create,) = [c for c in calls if c[:2] == ["issue", "create"]]
    body = create[create.index("--body") + 1]
    data = json.loads(Path(RED).read_text())
    assert len(data["results"]) == 4
    for row in data["results"]:
        assert row["check_id"] in body and row["path"] in body
    assert "https://example.test/runs/99" in body and "0123456" in body
    assert "<!-- ci-incident:branch=semgrep -->" in body
    assert create[create.index("--title") + 1] == "🔴 CI incident on `semgrep` (Semgrep finding on main)"
    assert [create[i + 1] for i, a in enumerate(create) if a == "--label"] == ["ci-failure", "priority: high"]
    assert "Nothing was reverted" in body


def test_in1_a_second_red_run_appends_to_the_same_incident_and_opens_nothing():
    open_issue = [{"number": 77, "body": "<!-- ci-incident:branch=semgrep -->\nold"}]
    calls, rc = _run_incident(RED, open_issue, event="schedule")
    assert rc == 0
    assert not [c for c in calls if c[:2] == ["issue", "create"]]
    (comment,) = [c for c in calls if c[:2] == ["issue", "comment"]]
    assert comment[2] == "77" and "nightly scan" in comment[comment.index("--body") + 1]


def test_in1_another_incident_is_not_adopted():
    # Synthetic: an open CI incident for main carries a different marker.
    calls, _ = _run_incident(RED, [{"number": 5, "body": "<!-- ci-incident:branch=main -->"}])
    assert [c for c in calls if c[:2] == ["issue", "create"]]


def test_in1_unreadable_results_still_file_an_incident_and_say_so():
    calls, rc = _run_incident(None, [])
    assert rc == 0
    (create,) = [c for c in calls if c[:2] == ["issue", "create"]]
    assert "could not be read" in create[create.index("--body") + 1]


def test_in1_the_green_recorded_results_have_no_findings_to_name():
    findings, _, readable = _incident_module().read_findings(str(FIX / "green_main_run_45761352439.json"))
    assert readable and findings == []


# --- IN-3: the stale rule ------------------------------------------------------------------------------------


CONFIG = CIHealthConfig()
REPO = "octo/widgets"


class Forge:
    def __init__(self, runs: list[dict], comments: list[dict] | None = None) -> None:
        self.runs = runs
        self._comments = comments or []

    def open_issues(self, repo, label):
        return [self.issue]

    def issue_comments(self, repo, number):
        return self._comments

    def workflow_runs(self, repo, workflow, branch, *, per_page=30):
        assert workflow == "semgrep.yml" and branch == "main"
        return self.runs

    issue = {
        "number": 77,
        "title": "🔴 CI incident on `semgrep` (Semgrep finding on main)",
        "body": "<!-- ci-incident:branch=semgrep -->\n| **At (UTC)** | 2026-11-26T04:00:00Z |",
        "user": {"login": "github-actions[bot]", "type": "Bot"},
        "labels": [{"name": "ci-failure"}],
        "state": "open",
        "created_at": "2026-11-26T04:00:00Z",
    }


def _recorded_runs() -> list[dict]:
    return json.loads((FIX / "semgrep_main_runs.json").read_text())


def _verdict(forge: Forge):
    (finding,) = SI.scan(forge, CONFIG, REPO)
    return finding


def test_in3_a_later_green_run_closes_it_naming_the_run():
    # The incident is dated 04:00Z; the recorded runs after it are green push runs.
    finding = _verdict(Forge(_recorded_runs()))
    assert finding.verdict == SI.CLOSE, finding.reason
    newest = max((r for r in _recorded_runs() if r["conclusion"] == "success"), key=lambda r: r["created_at"])
    assert str(newest["id"]) in finding.comment and "semgrep.yml" in finding.comment


def test_in3_no_later_green_run_keeps_it():
    late = dict(Forge.issue, body="<!-- ci-incident:branch=semgrep -->\n| **At (UTC)** | 2099-01-01T00:00:00Z |")
    forge = Forge(_recorded_runs())
    forge.issue = late
    assert _verdict(forge).verdict == SI.KEEP


def test_in3_a_newer_red_run_keeps_it():
    # Synthetic: the newest completed run is red.
    runs = [dict(_recorded_runs()[0], id=1, status="completed", conclusion="failure", created_at="2026-11-27T00:00:00Z")] + _recorded_runs()
    assert _verdict(Forge(runs)).verdict == SI.KEEP


def test_in3_a_green_nightly_closes_it_too():
    # Synthetic: a scheduled run is the newest green.
    runs = [dict(_recorded_runs()[0], id=2, status="completed", conclusion="success", event="schedule", created_at="2026-11-27T06:23:00Z")] + _recorded_runs()
    finding = _verdict(Forge(runs))
    assert finding.verdict == SI.CLOSE and "2" in finding.later_run


def test_in3_in_progress_and_cancelled_runs_decide_nothing():
    runs = [
        dict(_recorded_runs()[0], id=3, status="in_progress", conclusion=None, created_at="2026-11-27T00:00:00Z"),
        dict(_recorded_runs()[0], id=4, conclusion="cancelled", created_at="2026-11-27T00:01:00Z"),
    ]
    forge = Forge(runs)
    forge.issue = dict(Forge.issue, body="<!-- ci-incident:branch=semgrep -->\n| **At (UTC)** | 2099-01-01T00:00:00Z |")
    assert _verdict(forge).verdict == SI.KEEP


def test_in3_the_incident_the_script_files_is_one_the_rule_reads():
    mod = _incident_module()
    occ = mod.occurrence([], [], False, run_url="u", sha="a" * 40, event="push", now="2026-11-26T04:00:00Z")
    issue = dict(Forge.issue, title=mod.TITLE, body=mod.issue_body(occ))
    forge = Forge(_recorded_runs())
    forge.issue = issue
    assert _verdict(forge).key == "semgrep"
    assert _verdict(forge).verdict == SI.CLOSE


# --- DO: docs-only pull requests -----------------------------------------------------------------------------


def _targets(listing: str, event: str = "pull_request") -> dict[str, str]:
    with tempfile.TemporaryDirectory() as t:
        root = Path(t)
        (root / "bin").mkdir()
        gh = root / "bin" / "gh"
        gh.write_text('#!/bin/bash\nprintf "%s" "$FAKE_FILES"\n')
        gh.chmod(0o755)
        work = root / "work"
        work.mkdir()
        for line in listing.splitlines():
            status, name = line.split("\t")
            if status != "removed":
                (work / name).parent.mkdir(parents=True, exist_ok=True)
                (work / name).write_text("x\n")
        temp = root / "tmp"
        temp.mkdir()
        out = root / "out"
        out.write_text("")
        env = {
            "PATH": f"{root / 'bin'}:{os.environ['PATH']}",
            "GH_TOKEN": "t", "REPO": "o/r", "PR_NUMBER": "7", "EVENT_NAME": event,
            "GITHUB_OUTPUT": str(out), "RUNNER_TEMP": str(temp), "FAKE_FILES": listing,
        }
        subprocess.run(["/bin/bash", str(TARGETS)], env=env, cwd=work, check=True, capture_output=True)
        result = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
        listed = temp / "semgrep-targets.txt"
        result["targets"] = listed.read_text() if listed.exists() else ""
        return result


def test_do1_the_recorded_docs_only_pull_request_runs_the_secrets_pack_over_its_markdown():
    listing = (FIX / "docs_only_pr_2438_files.tsv").read_text()
    out = _targets(listing)
    assert out["mode"] == "secrets"
    assert out["targets"].splitlines() == [line.split("\t")[1] for line in listing.splitlines()]


def test_do1_the_recorded_mixed_pull_request_is_unchanged():
    out = _targets((FIX / "mixed_pr_2495_files.tsv").read_text())
    assert out["mode"] == "files"


def test_do1_images_are_not_scanned_and_an_images_only_pull_request_has_nothing_to_scan():
    assert _targets("added\tdocs/a.md\nadded\tdocs/x.png\n")["targets"].splitlines() == ["docs/a.md"]
    assert _targets("added\tdocs/x.png\nmodified\tdocs/y.pdf\n")["mode"] == "none"


def test_do1_any_non_doc_file_means_the_code_packs_run():
    for extra in ("src/a.py", "notes.txt", "Makefile", "data.json", ".github/workflows/x.yml"):
        assert _targets(f"modified\tdocs/a.md\nmodified\t{extra}\n")["mode"] == "files", extra


def test_do1_a_push_is_not_narrowed():
    assert _targets("modified\tdocs/a.md\n", event="push")["mode"] == "all"


def test_do1_the_secrets_only_scan_uses_no_code_pack():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "packs=(--config p/secrets)" in text


def test_semgrep_is_not_a_required_check():
    assert not ({"Semgrep", "Semgrep incident"} & {"Lint", "Unit Tests (3.13)", "Guards"})
