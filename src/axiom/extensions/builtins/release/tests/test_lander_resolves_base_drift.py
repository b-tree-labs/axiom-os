# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The lander resolves base drift end to end, with no person in the loop.

Acceptance rows LD-01 to LD-07. The host API, the container runtime and
git are fakes; everything between them is the real lander.

LD-01 the lander gathers the facts itself (files, run, workflow, base result)
LD-02 a red pull request gets the labelled repair commit, an FYI comment and a ledger entry
LD-03 a second pull request red with the same drift waits; no second repair
LD-04 the carrying pull request lands; the ledger says so; the waiter brings
      the base in through the pre-push path and re-runs
LD-05 no container runtime: local tools that match CI run the repair; a mismatch escalates
LD-06 a failing verify escalates and marks the ledger entry failed
LD-07 the try cap stops a drift that keeps failing
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from axiom.extensions.builtins.release.ci_drift import attribute_failure, choose_execution
from axiom.extensions.builtins.release.lander.actor import run_actor
from axiom.extensions.builtins.release.lander.config import parse_config
from axiom.extensions.builtins.release.lander.drift import DriftWiring
from axiom.extensions.builtins.release.lander.drift_ledger import DriftLedger
from axiom.extensions.builtins.release.lander.model import FailingCheck, OpenPull
from axiom.infra.orchestrator.approval import ApprovalGate
from axiom.infra.orchestrator.approval_store import InMemoryActionStore

EXPO_LOG = """\
##[error]Expo SDK major/minor mismatch (patch drift is ignored):
  expo-web-browser 58.0.7 - expected version: ~58.1.0
##[warning]expo 58.0.22 vs expected ~58.0.26 (patch-only; not failing CI)
Your project may not work correctly until you install the expected versions of the packages.
Found outdated dependencies
##[error]Process completed with exit code 1.
"""

WORKFLOW = """\
name: Mobile
on:
  pull_request:
    paths: ["apps/mobile/**"]
jobs:
  mobile-check:
    name: Mobile checks
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: apps/mobile
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
      - run: npm ci
      - run: npx expo install --check
"""

CODEOWNERS = "*  @example-org/platform\n/apps/mobile/  @mobile-owner\n"
REPAIR_DIFF = "1\t1\tapps/mobile/package.json\n14\t12\tapps/mobile/package-lock.json\n"
WORKFLOW_PATH = ".github/workflows/mobile.yml"


class FakeHost:
    """The forge: pull requests, runs, files, comments, re-runs."""

    def __init__(self) -> None:
        self.prs: dict[int, dict] = {}
        self.comments: dict[int, list[str]] = {}
        self.reruns: list[str] = []
        self.files = {
            WORKFLOW_PATH: WORKFLOW,
            ".github/CODEOWNERS": CODEOWNERS,
        }
        self.base = {"result": "success", "at": "2026-11-10T15:00:00Z"}
        self.opened: list[dict] = []

    def open_pr(self, *, base: str, head: str, title: str, body: str, auto_merge: bool) -> int:
        number = 900 + len(self.opened)
        self.opened.append(
            {"number": number, "base": base, "head": head, "title": title, "body": body, "auto_merge": auto_merge}
        )
        self.prs[number] = {"files": [], "head_ref": head, "state": "open", "pushable": True}
        return number

    def add(self, number: int, head_ref: str, files: list[str], *, pushable: bool = True) -> None:
        self.prs[number] = {"files": files, "head_ref": head_ref, "state": "open", "pushable": pushable}

    def pr_files(self, number: int) -> list[str]:
        return list(self.prs[number]["files"])

    def failing_run(self, number: int, check: str) -> dict | None:
        return {
            "log": EXPO_LOG,
            "workflow_path": WORKFLOW_PATH,
            "job": check,
            "run_id": f"run-{number}",
        }

    def file_at(self, ref: str, path: str) -> str:
        del ref
        return self.files.get(path, "")

    def base_check(self, base: str, workflow_path: str, job: str) -> dict:
        del base, workflow_path, job
        return dict(self.base)

    def branch_pushable(self, number: int) -> bool:
        return bool(self.prs[number]["pushable"])

    def comment(self, number: int, body: str) -> None:
        self.comments.setdefault(number, []).append(body)

    def pr_state(self, number: int) -> dict:
        return {"state": self.prs[number]["state"]}

    def rerun(self, run_id: str) -> None:
        self.reruns.append(run_id)


