# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""RIVET's note on a pull request stays true (the wiring of actions/rivet-land/scripts/rivet_note.py).

#2022's note said "RIVET is handling this" for a week while nothing handled it. Acceptance:

- NT-1: facts come from the forge: each failing check carries its own job link, a check
  superseded by a newer run is not failing, and only a fix-attempt branch or a bot's
  merge-from-main commit counts as something RIVET did.
- NT-2: a note whose verdict key is unchanged is rewritten once it is two hours old, and
  left alone before that.
- NT-3: a note on a pull request that has since merged is rewritten once to say so.
- NT-4: with no evidence of action, a "RIVET is handling" pull request is written as stalled.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom.extensions.builtins.release.lander import verdict as V

NOW = datetime(2026, 11, 23, 21, 0, tzinfo=UTC)
REPO = "o/r"


def _script():
    root = Path(__file__).resolve()
    while not (root / ".github").is_dir():
        root = root.parent
    spec = importlib.util.spec_from_file_location(
        "rivet_comment_nt", root / ".github" / "scripts" / "rivet_warn_comment.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _iso(delta_hours: float) -> str:
    return (NOW - timedelta(hours=delta_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeForge:
    """Answers the `gh` calls the script makes."""

    def __init__(self, *, view, refs=(), commits=None, note=None, closed=()):
        self.view, self.refs, self.commits = view, list(refs), commits or {}
        self.note, self.closed, self.writes = note, list(closed), []

    def __call__(self, args, *, payload=None):
        joined = " ".join(args)
        if args[:2] == ["pr", "view"]:
            return json.dumps(self.view)
        if args[:2] == ["pr", "list"]:
            return json.dumps([{"number": n} for n in self.closed])
        if "matching-refs" in joined:
            return json.dumps(self.refs)
        if "/commits/" in joined:
            sha = joined.rsplit("/commits/", 1)[1].split()[0]
            return json.dumps({"commit": {"committer": {"date": self.commits[sha]}}})
        if "--paginate" in args:  # existing note lookup
            return json.dumps(self.note) if self.note else ""
        if "--method" in args:
            self.writes.append((args, payload))
            return ""
        raise AssertionError(f"unexpected gh call: {args}")


RED_RUN = {
    "name": "Unit Tests (3.13)", "workflowName": "CI", "status": "COMPLETED", "conclusion": "FAILURE",
    "completedAt": _iso(30), "startedAt": _iso(31), "detailsUrl": f"https://github.com/{REPO}/actions/runs/5/job/6",
}


def _view(**kw):
    base = {"state": "OPEN", "statusCheckRollup": [RED_RUN], "commits": [], "mergedAt": None, "mergedBy": None, "mergeCommit": None}
    base.update(kw)
    return base


def _use(monkeypatch, mod, forge):
    monkeypatch.setattr(mod, "_gh", forge)
    monkeypatch.setattr(mod, "_utcnow", lambda: NOW)
    monkeypatch.setattr(mod, "charts_for", lambda *a, **k: "")


def test_nt1_failing_checks_carry_their_own_job_link_and_only_real_actions_count(monkeypatch):
    mod = _script()
    newer_green = {**RED_RUN, "conclusion": "SUCCESS", "completedAt": _iso(1), "startedAt": _iso(2), "name": "Lint"}
    older_cancelled = {**RED_RUN, "conclusion": "CANCELLED", "completedAt": _iso(10), "name": "Lint", "detailsUrl": "https://x/old"}
    human = {"authors": [{"login": "ben"}], "messageHeadline": "Merge branch 'main' into x", "committedDate": _iso(1), "oid": "h1"}
    bot = {"authors": [{"login": "rivet[bot]"}], "messageHeadline": "Merge branch 'main' into x", "committedDate": _iso(5), "oid": "b1"}
    forge = FakeForge(
        view=_view(statusCheckRollup=[RED_RUN, older_cancelled, newer_green], commits=[human, bot]),
        refs=[{"ref": "refs/heads/rivet-fix/7-abc", "object": {"sha": "s1"}}],
        commits={"s1": _iso(8)},
    )
    _use(monkeypatch, mod, forge)
    facts = mod.facts_for(REPO, "7", NOW)
    assert [c.name for c in facts.failing] == ["Unit Tests (3.13)"]
    assert facts.failing[0].url.endswith("/actions/runs/5/job/6")
    assert facts.last_action.what == "updated the branch from main"  # the bot's, 5 h ago; the human's 1 h does not count
    assert facts.last_action.at == NOW - timedelta(hours=5)


def test_nt4_a_handling_pull_request_with_no_action_is_written_as_stalled(monkeypatch):
    mod = _script()
    forge = FakeForge(view=_view())
    _use(monkeypatch, mod, forge)
    facts = mod.facts_for(REPO, "7", NOW)
    verdict = V.verdict_for({"rivet_handling": [{"number": 7, "title": "t", "reasons": ["failing_checks"]}]}, 7)
    body = mod.comment_body("", verdict=verdict, facts=facts)
    assert "nothing is repairing it" in body
    assert "is handling" not in body.lower()
    assert "as of 2026-11-23 21:00 UTC" in body


REPORT = {"rivet_handling": [{"number": 7, "title": "t", "reasons": ["failing_checks"]}]}


def _existing(key, hours_old):
    return {"id": 99, "body": f"<!-- rivet-lander:warn-only key={key} -->\nold", "updated_at": _iso(hours_old)}


def _key_now(mod, forge, monkeypatch):
    _use(monkeypatch, mod, forge)
    facts = mod.facts_for(REPO, "7", NOW)
    verdict = V.verdict_for(REPORT, 7)
    return mod._note_key(verdict, facts, mod._note.honest_state(verdict.state, facts))


def test_nt2_an_unchanged_note_is_rewritten_only_once_it_is_two_hours_old(monkeypatch):
    mod = _script()
    key = _key_now(mod, FakeForge(view=_view()), monkeypatch)
    fresh = FakeForge(view=_view(), note=_existing(key, 0.5))
    _use(monkeypatch, mod, fresh)
    mod.refresh(REPO, REPORT)
    assert fresh.writes == []
    old = FakeForge(view=_view(), note=_existing(key, 3))
    _use(monkeypatch, mod, old)
    mod.refresh(REPO, REPORT)
    assert len(old.writes) == 1 and "as of 2026-11-23 21:00 UTC" in old.writes[0][1]["body"]


def test_nt3_a_merged_pull_request_note_is_finished_once(monkeypatch):
    mod = _script()
    merged = _view(state="MERGED", mergedAt=_iso(1), mergedBy={"login": "ben"}, mergeCommit={"oid": "abc1234def"})
    forge = FakeForge(view=merged, note=_existing("old-key", 90), closed=[12])
    _use(monkeypatch, mod, forge)
    mod.refresh(REPO, {"ready": [], "needs_human": []})
    assert len(forge.writes) == 1
    body = forge.writes[0][1]["body"]
    assert "Merged" in body and "key=final-merged" in body
    # a second pass finds it final and writes nothing
    again = FakeForge(view=merged, note={"id": 99, "body": body, "updated_at": _iso(0)}, closed=[12])
    _use(monkeypatch, mod, again)
    mod.refresh(REPO, {"ready": [], "needs_human": []})
    assert again.writes == []
