# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The real host and checkout behind the lander's base-drift path.

``GhDriftHost`` reads runs, workflow files and the base branch's results
through ``gh``. ``LocalWorkspace`` changes a checkout with ``git``: it
stages by name, commits, and pushes with a plain ref update, so the
repository's pre-push hook runs and nothing is forced. Both take a
``run`` callable so tests can see every command without running one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from axiom.extensions.builtins.release.lander.actor import push_command
from axiom.extensions.builtins.release.lander.loop_guard import LanderRefused

Runner = Callable[..., tuple[int, str]]


def _subprocess(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    done = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=1800, check=False, env=os.environ.copy()
    )
    return done.returncode, (done.stdout or "") + ("" if done.returncode == 0 else done.stderr or "")


class GhDriftHost:
    def __init__(self, repo: str, *, run: Runner | None = None) -> None:
        self.repo = repo
        self._run = run or _subprocess

    def _ok(self, args: list[str]) -> str:
        code, out = self._run(args)
        if code != 0:
            raise LanderRefused(out.strip()[:300] or f"{args[:3]} failed")
        return out

    def _json(self, args: list[str]) -> object:
        try:
            return json.loads(self._ok(args) or "null")
        except json.JSONDecodeError:
            return None

    def pr_files(self, number: int) -> list[str]:
        data = self._json(["gh", "pr", "view", str(number), "--repo", self.repo, "--json", "files"])
        files = (data or {}).get("files") if isinstance(data, dict) else None
        return [str(item.get("path")) for item in files or [] if isinstance(item, dict)]

    def failing_run(self, number: int, check: str) -> dict | None:
        # ``gh pr checks`` exits non-zero when a check failed; its output is still the answer.
        _, raw = self._run(
            ["gh", "pr", "checks", str(number), "--repo", self.repo, "--json", "name,state,link"]
        )
        try:
            rows = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return None
        for row in rows if isinstance(rows, list) else []:
            if str(row.get("name")) != check or str(row.get("state")).upper() not in {"FAILURE", "ERROR"}:
                continue
            found = re.search(r"/actions/runs/(\d+)(?:/job/(\d+))?", str(row.get("link") or ""))
            if not found:
                return None
            run_id, job_id = found.group(1), found.group(2)
            run = self._json(["gh", "api", f"repos/{self.repo}/actions/runs/{run_id}"]) or {}
            job = check
            if job_id:
                detail = self._json(["gh", "api", f"repos/{self.repo}/actions/jobs/{job_id}"]) or {}
                job = str(detail.get("name") or check) if isinstance(detail, dict) else check
            log = self._ok(["gh", "run", "view", run_id, "--log-failed", "--repo", self.repo])
            return {
                "log": log,
                "workflow_path": str(run.get("path") or "") if isinstance(run, dict) else "",
                "job": job,
                "run_id": run_id,
            }
        return None

    def file_at(self, ref: str, path: str) -> str:
        code, out = self._run(
            [
                "gh",
                "api",
                "-H",
                "Accept: application/vnd.github.raw",
                f"repos/{self.repo}/contents/{path}?ref={ref}",
            ]
        )
        return out if code == 0 else ""

    def base_check(self, base: str, workflow_path: str, job: str) -> dict:
        name = Path(workflow_path).name
        runs = self._json(
            [
                "gh",
                "api",
                f"repos/{self.repo}/actions/workflows/{name}/runs?branch={base}&status=completed&per_page=1",
            ]
        )
        items = (runs or {}).get("workflow_runs") if isinstance(runs, dict) else None
        if not items:
            return {}
        run_id = items[0].get("id")
        jobs = self._json(["gh", "api", f"repos/{self.repo}/actions/runs/{run_id}/jobs"]) or {}
        for item in (jobs.get("jobs") if isinstance(jobs, dict) else None) or []:
            if str(item.get("name")) == job:
                return {"result": str(item.get("conclusion") or ""), "at": str(item.get("completed_at") or "")}
        return {
            "result": str(items[0].get("conclusion") or ""),
            "at": str(items[0].get("updated_at") or ""),
        }

    def branch_pushable(self, number: int) -> bool:
        data = self._json(
            [
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                self.repo,
                "--json",
                "isCrossRepository,maintainerCanModify",
            ]
        )
        if not isinstance(data, dict):
            return False
        return not data.get("isCrossRepository") or bool(data.get("maintainerCanModify"))

    def comment(self, number: int, body: str) -> None:
        self._ok(["gh", "pr", "comment", str(number), "--repo", self.repo, "--body", body])

    def pr_state(self, number: int) -> dict:
        data = self._json(["gh", "pr", "view", str(number), "--repo", self.repo, "--json", "state"])
        return {"state": str((data or {}).get("state") or "").lower()} if isinstance(data, dict) else {}

    def rerun(self, run_id: str) -> None:
        self._ok(["gh", "run", "rerun", str(run_id), "--failed", "--repo", self.repo])

    def open_pr(self, *, base: str, head: str, title: str, body: str, auto_merge: bool) -> int:
        url = self._ok(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                self.repo,
                "--base",
                base,
                "--head",
                head,
                "--title",
                title,
                "--body",
                body,
            ]
        ).strip()
        found = re.search(r"/pull/(\d+)", url)
        if not found:
            raise LanderRefused(f"could not read the repair pull request from {url[:120]!r}")
        number = int(found.group(1))
        if auto_merge:
            self._ok(["gh", "pr", "merge", str(number), "--repo", self.repo, "--auto", "--squash"])
        return number


