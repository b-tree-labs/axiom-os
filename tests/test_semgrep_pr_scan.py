# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Semgrep on a pull request scans what the pull request changed; main scans everything.

Acceptance:
  SG-1 Main (push) is unchanged: same image, packs, severity and --error, whole tree.
  SG-2 A pull request scans only the files it adds or changes (the same rules, the same
       flags), because the push scan keeps main free of ERROR findings.
  SG-3 A pull request that changes only docs (markdown, reStructuredText, images, pdf) runs the
       secrets pack alone over its changed text files; there is no trigger path filter. Semgrep is
       not a required check, so nothing here can block a merge.
  SG-4 Doubt scans everything: the file list cannot be read, a listed file is missing from the
       checkout, the list is empty, or it is long.
  SG-5 A pull request that only deletes files has nothing to scan.
  SG-6 Results and the target list are written under the runner temp directory, never at the
       repository root.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "semgrep.yml"
SCRIPT = ROOT / ".github" / "scripts" / "semgrep_pr_targets.sh"
REQUIRED_CHECKS = {"Lint", "Unit Tests (3.13)", "Guards"}

FAKE_GH = """#!/bin/bash
echo "$*" >> "$FAKE_GH_LOG"
if [ "$1" != "api" ]; then echo "unexpected gh call: $*" >&2; exit 99; fi
if [ "${FAKE_RC:-0}" != "0" ]; then echo "gh: HTTP 502" >&2; exit "$FAKE_RC"; fi
printf '%s' "$FAKE_FILES"
"""


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _on() -> dict:
    data = _workflow()
    return data.get("on", data.get(True))


