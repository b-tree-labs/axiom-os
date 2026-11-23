"""RIVET explains a batching-gate hold and acts on it.

A batching gate fails a pull request because OLDER open pull requests change
the same code. Rerunning it changes nothing while they are open, so the generic
"rerun failed checks" advice is wrong. RIVET names the older pull requests,
waits without asking when they are landing, asks only when one is stuck, reruns
the gate when one stops counting, and takes ``/rivet exempt <reason>``.
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

import rivet_remind  # noqa: E402
from rivet_remind import (  # noqa: E402
    BatchingHold,
    Blocker,
    CheckView,
    Cmd,
    Outcome,
    PullSnapshot,
    _batching_hold,
    batching_checks,
    batching_reason,
    blocker_numbers,
    blocker_state,
    command_plan,
    configure,
    decide_pull,
    job_id_from_url,
    load_reminder_config,
    parse_command,
    parse_state,
)

OWNER = "octo-owner"
NOW = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
CFG = None

GATE = CheckView(
    "PR batching check", "FAILURE", "https://github.com/o/r/actions/runs/77/job/9001", "77"
)
UNIT_OK = CheckView("unit", "SUCCESS", "https://github.com/o/r/actions/runs/78/job/9002", "78")
UNIT_BAD = CheckView("unit", "FAILURE", "https://github.com/o/r/actions/runs/78/job/9002", "78")
MESSAGE = (
    "Batch it: 2 other open PRs edit the SAME FILES as this one (#31, #35). Fold this "
    "change into one of them, or label batch-exempt with a reason"
)


def setUpModule() -> None:
    global CFG
    text = (ACTION / "release-lander.example.toml").read_text()
    text = text.replace("rerun_check_names = []", 'rerun_check_names = ["PR batching check"]')
    path = Path(tempfile.mkdtemp()) / "release-lander.toml"
    path.write_text(text)
    CFG = load_reminder_config(path, OWNER)
    configure(owner=CFG.assignee, issue_title=CFG.issue_title, marker=CFG.sticky_marker)


def _pull(**kwargs) -> PullSnapshot:
    base = dict(
        number=42,
        title="feat(tower): read-only zone map history",
        body="A read-only diagnostic tool.",
        url="https://github.com/o/r/pull/42",
        labels=(),
        files=("services/tower/tools.py",),
        created_at=NOW - timedelta(hours=3),
        head_sha="abc123",
        author="octo-owner",
        checks=(GATE, UNIT_OK),
        comments=(),
    )
    base.update(kwargs)
    return PullSnapshot(**base)


def _blocker(number: int, state: str) -> Blocker:
    return Blocker(number, f"older change {number}", f"https://github.com/o/r/pull/{number}", state)


def _hold(*blockers: Blocker) -> BatchingHold:
    return BatchingHold(checks=(GATE,), blockers=tuple(blockers))


def _decide(pull: PullSnapshot, hold: BatchingHold):
    assert CFG is not None
    return decide_pull(
        pull=pull,
        outcome=Outcome("escalated", "batching_gate"),
        now=NOW,
        config=CFG,
        batching=hold,
    )


def _already_reran(hold_state: dict | None = None) -> tuple:
    """A sticky from an earlier pass: the gate was already rerun once."""
    state = {"reran": ["PR batching check"], "waiting_since": NOW.isoformat()}
    state.update(hold_state or {})
    body = f"{rivet_remind.MARKER}\n{rivet_remind.encode_state(state)}\n"
    return (rivet_remind.CommentView(body, "github-actions[bot]", NOW - timedelta(hours=1), 5),)


class ReadTheGateTest(unittest.TestCase):
    def test_a_hold_is_only_the_batching_gate_failing(self) -> None:
        self.assertEqual(batching_checks((GATE, UNIT_OK), ("batching",)), (GATE,))
        # A real test failure beside the gate is not a batching hold.
        self.assertEqual(batching_checks((GATE, UNIT_BAD), ("batching",)), ())
        self.assertEqual(batching_checks((UNIT_OK,), ("batching",)), ())
        self.assertEqual(batching_checks((GATE,), ()), ())

    def test_blockers_come_from_the_gate_annotation(self) -> None:
        self.assertEqual(job_id_from_url(GATE.url), "9001")
        self.assertEqual(job_id_from_url("https://example.test/x"), "")
        self.assertEqual(blocker_numbers([MESSAGE], own=42), (31, 35))
        self.assertEqual(blocker_numbers([MESSAGE, "see #35 and #42"], own=42), (31, 35))
        self.assertEqual(blocker_numbers(["Process completed with exit code 1."], own=42), ())

    def test_a_blocker_is_classified_by_whether_it_can_land(self) -> None:
        row = {"state": "OPEN", "isDraft": False, "mergeable": "MERGEABLE", "labels": []}
        self.assertEqual(blocker_state(row), "landing")
        self.assertEqual(blocker_state({**row, "state": "MERGED"}), "closed")
        self.assertEqual(blocker_state({**row, "isDraft": True}), "draft")
        self.assertEqual(blocker_state({**row, "mergeable": "CONFLICTING"}), "conflicted")
        self.assertEqual(blocker_state({**row, "labels": [{"name": "do-not-merge"}]}), "held")
        failing = {**row, "statusCheckRollup": [{"name": "unit", "conclusion": "FAILURE"}]}
        self.assertEqual(blocker_state(failing), "failing")

    def test_the_reason_names_each_older_pull_request(self) -> None:
        text = batching_reason(_hold(_blocker(31, "conflicted"), _blocker(35, "landing")))
        self.assertIn("#31", text)
        self.assertIn("merge conflicts", text)
        self.assertIn("#35", text)
        self.assertNotIn("batching_gate", text)

    def test_reading_the_hold_asks_github_for_annotations_and_each_blocker(self) -> None:
        calls: list[list[str]] = []

        def client(args: list[str]) -> Cmd:
            calls.append(args)
            if args[0] == "api" and "check-runs/9001/annotations" in args[1]:
                rows = [
                    {"annotation_level": "failure", "message": MESSAGE},
                    {"annotation_level": "warning", "message": "also see #99"},
                ]
                return Cmd(0, json.dumps(rows), "")
            if args[:2] == ["pr", "view"]:
                number = int(args[2])
                row = {
                    "number": number,
                    "title": f"older change {number}",
                    "url": f"https://github.com/o/r/pull/{number}",
                    "state": "OPEN",
                    "isDraft": False,
                    "mergeable": "CONFLICTING" if number == 31 else "MERGEABLE",
                    "labels": [],
                    "statusCheckRollup": [],
                }
                return Cmd(0, json.dumps(row), "")
            return Cmd(1, "", "unexpected")

        hold = _batching_hold(client, "o/r", _pull(), CFG)
        assert hold is not None
        self.assertEqual(
            [(b.number, b.state) for b in hold.blockers], [(31, "conflicted"), (35, "landing")]
        )
        self.assertIsNone(_batching_hold(client, "o/r", _pull(checks=(GATE, UNIT_BAD)), CFG))

    def test_an_unreadable_annotation_still_gives_a_hold_with_no_names(self) -> None:
        hold = _batching_hold(lambda args: Cmd(1, "", "HTTP 403"), "o/r", _pull(), CFG)
        assert hold is not None
        self.assertEqual(hold.blockers, ())


class DecideTest(unittest.TestCase):
    def test_older_pull_requests_that_are_landing_do_not_ask_the_owner(self) -> None:
        pull = _pull(comments=_already_reran())
        decision = _decide(pull, _hold(_blocker(31, "landing"), _blocker(35, "landing")))
        self.assertFalse(decision.escalate)
        self.assertIsNone(decision.fresh)
        self.assertEqual(decision.rerun_run_ids, ())
        self.assertEqual(decision.add_labels, ())
        self.assertIn(CFG.waiting_label, decision.remove_labels)
        body = decision.sticky or ""
        self.assertIn("#31", body)
        self.assertIn("#35", body)
        self.assertNotIn("needs a decision", body)
        self.assertNotIn("Rerun failed checks", body)

    def test_a_stuck_older_pull_request_asks_and_recommends_the_exemption(self) -> None:
        pull = _pull(comments=_already_reran())
        decision = _decide(pull, _hold(_blocker(31, "conflicted"), _blocker(35, "landing")))
        self.assertTrue(decision.escalate)
        self.assertIn(CFG.waiting_label, decision.add_labels)
        body = decision.sticky or ""
        self.assertIn("needs a decision", body)
        self.assertIn("/rivet exempt <reason>", body)
        recommendation = next(line for line in body.splitlines() if "Recommendation" in line)
        self.assertIn("Exempt", recommendation)
        self.assertIn("#31", recommendation)
        self.assertEqual(parse_state(body)["options"], {"1": "rerun", "2": "exempt", "3": "close"})

    def test_no_named_blocker_asks_the_owner(self) -> None:
        decision = _decide(_pull(comments=_already_reran()), _hold())
        self.assertTrue(decision.escalate)
        self.assertIn("names no pull request", decision.sticky or "")

    def test_the_gate_is_rerun_when_a_blocker_stops_counting(self) -> None:
        seen = {"batch_seen": {"31": "open", "35": "open"}}
        pull = _pull(comments=_already_reran(seen))
        # Same picture as last time: no rerun, however often the lander looks.
        same = _decide(pull, _hold(_blocker(31, "landing"), _blocker(35, "failing")))
        self.assertEqual(same.rerun_run_ids, ())
        # #31 merged: the gate result is stale, so rerun it once.
        changed = _decide(pull, _hold(_blocker(31, "closed"), _blocker(35, "landing")))
        self.assertEqual(changed.rerun_run_ids, ("77",))
        self.assertFalse(changed.escalate)
        after = parse_state(changed.sticky or "")
        self.assertEqual(after["batch_seen"], {"31": "closed", "35": "open"})
        again = _decide(
            _pull(comments=_already_reran({"batch_seen": after["batch_seen"]})),
            _hold(_blocker(31, "closed"), _blocker(35, "landing")),
        )
        self.assertEqual(again.rerun_run_ids, ())

    def test_the_first_look_records_what_it_saw_without_a_second_rerun(self) -> None:
        decision = _decide(_pull(comments=_already_reran()), _hold(_blocker(31, "landing")))
        self.assertEqual(decision.rerun_run_ids, ())
        self.assertEqual(parse_state(decision.sticky or "")["batch_seen"], {"31": "open"})

    def test_without_a_batching_hold_the_old_decision_is_unchanged(self) -> None:
        assert CFG is not None
        pull = _pull(comments=_already_reran(), checks=(UNIT_BAD,))
        decision = decide_pull(
            pull=pull, outcome=Outcome("escalated", "test failure"), now=NOW, config=CFG
        )
        self.assertTrue(decision.escalate)
        self.assertIn("Rerun failed checks", decision.sticky or "")
        self.assertNotIn("/rivet exempt", decision.sticky or "")


class ExemptCommandTest(unittest.TestCase):
    def test_exempt_takes_a_reason(self) -> None:
        self.assertEqual(
            parse_command("/rivet exempt independent read-only tool"),
            ("exempt", "independent read-only tool"),
        )
        self.assertEqual(parse_command("/rivet exempt"), ("exempt", ""))

    def test_exempt_labels_records_the_reason_and_reruns_the_gate(self) -> None:
        plan = command_plan(
            ("exempt", "independent read-only tool"),
            author=OWNER,
            number=42,
            repo="o/r",
            state={},
            gate_run_ids=("77",),
        )
        self.assertEqual(
            plan[0], ["pr", "edit", "42", "--repo", "o/r", "--add-label", "batch-exempt"]
        )
        self.assertEqual(plan[1][:3], ["pr", "comment", "42"])
        self.assertIn("Batch exemption: independent read-only tool", plan[1][-1])
        self.assertIn(OWNER, plan[1][-1])
        self.assertEqual(plan[2], ["run", "rerun", "77", "--repo", "o/r", "--failed"])

    def test_exempt_without_a_reason_only_asks_for_one(self) -> None:
        for command, state in (
            (("exempt", ""), {}),
            (("option", "2"), {"options": {"2": "exempt"}}),
        ):
            plan = command_plan(
                command, author=OWNER, number=42, repo="o/r", state=state, gate_run_ids=("77",)
            )
            self.assertEqual(len(plan), 1)
            self.assertEqual(plan[0][:3], ["pr", "comment", "42"])
            self.assertIn("needs a reason", plan[0][-1])

    def test_exempt_is_the_owners_alone_and_uses_the_configured_label(self) -> None:
        other = command_plan(("exempt", "x"), author="someoneelse", number=42, repo="o/r", state={})
        self.assertEqual(other, [])
        plan = command_plan(
            ("exempt", "x"),
            author=OWNER,
            number=42,
            repo="o/r",
            state={},
            exempt_label="ci:batch-ok",
        )
        self.assertIn("ci:batch-ok", plan[0])

    def test_config_defaults(self) -> None:
        assert CFG is not None
        self.assertEqual(CFG.batching_check_names, ("batching",))
        self.assertEqual(CFG.batch_exempt_label, "batch-exempt")


if __name__ == "__main__":
    unittest.main()