class FakeWorkspace:
    """A checkout: runs commands, commits, pushes through the hook, merges the base."""

    def __init__(self, *, docker: bool = True, versions: dict | None = None, verify_rc: int = 0) -> None:
        self.docker = docker
        self.versions = versions or {"node": "v22.3.0", "npm": "11.0.0"}
        self.verify_rc = verify_rc
        self.ref = ""
        self.ran: list[str] = []
        self.commits: list[tuple[str, str]] = []
        self.pushes: list[str] = []
        self.merges: list[tuple[str, str]] = []
        self.discarded = 0
        self._dirty = False
        self._sha = 0

    def docker_available(self) -> bool:
        return self.docker

    def tool_versions(self) -> dict:
        return dict(self.versions)

    def checkout(self, ref: str) -> None:
        self.ref = ref

    def branch_from(self, base: str, branch: str) -> None:
        del base
        self.ref = branch

    def run(self, command: str) -> tuple[int, str]:
        self.ran.append(command)
        if "install --check" in command:
            return self.verify_rc, "" if self.verify_rc == 0 else "Found outdated dependencies"
        self._dirty = True
        return 0, ""

    def numstat(self) -> str:
        return REPAIR_DIFF if self._dirty else ""

    def discard(self) -> None:
        self.discarded += 1
        self._dirty = False

    def commit(self, message: str) -> str:
        self._sha += 1
        self.commits.append((self.ref, message))
        self._dirty = False
        return f"sha{self._sha}"

    def push(self, ref: str) -> str:
        self.pushes.append(ref)
        return f"sha{self._sha}"

    def merge_base(self, base: str) -> str:
        self._sha += 1
        self.merges.append((self.ref, base))
        return f"sha{self._sha}"


class NoEffects:
    def head(self, number: int) -> str:
        return f"head-{number}"

    def apply(self, number, fix, head_sha):  # pragma: no cover - never reached
        raise AssertionError("the drift path does not go through apply")

    def rerun(self, number, sha):  # pragma: no cover
        return "failing"

    def land(self, number, sha, method):  # pragma: no cover
        raise AssertionError("nothing lands here")

    def close(self, number, sha):  # pragma: no cover
        pass


def _config(*allowed: str):
    config = parse_config('mode = "warn"\n')
    return replace(config, approvers=("@owner",), auto_allowed=allowed or ("drift_repair",))


def _pull(number: int, head_ref: str, files: tuple[str, ...]) -> OpenPull:
    return OpenPull(
        number=number,
        title="mobile screen",
        head_sha=f"head-{number}",
        head_ref=head_ref,
        files=files,
        checks="failing",
        failing_checks=(FailingCheck("Mobile checks"),),
        failure_log=EXPO_LOG,
    )


def _act(pulls, *, tmp_path: Path, host: FakeHost, workspace: FakeWorkspace, config=None):
    return run_actor(
        pulls,
        repo="example/app",
        config=config or _config(),
        state_dir=tmp_path,
        effects=NoEffects(),
        gate=ApprovalGate(InMemoryActionStore()),
        drift=DriftWiring(host=host, workspace=workspace, actor="rivet-bot"),
    )


def _incident(tmp_path: Path, **workspace_kwargs):
    host = FakeHost()
    host.add(101, "feat/settings", ["apps/mobile/src/screens/Settings.tsx"])
    host.add(102, "feat/profile", ["apps/mobile/src/screens/Profile.tsx"])
    workspace = FakeWorkspace(**workspace_kwargs)
    return host, workspace


# --- LD-01 and LD-02 -----------------------------------------------------------------