def run_targets(
    files: list[tuple[str, str]] | str,
    *,
    event: str = "pull_request",
    rc: int = 0,
    present: bool = True,
    cap: str | None = None,
    path: str | None = None,
) -> dict[str, str]:
    """Run the real script in a throwaway checkout with a stand-in gh."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
        (bin_dir / "gh").chmod(0o755)
        work = root / "work"
        work.mkdir()
        if isinstance(files, str):
            listing = files
        else:
            listing = "".join(f"{status}\t{name}\n" for status, name in files)
            if present:
                for status, name in files:
                    if status != "removed":
                        target = work / name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text("x = 1\n", encoding="utf-8")
        out = root / "out"
        out.write_text("")
        temp = root / "runner-temp"
        temp.mkdir()
        log = root / "calls"
        log.write_text("")
        env = {
            "PATH": path if path is not None else f"{bin_dir}:{os.environ['PATH']}",
            "GH_TOKEN": "t",
            "REPO": "octo/widgets",
            "PR_NUMBER": "7",
            "EVENT_NAME": event,
            "GITHUB_OUTPUT": str(out),
            "RUNNER_TEMP": str(temp),
            "FAKE_GH_LOG": str(log),
            "FAKE_FILES": listing,
            "FAKE_RC": str(rc),
        }
        if cap is not None:
            env["SEMGREP_MAX_TARGETS"] = cap
        proc = subprocess.run(
            ["/bin/bash", str(SCRIPT)], env=env, cwd=work, capture_output=True, text=True, check=False
        )
        result = dict(
            line.split("=", 1) for line in out.read_text(encoding="utf-8").splitlines() if "=" in line
        )
        listed = temp / "semgrep-targets.txt"
        result["targets"] = listed.read_text(encoding="utf-8") if listed.exists() else ""
        result["_rc"] = str(proc.returncode)
        result["_text"] = proc.stdout + proc.stderr
        result["_calls"] = log.read_text(encoding="utf-8")
        result["_root_files"] = ",".join(sorted(p.name for p in work.iterdir()))
        return result


class TargetsScript(unittest.TestCase):
    def test_sg2_a_pull_request_scans_its_added_and_changed_files(self) -> None:
        out = run_targets([("added", "src/a.py"), ("modified", "src/b.py"), ("renamed", "lib/c.ts")])
        self.assertEqual(out["mode"], "files")
        self.assertEqual(out["targets"].splitlines(), ["src/a.py", "src/b.py", "lib/c.ts"])
        self.assertIn("pulls/7/files", out["_calls"])
        self.assertEqual(out["_rc"], "0")

    def test_sg1_a_push_scans_everything_and_asks_nothing(self) -> None:
        out = run_targets([("added", "src/a.py")], event="push")
        self.assertEqual(out["mode"], "all")
        self.assertEqual(out["_calls"], "")

    def test_sg1_other_events_scan_everything(self) -> None:
        for event in ("workflow_dispatch", "schedule", ""):
            self.assertEqual(run_targets([("added", "a.py")], event=event)["mode"], "all", event)

    def test_sg5_a_deletion_only_pull_request_has_nothing_to_scan(self) -> None:
        out = run_targets([("removed", "src/old.py"), ("removed", "src/older.py")])
        self.assertEqual(out["mode"], "none")

    def test_sg5_removed_files_are_not_targets(self) -> None:
        out = run_targets([("removed", "src/old.py"), ("modified", "src/new.py")])
        self.assertEqual(out["targets"].splitlines(), ["src/new.py"])

    def test_sg4_a_failed_file_listing_scans_everything(self) -> None:
        out = run_targets("", rc=1)
        self.assertEqual(out["mode"], "all")
        self.assertEqual(out["_rc"], "0")

    def test_sg4_an_empty_listing_scans_everything(self) -> None:
        self.assertEqual(run_targets("")["mode"], "all")

    def test_sg4_odd_listings_scan_everything(self) -> None:
        for odd in ("garbage", "added\n", "\t\n", "added\t\n", "null"):
            self.assertEqual(run_targets(odd)["mode"], "all", repr(odd))

    def test_sg4_a_listed_file_missing_from_the_checkout_scans_everything(self) -> None:
        out = run_targets([("added", "src/a.py"), ("modified", "src/b.py")], present=False)
        self.assertEqual(out["mode"], "all")

    def test_sg4_a_long_list_scans_everything(self) -> None:
        files = [("modified", f"src/f{i}.py") for i in range(6)]
        self.assertEqual(run_targets(files, cap="5")["mode"], "all")
        self.assertEqual(run_targets(files, cap="6")["mode"], "files")

    def test_sg4_glob_characters_in_a_name_scan_everything(self) -> None:
        for name in ("src/a[1].py", "src/a*.py", "src/a?.py", "src/{a,b}.py", "src/a\\b.py"):
            self.assertEqual(run_targets([("added", name)])["mode"], "all", name)

    def test_sg4_a_missing_gh_scans_everything(self) -> None:
        self.assertEqual(run_targets([("added", "a.py")], path="/nonexistent")["mode"], "all")

    def test_sg4_a_missing_token_or_pull_request_number_scans_everything(self) -> None:
        for name in ("GH_TOKEN", "PR_NUMBER"):
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp) / "o"
                out.write_text("")
                env = {
                    "PATH": os.environ["PATH"], "REPO": "o/r", "EVENT_NAME": "pull_request",
                    "GH_TOKEN": "" if name == "GH_TOKEN" else "t",
                    "PR_NUMBER": "" if name == "PR_NUMBER" else "7",
                    "GITHUB_OUTPUT": str(out), "RUNNER_TEMP": tmp,
                }
                subprocess.run(["/bin/bash", str(SCRIPT)], env=env, cwd=tmp, capture_output=True, check=False)
                self.assertIn("mode=all", out.read_text(), name)

    def test_sg2_names_with_spaces_survive(self) -> None:
        out = run_targets([("added", "src/with space.py")])
        self.assertEqual(out["mode"], "files")
        self.assertEqual(out["targets"].splitlines(), ["src/with space.py"])

    def test_sg6_the_script_writes_nothing_into_the_checkout(self) -> None:
        out = run_targets([("added", "a.py")])
        self.assertEqual(out["_root_files"], "a.py", "only the file the test created")

    def test_the_script_only_reads(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        for verb in ("--method", "-X", "POST", "PATCH", "DELETE"):
            self.assertNotIn(verb, text)


class WorkflowShape(unittest.TestCase):
    def test_sg1_main_and_pull_requests_run_the_same_rules_and_flags(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("semgrep/semgrep:1.182.0", text)
        for pack in ("p/python", "p/javascript", "p/typescript", "p/terraform", "p/secrets", "p/github-actions"):
            self.assertIn(f"--config {pack}", text)
        for flag in ("--severity=ERROR", "--error", "--metrics=off"):
            self.assertIn(flag, text)
        self.assertEqual(_on()["push"]["branches"], ["main"])
        self.assertNotIn("paths", _on()["push"])
        self.assertNotIn("paths-ignore", _on()["push"], "main always scans")
        self.assertNotIn("--baseline-commit", text)

    def test_sg3_docs_only_pull_requests_run_the_secrets_pack_alone(self) -> None:
        # No trigger path filter any more: the target step picks mode=secrets for docs-only
        # changes (tests/test_semgrep_nightly_and_incident.py, DO-1).
        self.assertNotIn("paths-ignore", _on()["pull_request"])
        self.assertNotIn("paths", _on()["pull_request"])

    def test_sg3_semgrep_is_not_a_required_check(self) -> None:
        job_name = _workflow()["jobs"]["semgrep"]["name"]
        self.assertNotIn(job_name, REQUIRED_CHECKS)

    def test_sg2_the_scan_runs_only_on_what_the_target_step_chose(self) -> None:
        steps = _workflow()["jobs"]["semgrep"]["steps"]
        names = [s.get("name") for s in steps]
        self.assertLess(names.index("Pick what to scan"), names.index("Scan"))
        pick = steps[names.index("Pick what to scan")]
        self.assertEqual(pick["id"], "targets")
        self.assertIn("semgrep_pr_targets.sh", pick["run"])
        scan = steps[names.index("Scan")]
        self.assertIn("steps.targets.outputs.mode != 'none'", scan["if"])
        self.assertIn("semgrep-targets.txt", scan["run"])
        self.assertIn('--include "${file}"', scan["run"])
        self.assertIn('"${include[@]}"', scan["run"])
        self.assertNotIn("targets[@]", scan["run"], "explicit paths would bypass Semgrep's ignore rules")

    def test_sg2_the_pull_request_job_can_read_the_file_list(self) -> None:
        perms = _workflow()["permissions"]
        self.assertEqual(perms["contents"], "read")
        self.assertEqual(perms["pull-requests"], "read")
        self.assertNotIn("write", " ".join(perms.values()))

    def test_sg6_results_land_under_runner_temp_not_the_repository_root(self) -> None:
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("/src/semgrep.json", text)
        self.assertNotIn("/src/semgrep.txt", text)
        self.assertIn("RUNNER_TEMP", text)
        upload = next(s for s in _workflow()["jobs"]["semgrep"]["steps"] if "upload-artifact" in str(s.get("uses")))
        paths = upload["with"]["path"]
        self.assertIn("runner.temp", paths)
        self.assertNotRegex(paths, r"(?m)^semgrep\.(json|txt)$")

    def test_actions_stay_pinned_by_sha(self) -> None:
        for step in _workflow()["jobs"]["semgrep"]["steps"]:
            if "uses" in step:
                self.assertRegex(step["uses"], r"@[0-9a-f]{40}\b")


if __name__ == "__main__":
    unittest.main()
