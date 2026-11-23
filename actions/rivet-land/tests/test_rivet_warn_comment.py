"""Contract for the Rivet lander config and comment."""

from __future__ import annotations

import json
import re
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from rivet_warn_comment import (  # noqa: E402
    comment_body,
    fleet_lines,
    redact,
    reviewer_section,
    sticky_comment_jq,
)

ACTION = Path(__file__).resolve().parents[1]


class RivetWarnCommentTest(unittest.TestCase):
    def test_redacts_webhook_material(self) -> None:
        raw = "handoff url https://hooks.example/fix key super-secret-value"
        cleaned = redact(raw, ["super-secret-value", "https://hooks.example/fix"])
        self.assertNotIn("super-secret-value", cleaned)
        self.assertNotIn("https://hooks.example/fix", cleaned)
        self.assertIn("[redacted]", cleaned)

    def test_comment_is_warn_only_and_names_the_batching_gate(self) -> None:
        body = comment_body(output="would approve", version="", pin_ready=False)
        visible, _details, _rest = body.partition("<details>")
        self.assertIn("Ready to land: all required checks passed.", visible)
        self.assertIn("Next step:", visible)
        self.assertIn("does not bypass that gate", visible)
        self.assertIn("batching gate", visible)
        self.assertIn("do-not-merge", visible)
        self.assertIn("Hold:", visible)
        self.assertNotIn("would approve", visible)
        self.assertNotIn('"outcomes"', visible)
        self.assertNotIn("only reports", body)
        self.assertNotIn("does not merge by itself", body)
        self.assertIn("0.70.3", visible)
        self.assertIn("TODO", visible)
        self.assertNotIn("super-secret", body)
        self.assertIn("would approve", body)
        self.assertTrue(body.strip().endswith("</details>"))

    def test_failing_checks_and_hold_are_plain_english(self) -> None:
        raw = json.dumps(
            {
                "stopped": "",
                "outcomes": [
                    {
                        "number": 2716,
                        "status": "escalated",
                        "reason": "foreign-claim:do-not-merge",
                        "action_id": "abc",
                        "head_sha": "deadbeef",
                        "steps": ["diagnose", "hold"],
                    }
                ],
            }
        )
        body = comment_body(
            output=raw,
            version="0.70.5",
            pin_ready=True,
            pr_number=2716,
            failing=[
                {"name": "backend-tests", "url": "https://example.test/backend"},
                {"name": "lint", "url": "https://example.test/lint"},
            ],
            hold={
                "reason": "waiting on the DNS cutover",
                "releases": "octo-owner",
                "review_by_text": "Oct 30",
            },
            labels=["do-not-merge"],
        )
        visible, _sep, details = body.partition("<details>")
        self.assertIn(
            "Not landing yet: 2 checks failing (backend-tests, lint). "
            "Held: do-not-merge, reason: waiting on the DNS cutover, review by Oct 30.",
            visible,
        )
        self.assertIn("[backend-tests](https://example.test/backend)", visible)
        self.assertIn("[lint](https://example.test/lint)", visible)
        self.assertIn("Next step: Fix the failing checks and push again.", visible)
        self.assertIn("octo-owner removes the do-not-merge label", visible)
        self.assertNotIn('"outcomes"', visible)
        self.assertNotIn('"head_sha"', visible)
        self.assertIn('"outcomes"', details)
        self.assertIn("<summary>Lander record</summary>", details)

    def test_for_reviewers_is_brief_and_has_no_json(self) -> None:
        section = reviewer_section(
            title="ci(rivet): require Hold: reason (PENG-834)",
            body=(
                "Adds a hold comment check.\n\n"
                "Stacked on #2712.\n"
                "The workflow must not land past the hold."
            ),
            files=[
                {"path": ".github/workflows/rivet-lander.yml", "additions": 20, "deletions": 4},
                {"path": ".github/scripts/rivet_hold_check.py", "additions": 300, "deletions": 0},
                {"path": "services/webapp/api/routes/auth.py", "additions": 12, "deletions": 1},
            ],
            hold={
                "reason": "waiting on the DNS cutover",
                "releases": "octo-owner",
                "review_by_text": "Oct 30",
            },
        )
        self.assertIn("## For reviewers", section)
        self.assertIn("Adds a hold comment check.", section)
        self.assertIn("CI config (risk)", section)
        self.assertIn("auth/access roles (risk)", section)
        self.assertIn("Size: 3 files, +332 −5.", section)
        self.assertIn("PENG-834", section)
        self.assertIn("stacked-on #2712", section)
        self.assertIn("Reason: waiting on the DNS cutover.", section)
        self.assertIn("Review by Oct 30.", section)
        self.assertIn("Check this:", section)
        self.assertGreaterEqual(section.count("\n- "), 2)
        self.assertLessEqual(section.count("\n- "), 4)
        self.assertNotIn("{", section)
        self.assertNotIn('"outcomes"', section)
        body = comment_body(
            output="idle",
            version="0.70.5",
            pin_ready=True,
            title="ci(rivet): require Hold: reason (PENG-834)",
            pr_body="Adds a hold comment check.",
            files=[{"path": ".github/workflows/rivet-lander.yml", "additions": 1, "deletions": 0}],
            hold={"reason": "Octo merges it", "review_by_text": "Oct 30", "releases": "Octo"},
        )
        visible, _sep, _details = body.partition("<details>")
        self.assertIn("## For reviewers", visible)
        self.assertEqual(visible.count("## For reviewers"), 1)

    def test_sticky_comment_is_updated_in_place(self) -> None:
        expr = sticky_comment_jq()
        self.assertIn("contains", expr)
        self.assertIn("rivet-lander-warn", expr)
        source = (ACTION / "scripts" / "rivet_warn_comment.py").read_text(encoding="utf-8")
        self.assertIn('"-X"', source)
        self.assertIn('"PATCH"', source)
        self.assertIn("sticky_comment_jq()", source)

    def test_ready_pin_does_not_ask_for_a_todo(self) -> None:
        body = comment_body(output="idle", version="9.9.9", pin_ready=True)
        self.assertIn("axiom-os-lm==9.9.9", body)
        self.assertNotIn("TODO", body)

    def test_example_config_is_the_safe_default(self) -> None:
        text = (ACTION / "release-lander.example.toml").read_text()
        cfg = tomllib.loads(text)
        self.assertEqual(cfg["mode"], "warn")
        self.assertEqual(cfg["fix_provider"], "llm")
        self.assertEqual(cfg["fixer"]["max_attempts"], 2)
        self.assertEqual(set(cfg["fixer"]["commands"]), {"lint", "test"})
        self.assertEqual(cfg["claims"]["label"], "rivet:claimed")
        self.assertIn("do-not-merge", cfg["claims"]["foreign_labels"])
        # Tier 0 and the tier-1 fixer act unattended; protected paths still route to a person.
        self.assertEqual(cfg["raci"]["auto_allowed"], ["merge", "rerun", "update_branch", "repair"])
        self.assertNotIn("auto_approver", cfg["raci"])
        self.assertTrue(cfg["reminders"]["rerun_cancelled"])
        self.assertEqual(set(cfg["human_paths"]), {"pricing", "security", "data", "deploy"})
        self.assertNotIn("sk_", text)
        self.assertNotIn("ghp_", text)
        self.assertNotRegex(text, r"(?i)soilmetrix|benjaminbooth|janna")

    def test_action_is_least_privilege_and_names_no_one(self) -> None:
        text = (ACTION / "action.yml").read_text()
        for script in (
            "rivet_remind.py",
            "rivet_hold_check.py",
            "rivet_warn_comment.py",
            "verify_config.py",
        ):
            self.assertIn(script, text)
        for name in ("axiom-version", "config", "github-token", "owner", "act"):
            self.assertRegex(text, rf"(?m)^  {name}:")
        self.assertIn("axiom-os-lm==", text)
        self.assertIn("AXIOM_LANDER_KILL", text)
        self.assertIn("--stdout-only", text)
        self.assertNotIn("| tee ", text)
        self.assertNotIn("gh pr merge", text)
        self.assertNotIn("git push", text)
        self.assertNotRegex(text, r"(?i)soilmetrix|octo-owner")
        # Every action ref is pinned to a commit.
        for ref in re.findall(r"uses: (\S+)", text):
            self.assertRegex(ref, r"@[0-9a-f]{40}$")
        caller = (ACTION / "caller-example.yml").read_text()
        for event in (
            "pull_request:",
            "schedule:",
            "workflow_dispatch:",
            "issue_comment:",
            "pull_request_review:",
        ):
            self.assertIn(event, caller)
        self.assertIn("@rivet-land-v1", caller)
        self.assertIn("owner:", caller)

    def test_example_human_paths_hold_money_credentials_data_and_deploys(self) -> None:
        human_paths = tomllib.loads((ACTION / "release-lander.example.toml").read_text())[
            "human_paths"
        ]
        compiled = tuple(
            re.compile(pattern, re.IGNORECASE)
            for patterns in human_paths.values()
            for pattern in patterns
        )
        held = (
            "api/routes/billing.py",
            "api/routes/stripe_webhook.py",
            "api/routes/auth.py",
            "api/security/tokens.py",
            "credentials.json",
            "deployment/migrations/20260101_foo.py",
            "schema/users.sql",
            ".github/workflows/promote-prod.yml",
        )
        open_paths = (
            "docs/billing/notes.md",
            "docs/security/overview.md",
            "frontend/src/App.jsx",
            "README.md",
            "package.json",
            "Dockerfile",
            "tests/conftest.py",
            ".github/workflows/lint.yml",
        )
        for path in held:
            self.assertTrue(any(p.search(path) for p in compiled), path)
        for path in open_paths:
            self.assertFalse(any(p.search(path) for p in compiled), path)

    def test_fleet_lines_lists_every_actionable_pr_and_folds_drafts(self) -> None:
        """Drafts once filled an 8-row cap and hid a green, held PR ."""
        outcomes = [{"number": 3000 + i, "status": "held", "reason": "draft"} for i in range(9)]
        outcomes += [
            {"number": 2900 + i, "status": "held", "reason": "human_route"} for i in range(9)
        ]
        outcomes.append({"number": 2929, "status": "waiting", "reason": "protected:deploy"})
        _lead, bullets, _next = fleet_lines({"outcomes": outcomes})
        text = "\n".join(bullets)
        self.assertIn("#2929: protected:deploy.", text)
        for i in range(9):
            self.assertIn(f"#{2900 + i}: human route.", text)
        draft_lines = [line for line in bullets if "draft" in line.lower()]
        self.assertEqual(len(draft_lines), 1)
        self.assertTrue(draft_lines[0].startswith("- Drafts (9): "))
        self.assertIn("#3008", draft_lines[0])
        self.assertEqual(bullets[-1], draft_lines[0])


