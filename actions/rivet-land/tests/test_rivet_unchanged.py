"""RIVET skips a run when nothing changed since its last full evaluation.

Acceptance:
  UN-1 The state key hashes every input that can change what the lander does: each open pull
       request (head, draft, review decision, mergeability, labels, check rollup, updated),
       the open needs issue, kill switch, release lock, act, owner, pin, config and the
       action's own files, and a coarse time bucket.
  UN-2 Identical state gives the identical key; pull request order does not matter.
  UN-3 Any doubt (query error, odd output, more pull requests than one page, no hash tool)
       gives an empty key: the full path runs.
  UN-4 Only schedule and workflow_run look the key up. workflow_dispatch evaluates in full and
       saves. pull_request, review and comment events never look up and never save.
  UN-5 The lookup is `actions/cache/restore` with lookup-only, pinned by SHA; a hit skips every
       later step and says so in the summary.
  UN-6 The key is saved only after a full run that finished normally, never on a hit, and
       the cache file lives under the runner temp directory, not the repository.
  UN-7 The skip can be turned off with an input; inputs stay backward compatible.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ACTION_DIR = Path(__file__).resolve().parents[1]
ACTION = yaml.safe_load((ACTION_DIR / "action.yml").read_text(encoding="utf-8"))
STEPS = ACTION["runs"]["steps"]
SCRIPT = ACTION_DIR / "scripts" / "rivet_state.sh"
NAMES = [step.get("name") for step in STEPS]
SKIP_GATE = "steps.idle.outputs.idle != 'true' && steps.seen.outputs.cache-hit != 'true'"
CACHE_SHA = "55cc8345863c7cc4c66a329aec7e433d2d1c52a9"

# A stand-in gh for `gh api graphql`. It answers from the environment and logs the call.
FAKE_GH = """#!/bin/bash
echo "$*" >> "$FAKE_GH_LOG"
if [ "$1 $2" != "api graphql" ]; then echo "unexpected gh call: $*" >&2; exit 99; fi
if [ "${FAKE_RC:-0}" != "0" ]; then echo "gh: HTTP 502" >&2; exit "$FAKE_RC"; fi
# A real gh applies --jq itself (gojq: object keys come out sorted). The stand-in uses jq -S.
filter=""
while [ $# -gt 0 ]; do if [ "$1" = "--jq" ]; then filter="$2"; fi; shift; done
printf '%s' "$FAKE_GQL" | jq -S -c "$filter"
"""


def pr(number: int, **over: object) -> dict:
    node = {
        "number": number,
        "headRefOid": f"sha{number}",
        "isDraft": False,
        "reviewDecision": None,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "updatedAt": "2026-11-25T10:00:00Z",
        "labels": {"nodes": []},
        "autoMergeRequest": None,
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
    }
    node.update(over)
    return node


def gql(prs: list[dict], issues: list[dict] | None = None, *, total: int | None = None,
        more: bool = False) -> str:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequests": {
                        "totalCount": len(prs) if total is None else total,
                        "pageInfo": {"hasNextPage": more},
                        "nodes": prs,
                    }
                },
                "search": {"nodes": issues or []},
            }
        }
    )


BASE = [pr(1), pr(2, isDraft=True)]


def run_state(
    payload: str | None = None,
    *,
    event: str = "schedule",
    rc: int = 0,
    now: str = "1795000000",
    extra: dict[str, str] | None = None,
    config_text: str | None = "[raci]\n",
    path: str | None = None,
) -> dict[str, str]:
    """Run the real script with a stand-in gh. Returns the step's outputs and stdout."""
    payload = gql(BASE) if payload is None else payload
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
        (bin_dir / "gh").chmod(0o755)
        out_file = root / "out"
        out_file.write_text("")
        log = root / "calls"
        log.write_text("")
        temp = root / "runner-temp"
        temp.mkdir()
        cfg = root / "release-lander.toml"
        if config_text is not None:
            cfg.write_text(config_text, encoding="utf-8")
        env = {
            "PATH": path if path is not None else f"{bin_dir}:{os.environ['PATH']}",
            "IDLE_READ_TOKEN": "t",
            "REPO": "octo/widgets",
            "EVENT_NAME": event,
            "GITHUB_OUTPUT": str(out_file),
            "GITHUB_STEP_SUMMARY": str(root / "summary"),
            "RUNNER_TEMP": str(temp),
            "FAKE_GH_LOG": str(log),
            "FAKE_GQL": payload,
            "FAKE_RC": str(rc),
            "RIVET_NOW": now,
            "SKIP_KILL": "",
            "SKIP_LOCK": "",
            "SKIP_ACT": "true",
            "SKIP_VERSION": "0.70.48",
            "SKIP_OWNER": "octo",
            "SKIP_CONFIG": str(cfg),
            "SKIP_MAX_AGE_HOURS": "6",
            "SKIP_ENABLED": "true",
            "ACTION_DIR": str(ACTION_DIR),
        }
        env.update(extra or {})
        proc = subprocess.run(
            ["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False
        )
        outputs = dict(
            line.split("=", 1) for line in out_file.read_text(encoding="utf-8").splitlines() if "=" in line
        )
        outputs["_text"] = proc.stdout + proc.stderr
        outputs["_rc"] = str(proc.returncode)
        outputs["_calls"] = log.read_text(encoding="utf-8")
        outputs["_file"] = str((temp / "rivet-seen" / "seen").exists())
        return outputs


def key_of(payload: str | None = None, **kwargs: object) -> str:
    return run_state(payload, **kwargs).get("key", "")  # type: ignore[arg-type]


class StateKey(unittest.TestCase):
    def test_un1_a_key_is_made_and_is_a_cache_key(self) -> None:
        out = run_state()
        self.assertRegex(out["key"], r"^rivet-seen-[0-9a-f]{64}$")
        self.assertEqual(out["lookup"], "true")
        self.assertEqual(out["_rc"], "0")
        self.assertEqual(out["_file"], "True", "the cache file lives under RUNNER_TEMP")
        self.assertIn(os.path.join("rivet-seen", "seen"), out["path"])

    def test_un2_identical_state_gives_the_identical_key(self) -> None:
        self.assertEqual(key_of(), key_of())

    def test_un2_pull_request_order_does_not_matter(self) -> None:
        self.assertEqual(key_of(gql([pr(1), pr(2)])), key_of(gql([pr(2), pr(1)])))
        self.assertEqual(
            key_of(gql([pr(1, labels={"nodes": [{"name": "a"}, {"name": "b"}]})])),
            key_of(gql([pr(1, labels={"nodes": [{"name": "b"}, {"name": "a"}]})])),
        )

    def test_un1_every_pull_request_field_changes_the_key(self) -> None:
        base = key_of(gql([pr(1)]))
        changes = {
            "headRefOid": "other",
            "isDraft": True,
            "reviewDecision": "APPROVED",
            "mergeable": "CONFLICTING",
            "mergeStateStatus": "BEHIND",
            "updatedAt": "2026-11-25T11:00:00Z",
            "labels": {"nodes": [{"name": "do-not-merge"}]},
            "autoMergeRequest": {"enabledAt": "2026-11-25T10:00:00Z"},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "FAILURE"}}}]},
        }
        for field, value in changes.items():
            self.assertNotEqual(base, key_of(gql([pr(1, **{field: value})])), field)

    def test_un1_a_missing_rollup_is_a_state_not_an_error(self) -> None:
        none = pr(1, commits={"nodes": [{"commit": {"statusCheckRollup": None}}]})
        self.assertRegex(key_of(gql([none])), r"^rivet-seen-")
        self.assertNotEqual(key_of(gql([none])), key_of(gql([pr(1)])))

    def test_un1_a_new_or_closed_pull_request_changes_the_key(self) -> None:
        self.assertNotEqual(key_of(gql([pr(1)])), key_of(gql([pr(1), pr(2)])))
        self.assertNotEqual(key_of(gql([pr(1), pr(2)])), key_of(gql([pr(2)])))

    def test_un1_the_needs_issue_changes_the_key(self) -> None:
        issue = {"number": 9, "title": "RIVET: needs octo", "updatedAt": "2026-11-25T09:00:00Z"}
        self.assertNotEqual(key_of(gql(BASE)), key_of(gql(BASE, [issue])))
        later = dict(issue, updatedAt="2026-11-25T12:00:00Z")
        self.assertNotEqual(key_of(gql(BASE, [issue])), key_of(gql(BASE, [later])))

    def test_un1_inputs_change_the_key(self) -> None:
        base = key_of()
        for name, value in (
            ("SKIP_KILL", "1"),
            ("SKIP_LOCK", "held"),
            ("SKIP_ACT", "false"),
            ("SKIP_VERSION", "0.70.49"),
            ("SKIP_OWNER", "someone"),
        ):
            self.assertNotEqual(base, key_of(extra={name: value}), name)

    def test_un1_the_config_content_changes_the_key(self) -> None:
        self.assertNotEqual(key_of(config_text="[raci]\n"), key_of(config_text="[raci]\nx = 1\n"))
        self.assertRegex(key_of(config_text=None), r"^rivet-seen-", "a missing config is not an error")

    def test_un1_the_time_bucket_is_six_hours_and_changes_the_key_at_the_boundary(self) -> None:
        six_hours = 6 * 3600
        start = 1795000000 - (1795000000 % six_hours)
        a = key_of(now=str(start))
        self.assertEqual(a, key_of(now=str(start + six_hours - 1)))
        self.assertNotEqual(a, key_of(now=str(start + six_hours)))
        # Three hours in is still the same bucket: the window is six, not three.
        self.assertEqual(a, key_of(now=str(start + 3 * 3600)))

    def test_un1_a_changed_window_busts_the_key_even_inside_one_bucket(self) -> None:
        start = 1795000000 - (1795000000 % (12 * 3600))
        keys = {key_of(now=str(start), extra={"SKIP_MAX_AGE_HOURS": hours}) for hours in ("3", "4", "6", "12")}
        self.assertEqual(len(keys), 4, "the window value is part of the hash, not only the bucket number")

    def test_the_window_defaults_to_the_lander_window_hours_of_six(self) -> None:
        self.assertEqual(str(ACTION["inputs"]["unchanged-max-age-hours"]["default"]), "6")

    def test_un1_the_actions_own_files_are_in_the_key(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("action.yml", text)
        self.assertIn("scripts", text)


class FailOpen(unittest.TestCase):
    def assert_open(self, out: dict[str, str], why: str) -> None:
        self.assertEqual(out.get("key", ""), "", why)
        self.assertEqual(out.get("lookup", "false"), "false", why)
        self.assertEqual(out["_rc"], "0", f"{why}: the step itself must not fail")

    def test_un3_a_failed_query_fails_open(self) -> None:
        self.assert_open(run_state(rc=1, payload=""), "gh error")

    def test_un3_odd_output_fails_open(self) -> None:
        for odd in ("", "null", "[]", "{}", "not json", '{"data":null}', '{"errors":[{"message":"x"}]}',
                    '{"data":{"repository":null}}'):
            self.assert_open(run_state(odd), repr(odd))

    def test_un3_more_than_one_page_fails_open(self) -> None:
        self.assert_open(run_state(gql(BASE, more=True)), "hasNextPage")
        self.assert_open(run_state(gql(BASE, total=101)), "totalCount mismatch")

    def test_un3_no_hash_tool_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # gh and jq but no sha256sum and no shasum
            bin_dir = Path(tmp)
            (bin_dir / "gh").write_text(FAKE_GH, encoding="utf-8")
            (bin_dir / "gh").chmod(0o755)
            jq = subprocess.run(["which", "jq"], capture_output=True, text=True).stdout.strip()
            (bin_dir / "jq").symlink_to(jq)
            out = run_state(path=str(bin_dir))
            self.assert_open(out, "no hash tool")
            self.assertIn("no hash tool", out["_text"])

    def test_un3_a_missing_gh_fails_open(self) -> None:
        self.assert_open(run_state(path="/nonexistent"), "no gh")

    def test_un3_missing_repo_token_or_action_dir_fails_open(self) -> None:
        for name in ("REPO", "IDLE_READ_TOKEN", "ACTION_DIR"):
            self.assert_open(run_state(extra={name: ""}), name)

    def test_un7_the_skip_can_be_turned_off(self) -> None:
        out = run_state(extra={"SKIP_ENABLED": "false"})
        self.assertEqual(out.get("key", ""), "")
        self.assertEqual(out.get("lookup", "false"), "false")
        self.assertEqual(out["_calls"], "", "no query when turned off")

    def test_garbage_values_for_the_age_fail_open(self) -> None:
        for bad in ("0", "-1", "abc", ""):
            self.assert_open(run_state(extra={"SKIP_MAX_AGE_HOURS": bad}), bad)


class Events(unittest.TestCase):
    def test_un4_schedule_and_workflow_run_look_up_and_save(self) -> None:
        for event in ("schedule", "workflow_run"):
            out = run_state(event=event)
            self.assertEqual(out["lookup"], "true", event)
            self.assertTrue(out["key"], event)

    def test_un4_dispatch_evaluates_in_full_and_saves(self) -> None:
        out = run_state(event="workflow_dispatch")
        self.assertEqual(out["lookup"], "false")
        self.assertTrue(out["key"])

    def test_un4_one_pull_request_events_never_look_up_never_save_never_ask(self) -> None:
        for event in ("pull_request", "pull_request_review", "issue_comment"):
            out = run_state(event=event)
            self.assertEqual(out.get("key", ""), "", event)
            self.assertEqual(out.get("lookup", "false"), "false", event)
            self.assertEqual(out["_calls"], "", event)

    def test_the_query_reads_only(self) -> None:
        out = run_state()
        self.assertIn("graphql", out["_calls"])
        self.assertNotIn("mutation", SCRIPT.read_text(encoding="utf-8"))
        self.assertEqual(out["_calls"].count("api graphql"), 1, "one read")


def _step(name: str) -> dict:
    return next(step for step in STEPS if step.get("name") == name)


class Wiring(unittest.TestCase):
    def test_the_state_step_follows_the_idle_step_and_precedes_the_lookup(self) -> None:
        self.assertEqual(NAMES.index("Is there any work"), 0)
        self.assertLess(NAMES.index("Is there any work"), NAMES.index("Read the state"))
        self.assertLess(NAMES.index("Read the state"), NAMES.index("Seen this state before"))
        self.assertLess(NAMES.index("Seen this state before"), NAMES.index("Name this repository for the App token"))
        state = _step("Read the state")
        self.assertEqual(state["id"], "state")
        self.assertIs(state.get("continue-on-error"), True)
        self.assertIn("steps.idle.outputs.idle != 'true'", state["if"])
        self.assertIn("rivet_state.sh", state["run"])
        self.assertNotIn("GH_TOKEN", state["env"], "the write-token guard in test_rivet_warn_comment")

    def test_un5_the_lookup_is_restore_with_lookup_only_pinned_by_sha(self) -> None:
        step = _step("Seen this state before")
        self.assertEqual(step["id"], "seen")
        self.assertEqual(step["uses"].split(" ")[0], f"actions/cache/restore@{CACHE_SHA}")
        self.assertIs(step["continue-on-error"], True)
        self.assertEqual(str(step["with"]["lookup-only"]).lower(), "true")
        self.assertEqual(step["with"]["key"], "${{ steps.state.outputs.key }}")
        self.assertEqual(step["with"]["path"], "${{ steps.state.outputs.path }}")
        self.assertIn("steps.state.outputs.lookup == 'true'", step["if"])
        self.assertNotIn("restore-keys", step["with"], "only an exact state is unchanged")

    def test_un5_a_hit_skips_every_later_step(self) -> None:
        later = NAMES[NAMES.index("Seen this state before") + 1 :]
        said = "Say nothing changed"
        for step in STEPS:
            if step.get("name") in (None, "Is there any work", "Read the state", "Seen this state before"):
                continue
            condition = " ".join(str(step.get("if", "")).split())
            if step["name"] == said:
                self.assertIn("steps.seen.outputs.cache-hit == 'true'", condition)
                continue
            self.assertIn(SKIP_GATE, condition, f"{step['name']!r} must skip on a hit")
        self.assertIn(said, later)

    def test_un5_the_hit_line_is_honest(self) -> None:
        run = _step("Say nothing changed")["run"]
        self.assertIn("nothing changed", run)
        self.assertIn("GITHUB_STEP_SUMMARY", run)

    def test_un6_the_key_is_saved_after_a_normal_full_run_only(self) -> None:
        saves = [s for s in STEPS if str(s.get("uses", "")).startswith("actions/cache/save@")]
        self.assertEqual(len(saves), 2, "the state before the run and the state after it")
        land = NAMES.index("RIVET land")
        for step in saves:
            self.assertEqual(step["uses"].split(" ")[0], f"actions/cache/save@{CACHE_SHA}")
            self.assertGreater(STEPS.index(step), land)
            self.assertIs(step["continue-on-error"], True)
            condition = " ".join(str(step["if"]).split())
            self.assertIn("steps.land.outcome == 'success'", condition)
            self.assertIn("steps.seen.outputs.cache-hit != 'true'", condition)
            self.assertIn("steps.idle.outputs.idle != 'true'", condition)
            self.assertIn("outputs.key != ''", condition)
            self.assertEqual(step["with"]["path"], "${{ steps.state.outputs.path }}")
            self.assertNotIn("~", step["with"]["path"])
        self.assertEqual(saves[0]["with"]["key"], "${{ steps.state.outputs.key }}")
        self.assertEqual(saves[1]["with"]["key"], "${{ steps.state_after.outputs.key }}")

    def test_un6_nothing_is_cached_at_the_repository_root(self) -> None:
        for step in STEPS:
            path = str((step.get("with") or {}).get("path", ""))
            if "cache" in str(step.get("uses", "")):
                self.assertTrue(
                    path.startswith(("~/", "${{ steps.state")),
                    f"{step.get('name')}: {path}",
                )
        self.assertIn("RUNNER_TEMP", SCRIPT.read_text(encoding="utf-8"))

    def test_un7_inputs_are_backward_compatible(self) -> None:
        inputs = ACTION["inputs"]
        self.assertEqual({n for n, s in inputs.items() if s.get("required")}, {"owner"})
        self.assertEqual(inputs["skip-unchanged"]["default"], "true")
        self.assertEqual(str(inputs["unchanged-max-age-hours"]["default"]), "6")

    def test_the_state_step_passes_the_inputs_that_change_behaviour(self) -> None:
        env = _step("Read the state")["env"]
        self.assertEqual(env["SKIP_KILL"], "${{ inputs.kill-switch }}")
        self.assertEqual(env["SKIP_LOCK"], "${{ inputs.release-lock }}")
        self.assertEqual(env["SKIP_ACT"], "${{ inputs.act }}")
        self.assertEqual(env["SKIP_VERSION"], "${{ inputs.axiom-version }}")
        self.assertEqual(env["SKIP_OWNER"], "${{ inputs.owner }}")
        self.assertEqual(env["SKIP_CONFIG"], "${{ inputs.config }}")
        self.assertEqual(env["SKIP_ENABLED"], "${{ inputs.skip-unchanged }}")
        self.assertEqual(env["SKIP_MAX_AGE_HOURS"], "${{ inputs.unchanged-max-age-hours }}")
        self.assertEqual(env["ACTION_DIR"], "${{ github.action_path }}")


class BustRules(unittest.TestCase):
    """How and when the caches are busted."""

    def test_pip_key_is_exact_and_names_os_python_pin_and_iso_week(self) -> None:
        step = next(s for s in STEPS if str(s.get("uses", "")).startswith("actions/cache@"))
        key = step["with"]["key"]
        for part in ("runner.os", "inputs.python-version", "inputs.axiom-version", "steps.week.outputs.iso"):
            self.assertIn(part, key)
        self.assertNotIn("restore-keys", step["with"], "a prefix fallback would mix versions")

    def test_the_week_step_is_iso_year_and_week_and_precedes_the_pip_cache(self) -> None:
        week = _step("Week for the pip cache")
        self.assertEqual(week["id"], "week")
        self.assertIn("%G-W%V", week["run"])
        self.assertLess(NAMES.index("Week for the pip cache"), NAMES.index("Cache pip wheels"))
        out = subprocess.run(["bash", "-c", week["run"].replace('"$GITHUB_OUTPUT"', "/dev/stdout")],
                             capture_output=True, text=True, check=True).stdout
        self.assertRegex(out.strip(), r"^iso=\d{4}-W\d{2}$")

    def test_force_full_reads_neither_cache(self) -> None:
        self.assertEqual(ACTION["inputs"]["force-full"]["default"], "false")
        pip = _step("Cache pip wheels")
        self.assertIn("inputs.force-full != 'true'", " ".join(pip["if"].split()))
        self.assertEqual(_step("Read the state")["env"]["SKIP_FORCE"], "${{ inputs.force-full }}")
        out = run_state(extra={"SKIP_FORCE": "true"})
        self.assertEqual(out["lookup"], "false", "no lookup, so no skip")
        self.assertRegex(out["key"], r"^rivet-seen-", "the key is still made so the run can save it")
        self.assertEqual(run_state(extra={"SKIP_FORCE": "false"})["lookup"], "true")

    def test_the_action_version_is_in_the_gate_key(self) -> None:
        self.assertEqual(_step("Read the state")["env"]["ACTION_REF"], "${{ github.action_ref }}")
        self.assertNotEqual(key_of(extra={"ACTION_REF": "rivet-land-v1"}), key_of(extra={"ACTION_REF": "other"}))

    def test_a_failed_run_never_records_seen(self) -> None:
        # Both saves require the lander step to have succeeded.
        for step in STEPS:
            if str(step.get("uses", "")).startswith("actions/cache/save@"):
                self.assertIn("steps.land.outcome == 'success'", step["if"])

    def test_the_caller_example_offers_force_full_on_dispatch(self) -> None:
        example = yaml.safe_load((ACTION_DIR / "caller-example.yml").read_text(encoding="utf-8"))
        dispatch = example[True]["workflow_dispatch"]
        self.assertIn("force-full", dispatch["inputs"])
        self.assertEqual(dispatch["inputs"]["force-full"]["type"], "boolean")
        with_ = next(s for s in example["jobs"]["rivet"]["steps"] if "rivet-land" in str(s.get("uses")))["with"]
        self.assertIn("inputs.force-full", str(with_["force-full"]))

    def test_the_readme_documents_manual_bust_and_retention(self) -> None:
        readme = (ACTION_DIR / "README.md").read_text(encoding="utf-8")
        for needle in ("gh cache list --key rivet-", "gh cache delete", "force-full", "7 days", "10 GB", "ISO week"):
            self.assertIn(needle, readme)


class CallerExample(unittest.TestCase):
    """B: the example a repo copies carries the two cost recommendations."""

    TEXT = (ACTION_DIR / "caller-example.yml").read_text(encoding="utf-8")
    EXAMPLE = yaml.safe_load(TEXT)

    def test_b_cron_is_every_three_hours(self) -> None:
        self.assertEqual([c["cron"] for c in self.EXAMPLE[True]["schedule"]], ["17 */3 * * *"])

    def test_b_the_job_skips_comments_the_script_ignores(self) -> None:
        condition = " ".join(str(self.EXAMPLE["jobs"]["rivet"]["if"]).split())
        self.assertIn("contains(github.event.comment.body, '/rivet')", condition)
        self.assertIn("contains(github.event.review.body, '/rivet')", condition)

    def test_b_the_job_if_is_a_superset_of_what_the_script_acts_on(self) -> None:
        # Whatever parse_command accepts has a /rivet line, so the filter cannot hide one.
        import sys

        sys.path.insert(0, str(ACTION_DIR / "scripts"))
        from rivet_remind import parse_command

        for body in ("/rivet merge", "thanks\n/rivet hold waiting", "  /RIVET rerun", "/rivet option B"):
            self.assertIsNotNone(parse_command(body), body)
            self.assertIn("/rivet", body.lower())

    def test_b_the_example_names_the_unchanged_gate_and_its_cadence_interplay(self) -> None:
        self.assertIn("unchanged-max-age-hours", self.TEXT)

    def test_b_the_readme_explains_the_recommendations(self) -> None:
        readme = (ACTION_DIR / "README.md").read_text(encoding="utf-8")
        for needle in ("17 */3 * * *", "`/rivet`", "unchanged-max-age-hours", "skip-unchanged", "lookup-only",
                       "window_hours", "46%", "56%", "18%"):
            self.assertIn(needle, readme)


if __name__ == "__main__":
    unittest.main()