class LocalWorkspace:
    """A checkout of the repository the lander acts on.

    :meth:`for_repo` keeps RIVET's own clone under the state directory, so
    a repair never switches the branch of a checkout a person is using. The
    clone is made on first use. When the repository ships its pre-push
    hook at ``scripts/hooks/pre-push`` it is installed in that clone, so a
    push runs the same gate a person's push does.
    """

    def __init__(self, root: Path, *, run: Runner | None = None, repo: str = "") -> None:
        self.root = Path(root)
        self.repo = repo
        self._run = run or _subprocess
        self._touched: list[str] = []

    @classmethod
    def for_repo(cls, state_dir: Path, repo: str, *, run: Runner | None = None) -> LocalWorkspace:
        root = Path(state_dir) / "agents" / "rivet" / "checkouts" / repo.replace("/", "__")
        return cls(root, run=run, repo=repo)

    def _ensure(self) -> None:
        if (self.root / ".git").exists() or not self.repo:
            return
        self.root.parent.mkdir(parents=True, exist_ok=True)
        code, out = self._run(["gh", "repo", "clone", self.repo, str(self.root)])
        if code != 0:
            raise LanderRefused(out.strip()[:300] or "clone failed")
        hook = self.root / "scripts" / "hooks" / "pre-push"
        if hook.is_file():
            target = self.root / ".git" / "hooks" / "pre-push"
            shutil.copyfile(hook, target)
            target.chmod(0o755)

    def _git(self, *args: str) -> str:
        code, out = self._run(["git", *args], cwd=self.root)
        if code != 0:
            raise LanderRefused(out.strip()[:300] or f"git {args[0]} failed")
        return out

    def docker_available(self) -> bool:
        if not shutil.which("docker") and self._run is _subprocess:
            return False
        code, _ = self._run(["docker", "info", "--format", "{{.ServerVersion}}"], cwd=self.root)
        return code == 0

    def tool_versions(self) -> dict:
        found = {}
        for tool, args in (
            ("node", ["node", "--version"]),
            ("npm", ["npm", "--version"]),
            ("python", ["python3", "--version"]),
        ):
            try:
                code, out = self._run(args, cwd=self.root)
            except OSError:
                continue
            if code == 0:
                found[tool] = out.strip()
        return found

    def checkout(self, ref: str) -> None:
        self._ensure()
        self._git("fetch", "origin", ref)
        self._git("checkout", "-B", ref, f"origin/{ref}")

    def branch_from(self, base: str, branch: str) -> None:
        self._ensure()
        self._git("fetch", "origin", base)
        self._git("checkout", "-B", branch, f"origin/{base}")

    def run(self, command: str) -> tuple[int, str]:
        return self._run(["sh", "-c", command], cwd=self.root)

    def numstat(self) -> str:
        out = self._git("diff", "--numstat")
        self._touched = [line.split("\t")[2].strip() for line in out.splitlines() if line.count("\t") >= 2]
        return out

    def discard(self) -> None:
        paths = self._touched or [line.split("\t")[2] for line in self.numstat().splitlines() if line.count("\t") >= 2]
        if paths:
            self._git("checkout", "--", *paths)
        self._touched = []

    def commit(self, message: str) -> str:
        if not self._touched:
            self.numstat()
        if not self._touched:
            raise LanderRefused("nothing to commit")
        self._git("add", "--", *self._touched)
        self._git("commit", "-m", message)
        self._touched = []
        return self._git("rev-parse", "HEAD").strip()

    def push(self, ref: str) -> str:
        self._git(*push_command("origin", f"HEAD:{ref}")[1:])
        return self._git("rev-parse", "HEAD").strip()

    def merge_base(self, base: str) -> str:
        self._git("fetch", "origin", base)
        self._git("merge", "--no-edit", f"origin/{base}")
        return self._git("rev-parse", "HEAD").strip()


__all__ = ["GhDriftHost", "LocalWorkspace"]
