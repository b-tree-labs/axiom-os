"""The lander costs almost nothing when there is nothing to do.

Acceptance:
  ID-1 The first step decides whether there is work, and says so as the output `idle`.
  ID-2 Every later step is skipped when idle is true.
  ID-3 Idle is true only when the pull request count succeeds and is exactly zero, and no
       "RIVET: needs" issue is open (the full run is what closes that issue).
  ID-4 Any doubt (failed call, odd output, rate limit) runs the full path.
  ID-5 A draft pull request is work.
  ID-6 The idle line is written to the job summary, once, and says what happened.
  ID-7 Events that act on one pull request or on a /rivet command are never idle.
  ID-8 The install is cached by the pinned version, with the cache action pinned by SHA.
  ID-10 The example caller runs the backstop every three hours and skips non-command comments.
  ID-9 The kill switch and release lock reach the lander step unchanged.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ACTION_DIR = Path(__file__).resolve().parents[1]
ACTION = yaml.safe_load((ACTION_DIR / "action.yml").read_text(encoding="utf-8"))
STEPS = ACTION["runs"]["steps"]
IDLE_SCRIPT = ACTION_DIR / "scripts" / "rivet_idle.sh"
GATE = "steps.idle.outputs.idle != 'true'"

# A stand-in gh. It answers from files the test writes, and records every call.
FAKE_GH = """#!/bin/bash
echo "$*" >> "$FAKE_GH_LOG"
case "$1 $2" in
  "pr list") out="$FAKE_PR_OUT"; rc="${FAKE_PR_RC:-0}" ;;
  "issue list") out="$FAKE_ISSUE_OUT"; rc="${FAKE_ISSUE_RC:-0}" ;;
  *) echo "unexpected gh call: $*" >&2; exit 99 ;;
esac
if [ "$rc" != "0" ]; then echo "gh: HTTP 403: API rate limit exceeded" >&2; exit "$rc"; fi
printf '%s' "$out"
"""


def _step(name: str) -> dict:
    return next(step for step in STEPS if step.get("name") == name)


def run_idle(
    *,
    pr_out: str = "0\n",
    pr_rc: int = 0,
    issue_out: str = "0\n",
    issue_rc: int = 0,
    event: str = "schedule",
) -> tuple[str, str, str, list[str]]:
    """Run the real script with a stand-in gh. Returns output file, summary, stdout, gh calls."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "gh"
        fake.write_text(FAKE_GH, encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        out_file = root / "output"
        summary = root / "summary"
        log = root / "calls"
        for path in (out_file, summary, log):
            path.write_text("", encoding="utf-8")
        env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GH_TOKEN": "t",
            "REPO": "octo/widgets",
            "EVENT_NAME": event,
            "GITHUB_OUTPUT": str(out_file),
            "GITHUB_STEP_SUMMARY": str(summary),
            "FAKE_GH_LOG": str(log),
            "FAKE_PR_OUT": pr_out,
            "FAKE_PR_RC": str(pr_rc),
            "FAKE_ISSUE_OUT": issue_out,
            "FAKE_ISSUE_RC": str(issue_rc),
        }
        proc = subprocess.run(
            ["bash", str(IDLE_SCRIPT)], env=env, capture_output=True, text=True, check=False
        )
        calls = [line for line in log.read_text(encoding="utf-8").splitlines() if line]
        return (
            out_file.read_text(encoding="utf-8"),
            summary.read_text(encoding="utf-8"),
            proc.stdout + proc.stderr + f"\nrc={proc.returncode}",
            calls,
        )


