"""RIVET reminders reach the owner through comments, one issue, and labels.

State lives in comment markers and labels. Nothing under the runner temp
dir is the source of truth.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ACTION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ACTION / "scripts"))

from rivet_remind import (  # noqa: E402
    CheckView,
    Cmd,
    CommentView,
    GhClient,
    Outcome,
    PullSnapshot,
    Decision,
    WaitingRow,
    _apply_decision,
    _auto_merge_holder,
    _comments_from,
    author_allowed,
    auto_merge_turned_off_by,
    cancelled_targets,
    check_request,
    claim_is_leaked,
    command_plan,
    configure,
    decide_pull,
    encode_state,
    format_wait,
    fresh_remind,
    is_urgent,
    issue_body,
    load_reminder_config,
    needs_owner,
    parse_command,
    parse_state,
    pull_read_plan,
    rate_limited,
    remind_due,
    rerun_targets,
    retry_seconds,
    run_id_from_url,
    sort_waiting,
    sticky_body,
)

OWNER = "octo-owner"
NOW = datetime(2026, 10, 26, 21, 0, tzinfo=UTC)
CFG = None  # filled in setUpModule


def setUpModule() -> None:
    """Load the shipped example config, with the two rerun names a repo would set."""
    global CFG, MARKER, ASSIGNEE, ISSUE_TITLE
    text = (ACTION / "release-lander.example.toml").read_text()
    text = text.replace(
        "rerun_check_names = []",
        'rerun_check_names = ["PR batching check", "Backend unit tests"]',
    )
    path = Path(tempfile.mkdtemp()) / "release-lander.toml"
    path.write_text(text)
    CFG = load_reminder_config(path, OWNER)
    configure(owner=CFG.assignee, issue_title=CFG.issue_title, marker=CFG.sticky_marker)
    import rivet_remind

    MARKER, ASSIGNEE, ISSUE_TITLE = (
        rivet_remind.MARKER,
        rivet_remind.ASSIGNEE,
        rivet_remind.ISSUE_TITLE,
    )


def _pull(**kwargs) -> PullSnapshot:
    base = dict(
        number=42,
        title="ci(rivet): remind the owner when a pull request is held",
        body="The lander escalates into a temp file. The owner never sees it.",
        url="https://github.com/o/r/pull/42",
        labels=("do-not-merge",),
        files=(".github/workflows/rivet-lander.yml",),
        created_at=NOW - timedelta(hours=30),
        head_sha="abc123",
        author="cursoragent",
        checks=(),
        comments=(),
    )
    base.update(kwargs)
    return PullSnapshot(**base)


def _comment(body: str, author: str, at: datetime, comment_id: int = 1) -> CommentView:
    return CommentView(body=body, author=author, created_at=at, comment_id=comment_id)


class CommentIdTest(unittest.TestCase):
    def test_graphql_node_ids_do_not_crash_the_refresh(self) -> None:
        raw = [
            {
                "id": "IC_kwDOPgu7788AAAABfyOtTQ",
                "body": "<!-- rivet-needs-owner -->\nhello",
                "author": {"login": "github-actions[bot]"},
                "createdAt": "2026-10-26T21:41:51Z",
            }
        ]
        comments = _comments_from(raw)
        self.assertEqual(len(comments), 1)
        self.assertIsNone(comments[0].comment_id)
        self.assertIn("rivet-needs-owner", comments[0].body)
        numeric = _comments_from(
            [
                {
                    "id": 42,
                    "body": "ok",
                    "user": {"login": "octo-owner"},
                    "created_at": "2026-10-26T21:00:00Z",
                }
            ]
        )
        self.assertEqual(numeric[0].comment_id, 42)


class ParseCommandTest(unittest.TestCase):
    def test_accepts_the_five_commands(self) -> None:
        self.assertEqual(parse_command("/rivet merge"), ("merge", ""))
        self.assertEqual(parse_command("  /rivet option 2"), ("option", "2"))
        self.assertEqual(parse_command("/rivet option merge"), ("option", "merge"))
        self.assertEqual(
            parse_command("/rivet hold waiting on the DNS cutover"),
            ("hold", "waiting on the DNS cutover"),
        )
        self.assertEqual(parse_command("/rivet rerun\n"), ("rerun", ""))
        self.assertEqual(parse_command("note\n/rivet close"), ("close", ""))

    def test_rejects_other_authors_words_and_unknown_verbs(self) -> None:
        self.assertIsNone(parse_command("please /rivet merge"))
        self.assertIsNone(parse_command("/rivet dance"))
        self.assertIsNone(parse_command("/rivet merge now"))
        self.assertIsNone(parse_command(""))
        self.assertTrue(author_allowed("octo-owner"))
        self.assertTrue(author_allowed("Octo-Owner"))
        self.assertFalse(author_allowed("cursoragent"))
        self.assertFalse(author_allowed("github-actions[bot]"))


class StickyCommentTest(unittest.TestCase):
    def test_hold_comment_is_one_marked_sticky_with_a_decision(self) -> None:
        body = sticky_body(
            pull=_pull(),
            reason="foreign-claim:do-not-merge",
            now=NOW,
            state={},
            urgent=False,
            rerunning=(),
        )
        self.assertIn(MARKER, body)
        self.assertEqual(body.count(MARKER), 1)
        refresher, _rest = body.split("**Blocking:**", 1)
        refresher_lines = [
            line
            for line in refresher.splitlines()
            if line.strip() and not line.startswith("<!--") and not line.startswith("#")
        ]
        self.assertGreaterEqual(len(refresher_lines), 2)
        self.assertLessEqual(len(refresher_lines), 3)
        self.assertIn("foreign-claim:do-not-merge", body)
        options = [line for line in body.splitlines() if line[:2] in {"1.", "2.", "3."}]
        self.assertGreaterEqual(len(options), 2)
        self.assertLessEqual(len(options), 3)
        for line in options:
            self.assertIn("**", line)
            self.assertGreater(len(line.split("**")[-1].strip()), 20)
        self.assertIn("**Recommendation:**", body)
        for command in (
            "/rivet merge",
            "/rivet option",
            "/rivet hold",
            "/rivet rerun",
            "/rivet close",
        ):
            self.assertIn(command, body)
        state = parse_state(body)
        self.assertEqual(state["waiting_since"], NOW.isoformat())
        self.assertEqual(encode_state(state).count("rivet-needs-owner-state"), 1)

    def test_reremind_is_a_fresh_mention_and_not_the_sticky_marker(self) -> None:
        text = fresh_remind(_pull(), "protected:pricing,security", NOW)
        self.assertIn("@octo-owner", text)
        self.assertNotIn(MARKER, text)
        self.assertIn("/rivet merge", text)

    def test_reremind_waits_24_hours_or_4_for_urgent_work(self) -> None:
        state = {
            "waiting_since": (NOW - timedelta(hours=25)).isoformat(),
            "last_reminded": (NOW - timedelta(hours=25)).isoformat(),
        }
        comments = (_comment("landed nothing", "github-actions[bot]", NOW - timedelta(hours=25)),)
        self.assertTrue(remind_due(state, comments, NOW, hours=24, assignee=ASSIGNEE))
        self.assertFalse(
            remind_due(state, comments, NOW - timedelta(hours=2), hours=24, assignee=ASSIGNEE)
        )
        answered = comments + (
            _comment("Looking tomorrow", "octo-owner", NOW - timedelta(hours=1)),
        )
        self.assertFalse(remind_due(state, answered, NOW, hours=24, assignee=ASSIGNEE))
        quiet_after = answered
        later = NOW + timedelta(hours=25)
        self.assertTrue(remind_due(state, quiet_after, later, hours=24, assignee=ASSIGNEE))
        recent = {"last_reminded": (NOW - timedelta(hours=5)).isoformat()}
        self.assertFalse(remind_due(recent, (), NOW, hours=24, assignee=ASSIGNEE))
        self.assertTrue(remind_due(recent, (), NOW, hours=4, assignee=ASSIGNEE))

    def test_prod_billing_and_security_are_urgent(self) -> None:
        assert CFG is not None
        self.assertTrue(
            is_urgent(
                _pull(title="production deploy of billing", files=("README.md",), labels=()), CFG
            )
        )
        self.assertTrue(
            is_urgent(
                _pull(
                    title="rates",
                    files=("services/webapp/api/routes/billing.py",),
                    labels=(),
                    body="numbers",
                ),
                CFG,
            )
        )
        self.assertTrue(
            is_urgent(_pull(labels=("security",), files=("README.md",), body="notes"), CFG)
        )
        self.assertFalse(
            is_urgent(
                _pull(title="product copy", files=("README.md",), labels=(), body="a product"), CFG
            )
        )


class IssueAndStateTest(unittest.TestCase):
    def test_issue_lists_the_longest_urgent_wait_first(self) -> None:
        rows = [
            WaitingRow(3, "docs", "https://example/3", 2 * 3600, False, "draft"),
            WaitingRow(
                9, "billing change", "https://example/9", 5 * 3600, True, "protected:pricing"
            ),
            WaitingRow(
                4, "older billing", "https://example/4", 30 * 3600, True, "protected:security"
            ),
            WaitingRow(8, "old docs", "https://example/8", 40 * 3600, False, "conflict"),
        ]
        ordered = sort_waiting(rows)
        self.assertEqual([row.number for row in ordered], [4, 9, 8, 3])
        body = issue_body(ordered, now=NOW)
        self.assertIn(ISSUE_TITLE, body)
        self.assertLess(body.index("#4"), body.index("#9"))
        self.assertLess(body.index("#9"), body.index("#8"))
        self.assertIn(format_wait(30 * 3600), body)
        self.assertIn("1d 6h", body)
        self.assertNotIn(MARKER, body)

    def test_neutral_check_has_a_real_summary_and_never_an_empty_success(self) -> None:
        payload = check_request(
            name="Rivet", sha="abc", held=True, summary="Held: protected pricing path."
        )
        self.assertEqual(payload["conclusion"], "neutral")
        self.assertIn("protected pricing", payload["output"]["summary"])
        self.assertTrue(payload["output"]["summary"].strip())
        clear = check_request(
            name="Rivet", sha="abc", held=False, summary="Nothing is held on this pull request."
        )
        self.assertEqual(clear["conclusion"], "success")
        self.assertGreater(len(clear["output"]["summary"]), 10)
        with self.assertRaises(ValueError):
            check_request(name="Rivet", sha="abc", held=True, summary="  ")
        with self.assertRaises(ValueError):
            check_request(name="Rivet", sha="abc", held=False, summary="success")

    def test_leaked_claims_use_the_label_event_not_a_temp_file(self) -> None:
        fresh = [
            {
                "event": "labeled",
                "label": "rivet:claimed",
                "created_at": NOW - timedelta(minutes=10),
            }
        ]
        stale = [
            {"event": "labeled", "label": "rivet:claimed", "created_at": NOW - timedelta(hours=2)}
        ]
        self.assertFalse(claim_is_leaked(fresh, now=NOW, ttl_s=3600, closed=False))
        self.assertTrue(claim_is_leaked(stale, now=NOW, ttl_s=3600, closed=False))
        self.assertTrue(claim_is_leaked(fresh, now=NOW, ttl_s=3600, closed=True))
        self.assertTrue(claim_is_leaked([], now=NOW, ttl_s=3600, closed=False))
        other = [{"event": "labeled", "label": "do-not-merge", "created_at": NOW}]
        self.assertTrue(claim_is_leaked(other, now=NOW, ttl_s=3600, closed=False))


class CancelledRerunTest(unittest.TestCase):
    """Tier 0: a required check that was cancelled is rerun once per head SHA."""

    SHA = "a" * 40
    CHECKS = (
        CheckView("lint", "CANCELLED", "https://github.com/o/r/actions/runs/11/job/1", "11"),
        CheckView(
            "deploy-preview", "CANCELLED", "https://github.com/o/r/actions/runs/12/job/1", "12"
        ),
        CheckView("unit", "SUCCESS", "https://github.com/o/r/actions/runs/13/job/1", "13"),
    )

    def test_only_required_cancelled_checks_are_picked(self) -> None:
        picked = cancelled_targets(self.CHECKS, {"lint", "unit"}, {}, self.SHA)
        self.assertEqual([item.name for item in picked], ["lint"])

    def test_once_per_head_sha_and_again_on_a_new_head(self) -> None:
        done = {"cancel_reran": {"sha": self.SHA, "names": ["lint"]}}
        self.assertEqual(cancelled_targets(self.CHECKS, {"lint"}, done, self.SHA), [])
        self.assertEqual(
            [i.name for i in cancelled_targets(self.CHECKS, {"lint"}, done, "b" * 40)], ["lint"]
        )

    def test_decide_reruns_before_treating_the_pull_request_as_failing(self) -> None:
        assert CFG is not None
        pull = _pull(checks=self.CHECKS, head_sha=self.SHA)
        first = decide_pull(
            pull=pull,
            outcome=Outcome("held", "checks not passing"),
            now=NOW,
            config=CFG,
            required_cancelled=frozenset({"lint"}),
        )
        self.assertEqual(first.rerun_run_ids, ("11",))
        self.assertFalse(first.escalate)
        self.assertIsNotNone(first.sticky)
        state = parse_state(first.sticky or "")
        self.assertEqual(state["cancel_reran"], {"sha": self.SHA, "names": ["lint"]})
        # Per-SHA tracking must not use up the once-per-pull-request name list.
        self.assertNotIn("lint", state.get("reran") or [])
        # The next run reads that sticky comment back and does not rerun again.
        again = decide_pull(
            pull=_pull(
                checks=self.CHECKS,
                head_sha=self.SHA,
                comments=(_comment(first.sticky or "", "github-actions[bot]", NOW),),
            ),
            outcome=Outcome("held", "checks not passing"),
            now=NOW + timedelta(minutes=30),
            config=CFG,
            required_cancelled=frozenset({"lint"}),
        )
        self.assertEqual(again.rerun_run_ids, ())

    def test_nothing_required_cancelled_changes_nothing(self) -> None:
        assert CFG is not None
        decision = decide_pull(
            pull=_pull(checks=self.CHECKS, head_sha=self.SHA),
            outcome=Outcome("landed", ""),
            now=NOW,
            config=CFG,
        )
        self.assertEqual(decision.rerun_run_ids, ())


class RerunAndScopeTest(unittest.TestCase):
    def test_rerun_is_only_the_stale_check_and_only_once(self) -> None:
        checks = (
            CheckView(
                "PR batching check", "FAILURE", "https://github.com/o/r/actions/runs/99/job/7", "99"
            ),
            CheckView(
                "Backend unit tests",
                "FAILURE",
                "https://github.com/o/r/actions/runs/50/job/1",
                "50",
            ),
            CheckView("PR batching check", "SUCCESS", "", ""),
        )
        assert CFG is not None
        picked = rerun_targets(checks, CFG.rerun_check_names, already=set())
        # A second failure is real and escalates.
        self.assertEqual(
            [item.name for item in picked], ["PR batching check", "Backend unit tests"]
        )
        self.assertEqual(picked[0].run_id, "99")
        again = rerun_targets(
            checks,
            CFG.rerun_check_names,
            already={"PR batching check", "Backend unit tests"},
        )
        self.assertEqual(again, [])
        self.assertEqual(run_id_from_url(checks[0].url), "99")

    def test_a_pull_request_event_reads_one_pull_request(self) -> None:
        self.assertEqual(pull_read_plan("pull_request", 42), "view")
        self.assertEqual(pull_read_plan("issue_comment", 42), "view")
        self.assertEqual(pull_read_plan("pull_request_review", 42), "view")
        self.assertEqual(pull_read_plan("schedule", None), "list")
        self.assertEqual(pull_read_plan("workflow_run", None), "list")
        self.assertEqual(pull_read_plan("workflow_dispatch", None), "list")

    def test_decide_reruns_before_it_escalates(self) -> None:
        assert CFG is not None
        pull = _pull(
            checks=(
                CheckView(
                    "PR batching check",
                    "FAILURE",
                    "https://github.com/o/r/actions/runs/99/job/7",
                    "99",
                ),
            )
        )
        first = decide_pull(
            pull=pull,
            outcome=Outcome("escalated", "checks not green"),
            now=NOW,
            config=CFG,
        )
        self.assertEqual(first.rerun_run_ids, ("99",))
        self.assertFalse(first.escalate)
        self.assertEqual(first.check["conclusion"], "neutral")
        self.assertIn("batching", first.check["output"]["summary"].lower())
        self.assertNotIn(MARKER, first.fresh or "")
        second = decide_pull(
            pull=_pull(
                comments=(_comment(first.sticky or "", "github-actions[bot]", NOW, 7),),
                checks=pull.checks,
            ),
            outcome=Outcome("escalated", "checks not green"),
            now=NOW + timedelta(minutes=30),
            config=CFG,
        )
        self.assertEqual(second.rerun_run_ids, ())
        self.assertTrue(second.escalate)
        self.assertTrue(needs_owner("escalated", "protected:pricing"))
        self.assertTrue(
            needs_owner("skipped", "foreign-claim:do-not-merge", ("agent:working", "do-not-merge"))
        )
        self.assertFalse(
            needs_owner("skipped", "foreign-claim:agent:working", ("agent:working", "do-not-merge"))
        )
        self.assertFalse(needs_owner("landed", ""))

    def test_commands_act_for_the_owner_only(self) -> None:
        plan = command_plan(
            ("merge", ""),
            author="octo-owner",
            number=42,
            repo="o/r",
            state={"options": {"1": "rerun", "2": "hold", "3": "close"}},
        )
        self.assertEqual(plan[0][:3], ["pr", "merge", "42"])
        self.assertIn("--squash", plan[0])
        ignored = command_plan(
            ("merge", ""),
            author="someoneelse",
            number=42,
            repo="o/r",
            state={},
        )
        self.assertEqual(ignored, [])
        option = command_plan(
            ("option", "1"),
            author="octo-owner",
            number=42,
            repo="o/r",
            state={"options": {"1": "rerun"}},
        )
        self.assertEqual(option[0][0], "run")
        self.assertIn("--failed", option[0])
        hold = command_plan(
            ("hold", "waiting on DNS"),
            author="octo-owner",
            number=42,
            repo="o/r",
            state={},
        )
        blob = " ".join(" ".join(item) for item in hold)
        self.assertIn("do-not-merge", blob)
        self.assertIn("Hold: waiting on DNS", blob)
        self.assertIn("Releases: octo-owner", blob)
        close = command_plan(
            ("close", ""),
            author="octo-owner",
            number=42,
            repo="o/r",
            state={},
        )
        self.assertEqual(close[0][:3], ["pr", "close", "42"])


class BackoffAndConfigTest(unittest.TestCase):
    def test_rate_limit_backs_off_then_succeeds(self) -> None:
        calls = {"n": 0}

        def run(_args: list[str]) -> Cmd:
            calls["n"] += 1
            if calls["n"] < 3:
                return Cmd(1, "", "You have exceeded a secondary rate limit. Retry after 1 second.")
            return Cmd(0, "ok", "")

        sleeps: list[float] = []
        client = GhClient(run, sleeps.append)
        result = client(["pr", "view", "42"])
        self.assertEqual(result.stdout, "ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(sleeps), 2)
        self.assertTrue(rate_limited(Cmd(1, "", "HTTP 429")))
        self.assertGreater(retry_seconds("HTTP 429", 1), retry_seconds("HTTP 429", 0))

    def test_config_and_workflow_wire_the_reminder(self) -> None:
        assert CFG is not None
        self.assertEqual(CFG.assignee, OWNER)
        self.assertEqual(ASSIGNEE, OWNER)
        self.assertEqual(ISSUE_TITLE, f"RIVET: needs {OWNER}")
        self.assertEqual(MARKER, "<!-- rivet-needs-owner -->")
        self.assertEqual(CFG.remind_after_hours, 24)
        self.assertEqual(CFG.urgent_remind_after_hours, 4)
        self.assertIn("PR batching check", CFG.rerun_check_names)
        self.assertEqual(CFG.check_name, "RIVET")
        text = (ACTION / "release-lander.example.toml").read_text()
        self.assertNotIn("sk_", text)
        self.assertNotIn("ghp_", text)
        action = (ACTION / "action.yml").read_text()
        script = (ACTION / "scripts" / "rivet_remind.py").read_text()
        for event in ("issue_comment", "pull_request_review", "workflow_dispatch"):
            self.assertIn(event, action)
        self.assertIn("rivet_remind.py", action)
        self.assertNotIn("gh pr merge", action)
        self.assertIn("run_actor", script)
        self.assertIn("if focus is None", script)
        self.assertIn('["pr", "view"', script)
        self.assertIn("list_open", script)
        self.assertNotIn("claim-labels.json", script)


class ActAndOwnerTest(unittest.TestCase):
    def test_report_only_strips_landing_from_the_config(self) -> None:
        source = (ACTION / "scripts" / "rivet_remind.py").read_text(encoding="utf-8")
        self.assertIn('act = "--act" in argv', source)
        self.assertIn("dataclasses.replace(lander, auto_allowed=())", source)
        self.assertIn("act=act", source)

    def test_no_owner_means_no_commands(self) -> None:
        import rivet_remind

        try:
            configure(owner="")
            self.assertFalse(author_allowed(""))
            self.assertFalse(author_allowed("anyone"))
            self.assertEqual(rivet_remind.ISSUE_TITLE, "RIVET: needs owner")
        finally:
            assert CFG is not None
            configure(owner=CFG.assignee, issue_title=CFG.issue_title, marker=CFG.sticky_marker)

    def test_owner_input_overrides_the_config_and_names_the_issue(self) -> None:
        import rivet_remind

        path = Path(tempfile.mkdtemp()) / "c.toml"
        path.write_text('[reminders]\nassignee = "someone-else"\n')
        config = load_reminder_config(path, "octo-owner")
        self.assertEqual(config.assignee, "octo-owner")
        try:
            configure(owner=config.assignee)
            self.assertEqual(rivet_remind.ISSUE_TITLE, "RIVET: needs octo-owner")
            self.assertTrue(author_allowed("Octo-Owner"))
        finally:
            assert CFG is not None
            configure(owner=CFG.assignee, issue_title=CFG.issue_title, marker=CFG.sticky_marker)


def _am(event: str, login: str, kind: str = "User") -> dict:
    return {"event": event, "actor": {"login": login, "type": kind}}


class AutoMergeTurnedOffTest(unittest.TestCase):
    """A person turning auto-merge off holds the pull request (the lander once merged one anyway)."""

    def test_the_2975_timeline_is_held(self) -> None:
        timeline = [
            _am("auto_merge_enabled", "octo-owner"),
            {
                "event": "labeled",
                "label": {"name": "rivet:waiting"},
                "actor": {"login": "github-actions[bot]", "type": "Bot"},
            },
            _am("auto_merge_disabled", "octo-owner"),
            {
                "event": "labeled",
                "label": {"name": "rivet:claimed"},
                "actor": {"login": "github-actions[bot]", "type": "Bot"},
            },
        ]
        self.assertEqual(auto_merge_turned_off_by(timeline), "octo-owner")

    def test_turning_it_back_on_releases_the_hold(self) -> None:
        timeline = [
            _am("auto_merge_disabled", "octo-owner"),
            _am("auto_merge_enabled", "octo-owner"),
        ]
        self.assertEqual(auto_merge_turned_off_by(timeline), "")

    def test_never_armed_is_not_a_hold(self) -> None:
        self.assertEqual(auto_merge_turned_off_by([]), "")
        self.assertEqual(auto_merge_turned_off_by([{"event": "labeled"}]), "")

    def test_armed_is_not_a_hold(self) -> None:
        self.assertEqual(auto_merge_turned_off_by([_am("auto_merge_enabled", "someone")]), "")

    def test_a_bot_turning_it_off_is_not_a_hold(self) -> None:
        self.assertEqual(
            auto_merge_turned_off_by([_am("auto_merge_disabled", "github-actions[bot]", "Bot")]), ""
        )
        self.assertEqual(
            auto_merge_turned_off_by([_am("auto_merge_disabled", "renovate[bot]")]), ""
        )

    def test_any_person_counts_not_only_the_owner(self) -> None:
        self.assertEqual(auto_merge_turned_off_by([_am("auto_merge_disabled", "luke")]), "luke")

    def test_holder_reads_every_timeline_page(self) -> None:
        page1 = json.dumps([_am("auto_merge_enabled", "octo-owner")])
        page2 = json.dumps([_am("auto_merge_disabled", "octo-owner")])
        seen: list[list[str]] = []

        def run(args: list[str]) -> Cmd:
            seen.append(args)
            return Cmd(0, page1 + "\n" + page2, "")

        client = GhClient(run, lambda _s: None)
        self.assertEqual(_auto_merge_holder(client, "o/r", 7), "octo-owner")
        self.assertIn("--paginate", seen[0])

    def test_holder_fails_closed_when_the_timeline_is_unreadable(self) -> None:
        client = GhClient(lambda _a: Cmd(1, "", "boom"), lambda _s: None)
        self.assertTrue(_auto_merge_holder(client, "o/r", 7))

    def test_run_loop_holds_back_before_the_lander_acts(self) -> None:
        source = (ACTION / "scripts" / "rivet_remind.py").read_text(encoding="utf-8")
        hold = source.index("holder = _auto_merge_holder(")
        act = source.index("acted = _act(")
        self.assertLess(hold, act)



class RerunFallbackTest(unittest.TestCase):
    def test_a_refused_failed_jobs_rerun_falls_back_to_a_whole_run(self) -> None:
        calls: list[list[str]] = []

        def client(args: list[str]) -> Cmd:
            calls.append(args)
            if args[:2] == ["run", "rerun"] and "--failed" in args:
                return Cmd(1, "", "no failed jobs")
            return Cmd(0, "", "")

        decision = Decision(None, None, ("42",), False, None, (), ())
        _apply_decision(client, "o/r", _pull(), decision, CFG)
        reruns = [c for c in calls if c[:2] == ["run", "rerun"]]
        self.assertEqual(reruns, [["run", "rerun", "42", "--repo", "o/r", "--failed"], ["run", "rerun", "42", "--repo", "o/r"]])


if __name__ == "__main__":
    unittest.main()


class EmptyMarkerTest(unittest.TestCase):
    """2026-11-20: configs with no [reminders] got an empty issue title (issue creation
    failed) and an empty sticky marker, which matched EVERY comment, so RIVET overwrote
    other people's comments in three repos."""

    def test_missing_reminders_get_a_title_and_a_marker(self) -> None:
        from rivet_remind import load_reminder_config

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lander.toml"
            path.write_text('mode = "warn"\n', encoding="utf-8")
            cfg = load_reminder_config(path, owner="someone")
        self.assertEqual(cfg.issue_title, "RIVET: needs someone")
        self.assertEqual(cfg.sticky_marker, "<!-- rivet-needs-owner -->")

    def test_an_empty_marker_is_refused(self) -> None:
        from rivet_remind import _post_or_update_comment

        with self.assertRaises(ValueError):
            _post_or_update_comment(lambda args: Cmd(0, "[]", ""), "o/r", 1, "body", "")

    def test_a_comment_without_the_marker_is_never_edited(self) -> None:
        from rivet_remind import _post_or_update_comment

        calls: list[list[str]] = []
        human = [{"id": 7, "body": "a person's review comment", "user": {"login": "someone"}}]

        def client(args: list[str]) -> Cmd:
            calls.append(list(args))
            if args[:1] == ["api"] and "comments?per_page" in args[1]:
                return Cmd(0, json.dumps(human), "")
            return Cmd(0, "{}", "")

        _post_or_update_comment(client, "o/r", 1, "<!-- rivet-needs-owner -->\nheld", "<!-- rivet-needs-owner -->")
        self.assertFalse(any("PATCH" in c and any("/comments/7" in part for part in c) for c in calls), calls)