def test_ld01_ld02_a_red_pull_request_is_repaired_on_its_own_branch(tmp_path: Path):
    host, workspace = _incident(tmp_path)
    result = _act([_pull(101, "feat/settings", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    outcome = result.outcomes[0]
    assert outcome.status == "repaired", outcome
    assert workspace.commits == [("feat/settings", "rivet: base-drift repair — expo-expected-version")]
    assert workspace.pushes == ["feat/settings"]
    assert any("node:20" in command and "expo install --fix" in command for command in workspace.ran)
    assert any("expo install --check" in command for command in workspace.ran)
    comment = "\n".join(host.comments[101])
    assert "not caused by this pull request" in comment
    assert "FYI @mobile-owner" in comment
    entry = DriftLedger(tmp_path).for_pr(101)
    assert entry is not None
    assert entry["status"] == "pushed"
    assert entry["commit"] == "sha1"
    assert entry["key"].startswith("expo-expected-version:main:apps/mobile")
    assert "expo-web-browser@58.1.0" in entry["key"]


def test_ld01_without_the_opt_in_the_lander_is_unchanged(tmp_path: Path):
    host, workspace = _incident(tmp_path)
    result = _act(
        [_pull(101, "feat/settings", ())],
        tmp_path=tmp_path,
        host=host,
        workspace=workspace,
        config=_config("merge"),
    )
    assert result.outcomes[0].status == "escalated"
    assert workspace.commits == [] and host.comments == {}


# --- LD-03 and LD-04 -----------------------------------------------------------------


def test_ld03_ld04_the_second_pull_request_waits_then_takes_the_base(tmp_path: Path):
    host, workspace = _incident(tmp_path)
    _act([_pull(101, "feat/settings", ())], tmp_path=tmp_path, host=host, workspace=workspace)

    waiting = _act([_pull(102, "feat/profile", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    assert waiting.outcomes[0].status == "waiting"
    assert "101" in waiting.outcomes[0].reason
    assert len(workspace.commits) == 1, "no second repair"
    assert any("not caused by this pull request" in body for body in host.comments[102])

    # A second pass while still waiting does not comment again.
    _act([_pull(102, "feat/profile", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    assert len(host.comments[102]) == 1

    host.prs[101]["state"] = "merged"
    rebased = _act([_pull(102, "feat/profile", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    assert DriftLedger(tmp_path).for_pr(101)["status"] == "landed"
    assert rebased.outcomes[0].status == "rebased"
    assert workspace.merges == [("feat/profile", "main")]
    assert workspace.pushes == ["feat/settings", "feat/profile"]
    assert host.reruns == ["run-102"]
    assert len(workspace.commits) == 1


def test_ld03_a_branch_that_cannot_take_pushes_gets_one_auto_merging_repair_pr(tmp_path: Path):
    host, workspace = _incident(tmp_path)
    host.add(103, "contributor/fork", ["apps/mobile/src/a.tsx"], pushable=False)
    host.add(104, "contributor/other", ["apps/mobile/src/b.tsx"], pushable=False)
    result = _act([_pull(103, "contributor/fork", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    assert result.outcomes[0].status == "repaired"
    assert workspace.pushes == ["rivet/base-drift/expo-expected-version"]
    assert workspace.commits[0][0] == "rivet/base-drift/expo-expected-version"
    assert len(host.opened) == 1 and host.opened[0]["auto_merge"] is True
    assert host.opened[0]["base"] == "main"
    entry = DriftLedger(tmp_path).for_pr(str(host.opened[0]["number"]))
    assert entry is not None and entry["status"] == "pushed"
    again = _act([_pull(104, "contributor/other", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    assert again.outcomes[0].status == "waiting"
    assert len(host.opened) == 1, "at most one repair pull request per drift"


# --- LD-05 -----------------------------------------------------------------------------


def test_ld05_no_docker_and_matching_local_tools_runs_locally(tmp_path: Path):
    host, workspace = _incident(tmp_path, docker=False, versions={"node": "v20.11.1", "npm": "10.2.4"})
    result = _act([_pull(101, "feat/settings", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    assert result.outcomes[0].status == "repaired"
    assert all("docker" not in command for command in workspace.ran)
    assert any(command.startswith("cd apps/mobile && npx expo install --fix") for command in workspace.ran)


def test_ld05_no_docker_and_a_mismatched_npm_escalates(tmp_path: Path):
    host, workspace = _incident(tmp_path, docker=False, versions={"node": "v20.11.1", "npm": "11.0.0"})
    result = _act([_pull(101, "feat/settings", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    outcome = result.outcomes[0]
    assert outcome.status == "escalated"
    assert "npm 11" in outcome.reason and "npm 10" in outcome.reason
    assert workspace.ran == [] and workspace.pushes == []


def test_ld05_choose_execution_is_pure():
    chain = {"node_version": "20", "npm_version": "10", "image": "node:20", "python_version": ""}
    assert choose_execution(chain, docker=True, local={})["mode"] == "container"
    assert choose_execution(chain, docker=False, local={"node": "v20.1.0", "npm": "10.9.0"})["mode"] == "local"
    refused = choose_execution(chain, docker=False, local={"node": "v22.1.0", "npm": "10.9.0"})
    assert refused["mode"] == "escalate" and "Node 22" in refused["reason"]
    py = {"python_version": "3.12", "image": "python:3.12", "node_version": "", "npm_version": ""}
    assert choose_execution(py, docker=False, local={"python": "Python 3.12.4"})["mode"] == "local"
    assert choose_execution(py, docker=False, local={"python": "Python 3.13.0"})["mode"] == "escalate"


# --- LD-06 -----------------------------------------------------------------------------


def test_ld06_a_failing_verify_escalates_and_the_ledger_says_failed(tmp_path: Path):
    host, workspace = _incident(tmp_path, verify_rc=1)
    result = _act([_pull(101, "feat/settings", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    outcome = result.outcomes[0]
    assert outcome.status == "escalated"
    assert "verify failed" in outcome.reason
    assert workspace.pushes == [] and workspace.commits == []
    assert workspace.discarded == 1
    assert DriftLedger(tmp_path).for_pr(101)["status"] == "failed"


# --- LD-07 -----------------------------------------------------------------------------


def test_ld07_the_try_cap_stops_a_drift_that_keeps_failing(tmp_path: Path):
    host, workspace = _incident(tmp_path, verify_rc=1)
    for _ in range(3):
        _act([_pull(101, "feat/settings", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    ran_before = len(workspace.ran)
    capped = _act([_pull(101, "feat/settings", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    assert capped.outcomes[0].status == "escalated"
    assert "try-cap" in capped.outcomes[0].reason or "cap" in capped.outcomes[0].reason
    assert len(workspace.ran) == ran_before, "nothing runs after the cap"


# --- attribution the lander can decide without an upstream time ------------------------


def test_ld01_inputs_that_decide_the_failure_make_an_untouched_pr_base_drift():
    verdict = attribute_failure(
        entry_inputs=("**/package.json", "**/package-lock.json"),
        inputs_complete=True,
        pr_files=["apps/mobile/src/screens/Settings.tsx"],
        check_globs=["apps/mobile/**"],
        base_result="success",
        base_result_at="2026-11-10T15:00:00Z",
    )
    assert verdict["verdict"] == "base_drift"
    assert any("fails the same way" in line for line in verdict["evidence"])


def test_ld01_the_ledger_is_a_json_file_under_rivets_state(tmp_path: Path):
    ledger = DriftLedger(tmp_path)
    ledger.upsert("k:main:.", pr="5", status="verifying")
    assert (tmp_path / "agents" / "rivet" / "drift-repairs.json").is_file()
    again = DriftLedger(tmp_path)
    assert again.get("k:main:.")["status"] == "verifying"
    assert again.get("k:main:.")["created_at"]


# --- LD-08: the real adapters build the right host and git commands ---------------------


class Recorder:
    def __init__(self, replies: dict[str, tuple[int, str]]) -> None:
        self.replies = replies
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], cwd=None) -> tuple[int, str]:
        del cwd
        self.calls.append(list(args))
        joined = " ".join(args)
        for needle, reply in self.replies.items():
            if needle in joined:
                return reply
        return 0, ""


def test_ld08_the_gh_host_reads_the_run_the_workflow_and_the_base():
    import json

    from axiom.extensions.builtins.release.lander.drift_adapters import GhDriftHost

    runner = Recorder(
        {
            "pr checks": (
                1,
                json.dumps(
                    [
                        {"name": "Mobile checks", "state": "FAILURE",
                         "link": "https://github.com/example/app/actions/runs/77/job/88"},
                        {"name": "Lint", "state": "SUCCESS", "link": ""},
                    ]
                ),
            ),
            "api repos/example/app/actions/runs/77": (0, json.dumps({"path": ".github/workflows/mobile.yml"})),
            "run view 77": (0, EXPO_LOG),
            "actions/jobs/88": (0, json.dumps({"name": "Mobile checks"})),
            "workflows/mobile.yml/runs": (0, json.dumps({"workflow_runs": [{"id": 5}]})),
            "actions/runs/5/jobs": (
                0,
                json.dumps({"jobs": [{"name": "Mobile checks", "conclusion": "success",
                                       "completed_at": "2026-11-10T15:00:00Z"}]}),
            ),
        }
    )
    host = GhDriftHost("example/app", run=runner)
    found = host.failing_run(101, "Mobile checks")
    assert found == {
        "log": EXPO_LOG,
        "workflow_path": ".github/workflows/mobile.yml",
        "job": "Mobile checks",
        "run_id": "77",
    }
    assert host.base_check("main", ".github/workflows/mobile.yml", "Mobile checks") == {
        "result": "success",
        "at": "2026-11-10T15:00:00Z",
    }
    host.rerun("77")
    assert ["gh", "run", "rerun", "77", "--failed", "--repo", "example/app"] in runner.calls


def test_ld08_the_local_workspace_pushes_plainly_and_stages_by_name():
    from axiom.extensions.builtins.release.lander.drift_adapters import LocalWorkspace

    runner = Recorder({"diff --numstat": (0, REPAIR_DIFF), "rev-parse HEAD": (0, "abc123\n")})
    workspace = LocalWorkspace(Path("/checkout"), run=runner)
    workspace.checkout("feat/settings")
    workspace.numstat()
    assert workspace.commit("rivet: base-drift repair — expo-expected-version") == "abc123"
    workspace.push("feat/settings")
    joined = [" ".join(call) for call in runner.calls]
    assert "git add -- apps/mobile/package.json apps/mobile/package-lock.json" in joined
    assert "git push origin HEAD:feat/settings" in joined
    assert not any("--no-verify" in call or "--force" in call or " -f" in call for call in joined)
    assert not any("add -A" in call or "add ." in call for call in joined)


def test_ld08_container_availability_is_detected_not_assumed():
    from axiom.extensions.builtins.release.lander.drift_adapters import LocalWorkspace

    down = LocalWorkspace(Path("/checkout"), run=Recorder({"docker info": (1, "Cannot connect")}))
    assert down.docker_available() is False
    up = LocalWorkspace(Path("/checkout"), run=Recorder({"docker info": (0, "27.1.1")}))
    assert up.docker_available() is True


def test_ld06_a_host_error_mid_repair_escalates_that_pull_request_only(tmp_path: Path):
    from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused

    host, workspace = _incident(tmp_path)

    def refuse(ref: str) -> str:
        raise LanderRefused("pre-push hook refused the push")

    workspace.push = refuse  # type: ignore[method-assign]
    result = _act([_pull(101, "feat/settings", ())], tmp_path=tmp_path, host=host, workspace=workspace)
    assert result.outcomes[0].status == "escalated"
    assert "pre-push hook refused" in result.outcomes[0].reason
    assert DriftLedger(tmp_path).for_pr(101)["status"] == "failed", "no other pull request waits on it"