if __name__ == "__main__":
    unittest.main()


class CompositeContextTest(unittest.TestCase):
    """GitHub rejects a composite action that reads a context it cannot see. v0.70.40 read
    vars.AXIOM_LANDER_KILL and failed to load in every repo, so the caller passes them in."""

    def test_the_action_reads_no_context_a_composite_action_cannot_see(self) -> None:
        text = (ACTION / "action.yml").read_text()
        self.assertNotRegex(text, r"\$\{\{[^}]*\b(vars|secrets|matrix|needs|jobs)\.")

    def test_the_kill_switch_and_release_lock_are_inputs(self) -> None:
        text = (ACTION / "action.yml").read_text()
        for name in ("kill-switch", "release-lock"):
            self.assertRegex(text, rf"(?m)^  {name}:")
        caller = (ACTION / "caller-example.yml").read_text()
        self.assertIn("kill-switch: ${{ vars.AXIOM_LANDER_KILL }}", caller)
        self.assertIn("release-lock: ${{ vars.AXIOM_RELEASE_LOCK }}", caller)


class AppTokenTest(unittest.TestCase):
    """2026-11-20: merges made with the Actions token start no other workflow, so main's
    test gates never ran on what RIVET landed. With app-id and app-private-key the
    action lands as the RIVET GitHub App; without them it keeps the Actions token."""

    def test_the_app_token_is_minted_only_when_both_inputs_are_set(self) -> None:
        text = (ACTION / "action.yml").read_text()
        for name in ("app-id", "app-private-key"):
            self.assertRegex(text, rf"(?m)^  {name}:")
        self.assertIn("actions/create-github-app-token@", text)
        self.assertIn("inputs.app-id != '' && inputs.app-private-key != ''", text)

    def test_every_gh_token_uses_the_resolved_token(self) -> None:
        text = (ACTION / "action.yml").read_text()
        self.assertNotIn("GH_TOKEN: ${{ inputs.github-token }}", text)
        self.assertGreaterEqual(text.count("GH_TOKEN: ${{ steps.token.outputs.value }}"), 4)

    def test_the_caller_example_passes_the_app_credentials(self) -> None:
        caller = (ACTION / "caller-example.yml").read_text()
        self.assertIn("app-id: ${{ vars.RIVET_APP_ID }}", caller)
        self.assertIn("app-private-key: ${{ secrets.RIVET_APP_PRIVATE_KEY }}", caller)
