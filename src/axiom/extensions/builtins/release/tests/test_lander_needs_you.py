# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A pull request held for a person is in that person's Needs you (#2246, item 3).

Five held pull requests sat for five days because the only place that said so was
GitHub, and nobody looked. Needs you is the one queue every surface reads, so a
held pull request belongs there, for the person who has to act and nobody else.

- NY-1: the heartbeat records each watched repo's held pull requests, read-only:
  only those a person must act on, never drafts, and nothing about the rest.
- NY-2: a recorded pull request is in the Needs you of the person who acts (the
  requested reviewer, else an approver), under To approve, linking to GitHub,
  saying why; anyone else sees nothing.
- NY-3: a person matches a GitHub login by principal (``@login:github``) or by an
  alias on the node's identity; the local terminal caller is the node's owner.
- NY-4: a record older than a day is not shown, so a stopped heartbeat cannot
  leave stale asks; and a refresh within fifteen minutes is skipped.
- NY-5: needs_you pools these items, and a failing read leaves the rest intact.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from axiom.extensions.builtins.release.lander import attention as A


def _view(number, reasons=(), *, title=None):
    return {
        "number": number,
        "title": title or f"change {number}",
        "head_sha": "s",
        "head_ref": "b",
        "checks": "passing",
        "approval": "",
        "reasons": list(reasons),
        "repair": "",
        "reviewer": "",
        "tier": 2,
    }


def _report(**sections):
    base = {
        "repo": "o/r",
        "ready": [],
        "needs_human": [],
        "waiting_on_ci": [],
        "rivet_handling": [],
        "with_author": [],
        "superseded": [],
    }
    base.update(sections)
    return base


NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _record(tmp_path, report, *, approvers=("@ada:github",), at=NOW):
    A.record(tmp_path, "o/r", report, approvers=approvers, now=at)


# -- NY-1 -----------------------------------------------------------------------------


def test_ny1_only_pull_requests_a_person_acts_on_are_recorded(tmp_path):
    report = _report(
        needs_human=[_view(1, ["dependency"]), _view(2, ["dependency", "draft"])],
        ready=[_view(3)],
        with_author=[_view(4, ["draft"])],
        rivet_handling=[_view(5, ["failing_checks"])],
    )
    _record(tmp_path, report)
    saved = json.loads(A.record_path(tmp_path, "o/r").read_text(encoding="utf-8"))
    assert [item["number"] for item in saved["items"]] == [1]
    assert saved["items"][0]["url"] == "https://github.com/o/r/pull/1"


def test_ny1_the_authors_own_step_is_not_queued_for_anyone(tmp_path):
    # Found live: a pull request held only for a conflict was listed for every approver.
    _record(
        tmp_path,
        _report(needs_human=[_view(6, ["conflict"]), _view(16, ["changes requested by bo"])]),
    )
    saved = json.loads(A.record_path(tmp_path, "o/r").read_text(encoding="utf-8"))
    assert saved["items"] == []


# -- NY-2 -----------------------------------------------------------------------------


def test_ny2_the_approver_sees_it_under_to_approve_and_others_do_not(tmp_path):
    _record(tmp_path, _report(needs_human=[_view(7, ["dependency"], title="bump npm")]))
    mine = A.for_person("@ada:github", state_dir=tmp_path, now=NOW)
    assert len(mine) == 1
    item = mine[0]
    assert item["section"] == "approve"
    assert item["source"] == "rivet"
    assert item["href"] == "https://github.com/o/r/pull/7"
    assert "#7" in item["title"] and "bump npm" in item["title"]
    assert "dependency files" in item["reason"]
    assert A.for_person("@bo:github", state_dir=tmp_path, now=NOW) == []


def test_ny2_a_requested_reviewer_is_who_acts_not_every_approver(tmp_path):
    _record(tmp_path, _report(needs_human=[_view(8, ["dependency", "waiting on bo"])]))
    mine = A.for_person("@bo:github", state_dir=tmp_path, now=NOW)
    assert [i["id"] for i in mine] == ["rivet:o/r#8"]
    assert mine[0]["title"].startswith("Review #8")
    assert A.for_person("@ada:github", state_dir=tmp_path, now=NOW) == []


# -- NY-3 -----------------------------------------------------------------------------


def test_ny3_logins_come_from_the_principal_or_the_owners_identity_aliases():
    identity = {
        "owner": "pat@example.org",
        "aliases": ["pat@sso.example.org", "@pat-gh:github", "@pat2:github"],
    }
    assert A.logins_for("@ada:github", identity=identity) == {"ada"}
    assert A.logins_for("pat@example.org", identity=identity) == {"pat-gh", "pat2"}
    assert A.logins_for("pat@sso.example.org", identity=identity) == {"pat-gh", "pat2"}
    assert A.logins_for("@cli:local", identity=identity) == {"pat-gh", "pat2"}
    assert A.logins_for("someone@example.org", identity=identity) == set()


def test_ny3_an_email_like_github_alias_is_not_a_login():
    # "login@github.com" reads as an email address that does not exist; the principal form is the one.
    identity = {"owner": "pat@example.org", "aliases": ["pat@github.com", "pat@github"]}
    assert A.logins_for("pat@example.org", identity=identity) == set()
    assert A.logins_for("pat@github.com", identity=identity) == set()


# -- NY-4 -----------------------------------------------------------------------------


def test_ny4_a_day_old_record_is_not_shown(tmp_path):
    _record(tmp_path, _report(needs_human=[_view(9, ["dependency"])]), at=NOW - timedelta(hours=25))
    assert A.for_person("@ada:github", state_dir=tmp_path, now=NOW) == []


def test_ny4_a_fresh_record_is_not_refreshed(tmp_path):
    _record(tmp_path, _report(), at=NOW - timedelta(minutes=5))
    assert A.due(tmp_path, "o/r", now=NOW) is False
    assert A.due(tmp_path, "o/r", now=NOW + timedelta(minutes=20)) is True
    assert A.due(tmp_path, "x/never", now=NOW) is True


# -- NY-5 -----------------------------------------------------------------------------


def test_ny5_needs_you_pools_rivet_items_and_survives_a_failure(monkeypatch, tmp_path):
    from axiom.extensions.builtins.notifications import needs_you as N

    class Empty:
        def query(self, _q):
            return []

    item = {
        "id": "rivet:o/r#1",
        "section": "approve",
        "kind": "pull_request",
        "title": "Approve #1",
        "source": "rivet",
        "due": None,
        "priority": "normal",
        "href": "u",
        "action": None,
        "created_at": None,
    }
    monkeypatch.setattr(A, "for_person", lambda person, **_: [item] if person == "p" else [])
    got = N.needs_you(
        "p", inbox_store=Empty(), approvals_path=tmp_path / "none.json", due_provider=lambda *_: []
    )
    approve = next(s for s in got["sections"] if s["id"] == "approve")
    assert item in approve["items"]

    def boom(person, **_):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(A, "for_person", boom)
    got = N.needs_you(
        "p", inbox_store=Empty(), approvals_path=tmp_path / "none.json", due_provider=lambda *_: []
    )
    assert got["count"] == len(got["items"])


def test_record_path_is_inside_the_state_dir(tmp_path):
    assert Path(A.record_path(tmp_path, "o/r")).is_relative_to(tmp_path)