class IdleStepShape(unittest.TestCase):
    def test_id1_the_first_step_is_the_idle_decision(self) -> None:
        first = STEPS[0]
        self.assertEqual(first.get("id"), "idle")
        self.assertIn("rivet_idle.sh", first["run"])
        self.assertEqual(first["shell"], "bash")
        self.assertNotIn("if", first, "the decision itself always runs")
        self.assertNotIn("uses", first, "no action to download before the decision")

    def test_the_idle_step_reads_with_its_own_variable_not_the_write_token_name(self) -> None:
        # The existing guard keeps `GH_TOKEN: inputs.github-token` out of the file: every
        # writing step uses the resolved token. The count is read-only and runs before
        # the App token exists, so it names its own variable.
        self.assertEqual(STEPS[0]["env"]["IDLE_READ_TOKEN"], "${{ inputs.github-token }}")
        self.assertNotIn("GH_TOKEN", STEPS[0]["env"])

    def test_the_script_maps_the_read_token_to_gh(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "gh").write_text('#!/bin/bash\necho "$GH_TOKEN" >> "$TOKEN_LOG"\nprintf 0\n')
            (root / "gh").chmod(0o755)
            out_file = root / "o"
            out_file.write_text("")
            env = {
                "PATH": f"{root}:{os.environ['PATH']}", "IDLE_READ_TOKEN": "read-token",
                "REPO": "o/r", "EVENT_NAME": "schedule", "GITHUB_OUTPUT": str(out_file),
                "TOKEN_LOG": str(root / "t"),
            }
            subprocess.run(["bash", str(IDLE_SCRIPT)], env=env, capture_output=True, check=False)
            self.assertEqual((root / "t").read_text().split(), ["read-token", "read-token"])

    def test_id2_every_later_step_is_gated_on_idle(self) -> None:
        for step in STEPS[1:]:
            condition = " ".join(str(step.get("if", "")).split())
            self.assertIn(GATE, condition, f"step {step.get('name')!r} is not gated on idle")

    def test_id2_always_steps_keep_always_and_add_the_gate(self) -> None:
        for step in STEPS[1:]:
            condition = " ".join(str(step.get("if", "")).split())
            if "always()" in condition:
                self.assertIn(GATE, condition)

    def test_id9_kill_switch_and_release_lock_still_reach_the_lander(self) -> None:
        env = _step("RIVET land")["env"]
        self.assertEqual(env["AXIOM_LANDER_KILL"], "${{ inputs.kill-switch }}")
        self.assertEqual(env["AXIOM_RELEASE_LOCK"], "${{ inputs.release-lock }}")

    def test_id9_inputs_are_backward_compatible(self) -> None:
        # No new required input, and none of the old ones is gone.
        inputs = ACTION["inputs"]
        for name in (
            "axiom-version", "config", "github-token", "owner", "act", "python-version",
            "kill-switch", "release-lock", "openrouter-api-key", "fixer-model",
            "fixer-routing-tier", "app-id", "app-private-key",
        ):
            self.assertIn(name, inputs)
        required = {name for name, spec in inputs.items() if spec.get("required")}
        self.assertEqual(required, {"owner"})

    def test_id8_install_is_cached_by_the_pinned_version(self) -> None:
        names = [step.get("name") for step in STEPS]
        cache = next(step for step in STEPS if str(step.get("uses", "")).startswith("actions/cache@"))
        self.assertRegex(cache["uses"], r"^actions/cache@[0-9a-f]{40}\b")
        self.assertIn("inputs.axiom-version", cache["with"]["key"])
        self.assertIn("inputs.python-version", cache["with"]["key"])
        self.assertLess(STEPS.index(cache), names.index("Install axiom-os-lm"))
        self.assertGreater(STEPS.index(cache), names.index("Set up Python"))

    def test_id8_every_action_is_still_pinned_by_sha(self) -> None:
        for step in STEPS:
            uses = step.get("uses")
            if uses:
                self.assertRegex(uses, r"@[0-9a-f]{40}$|@[0-9a-f]{40} ", uses)


class CallerExample(unittest.TestCase):
    """The example a repo copies keeps the schedule sparse and skips comments that are not commands."""

    EXAMPLE = yaml.safe_load((ACTION_DIR / "caller-example.yml").read_text(encoding="utf-8"))

    def test_id10_the_example_cron_is_every_three_hours(self) -> None:
        crons = [item["cron"] for item in self.EXAMPLE[True]["schedule"]]
        self.assertEqual(crons, ["17 */3 * * *"])

    def test_id10_the_job_skips_comments_and_reviews_that_carry_no_rivet_command(self) -> None:
        condition = " ".join(str(self.EXAMPLE["jobs"]["rivet"]["if"]).split())
        self.assertIn("contains(github.event.comment.body, '/rivet')", condition)
        self.assertIn("contains(github.event.review.body, '/rivet')", condition)
        # Other events are never filtered by it.
        self.assertIn("github.event_name != 'issue_comment'", condition)
        self.assertIn("github.event_name != 'pull_request_review'", condition)


class IdleDecision(unittest.TestCase):
    def test_id3_zero_pull_requests_and_no_needs_issue_is_idle(self) -> None:
        out, summary, _text, calls = run_idle()
        self.assertIn("idle=true", out)
        self.assertEqual(len(calls), 2)
        # draft pull requests count: no --draft filter, no label filter, open only
        pr_call = next(c for c in calls if c.startswith("pr list"))
        self.assertIn("--state open", pr_call)
        self.assertNotIn("--draft", pr_call)
        self.assertNotIn("--label", pr_call)
        self.assertIn("--repo octo/widgets", pr_call)

    def test_id5_one_open_pull_request_is_not_idle(self) -> None:
        out, summary, _text, _calls = run_idle(pr_out="1\n")
        self.assertIn("idle=false", out)
        self.assertNotIn("idle=true", out)
        self.assertEqual(summary, "")

    def test_id5_many_open_pull_requests_are_not_idle(self) -> None:
        out, *_ = run_idle(pr_out="37\n")
        self.assertIn("idle=false", out)

    def test_id3_an_open_needs_issue_keeps_the_full_run(self) -> None:
        out, summary, _text, _calls = run_idle(issue_out="1\n")
        self.assertIn("idle=false", out)
        self.assertEqual(summary, "")

    def test_id4_a_failed_pull_request_call_fails_open(self) -> None:
        out, summary, text, _calls = run_idle(pr_rc=1, pr_out="")
        self.assertIn("idle=false", out)
        self.assertNotIn("idle=true", out)
        self.assertEqual(summary, "")
        self.assertIn("rc=0", text, "the step itself must not fail the job")
        self.assertIn("rate limit", text.lower())

    def test_id4_a_failed_issue_call_fails_open(self) -> None:
        out, *_ = run_idle(issue_rc=1, issue_out="")
        self.assertIn("idle=false", out)

    def test_id4_odd_output_fails_open(self) -> None:
        for odd in ("", "null", "[]", "0 0", "zero", "-1", "00", " ", "0x0", "{}"):
            out, *_ = run_idle(pr_out=odd)
            self.assertIn("idle=false", out, f"pull request output {odd!r}")
            self.assertNotIn("idle=true", out, f"pull request output {odd!r}")
        for odd in ("", "null", "[]", "none"):
            out, *_ = run_idle(issue_out=odd)
            self.assertIn("idle=false", out, f"issue output {odd!r}")

    def test_id4_a_missing_gh_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "output"
            out_file.write_text("", encoding="utf-8")
            env = {
                "PATH": "/nonexistent",
                "GH_TOKEN": "t",
                "REPO": "octo/widgets",
                "EVENT_NAME": "schedule",
                "GITHUB_OUTPUT": str(out_file),
            }
            proc = subprocess.run(
                ["/bin/bash", str(IDLE_SCRIPT)], env=env, capture_output=True, text=True, check=False
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("idle=false", out_file.read_text(encoding="utf-8"))

    def test_id4_a_missing_repo_or_token_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_file = Path(tmp) / "output"
            out_file.write_text("", encoding="utf-8")
            env = {"PATH": os.environ["PATH"], "EVENT_NAME": "schedule", "GITHUB_OUTPUT": str(out_file)}
            proc = subprocess.run(
                ["bash", str(IDLE_SCRIPT)], env=env, capture_output=True, text=True, check=False
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("idle=false", out_file.read_text(encoding="utf-8"))

    def test_id6_the_summary_has_one_honest_line(self) -> None:
        out, summary, _text, _calls = run_idle()
        self.assertEqual(
            summary.strip(), "RIVET lander: no open pull requests; nothing to evaluate."
        )
        self.assertEqual(out.count("idle="), 1)

    def test_id7_events_on_one_pull_request_never_ask_github_and_never_idle(self) -> None:
        for event in ("pull_request", "pull_request_review", "issue_comment"):
            out, summary, _text, calls = run_idle(event=event)
            self.assertIn("idle=false", out, event)
            self.assertEqual(calls, [], event)
            self.assertEqual(summary, "", event)

    def test_id7_the_open_set_events_can_be_idle(self) -> None:
        for event in ("schedule", "workflow_dispatch", "workflow_run"):
            out, *_ = run_idle(event=event)
            self.assertIn("idle=true", out, event)

    def test_the_script_never_writes_to_the_repository(self) -> None:
        text = IDLE_SCRIPT.read_text(encoding="utf-8")
        calls = re.findall(r"\$\(gh\s+(\w+)\s+(\w+)", text)
        self.assertTrue(calls)
        for noun, verb in calls:
            self.assertEqual(verb, "list", f"gh {noun} {verb}")


if __name__ == "__main__":
    unittest.main()
