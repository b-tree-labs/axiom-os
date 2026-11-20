# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Public-mirror issue closure: our comment, nobody else's text."""

from __future__ import annotations

import io
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / ".github" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import mirror_public_issues as mpi  # noqa: E402

PUBLIC = "b-tree-labs/axiom-os"
PRIVATE = "source/repo"
SECRET = "SECRET TITLE do not leak https://source/repo/issues/7"


def _source(**overrides) -> dict:
    item = {
        "number": 7,
        "state": "closed",
        "state_reason": "completed",
        "labels": [{"name": "from-public"}],
        "body": f"<!-- public: {PUBLIC}#10 -->\n\n{SECRET}",
        "closed_at": "2026-10-20T00:00:00Z",
    }
    item.update(overrides)
    return item


def _release(tag: str, published: str, draft: bool = False) -> dict:
    return {"tag_name": tag, "published_at": published, "draft": draft}


def _cfg(**overrides) -> mpi.CloseConfig:
    cfg = mpi.CloseConfig(
        private_repo=PRIVATE,
        public_repo=PUBLIC,
        dry_run=False,
        forced_version="",
        release_tag="",
        release_published_at="",
        mirror_run_id="",
        mirror_attempts=1,
        mirror_pause=0,
    )
    return mpi.CloseConfig(**{**cfg.__dict__, **overrides})


class FakeGitHub:
    def __init__(self, token: str = "token"):
        self.token = token
        self.labels: set[str] = set()
        self.created_labels: list[tuple] = []
        self.issues: list[dict] = []
        self.releases: list[dict] = []
        self.public: dict[int, tuple[dict | None, list[dict]]] = {}
        self.comments: list[tuple] = []
        self.states: list[tuple] = []
        self.jobs: list[dict] = []
        self.tracking: list[dict] = []
        self.created_issues: list[dict] = []
        self.updated: list[tuple] = []

    def label_exists(self, repo, name):
        return name in self.labels

    def create_label(self, repo, name, description, color):
        self.created_labels.append((repo, name, description, color))
        self.labels.add(name)

    def list_labeled_issues(self, repo, label, state):
        return list(self.issues)

    def list_releases(self, repo):
        return list(self.releases)

    def get_issue(self, repo, number):
        issue, _comments = self.public.get(number, (None, []))
        return issue

    def list_comments(self, repo, number):
        _issue, comments = self.public.get(number, (None, []))
        return comments

    def post_comment(self, repo, number, body):
        self.comments.append((repo, number, body))

    def set_issue_state(self, repo, number, state, state_reason=None):
        self.states.append((repo, number, state, state_reason))

    def list_run_jobs(self, repo, run_id):
        return self.jobs

    def find_tracking_issues(self, repo):
        return list(self.tracking)

    def create_issue(self, repo, title, body):
        item = {"number": 900, "title": title, "body": body, "state": "open"}
        self.created_issues.append((repo, item))
        self.tracking.append(item)
        return item

    def update_issue(self, repo, number, fields):
        self.updated.append((repo, number, dict(fields)))


def _pair(source_issues, releases, public_issue, comments=None, jobs=None):
    private = FakeGitHub("source-token")
    public = FakeGitHub("mirror-token")
    private.issues = source_issues
    private.releases = releases
    private.jobs = jobs or []
    public.public[10] = (public_issue, comments or [])
    return private, public


def _open_public():
    return {"number": 10, "state": "open"}


def test_comment_is_one_sentence():
    assert mpi.comment_for("v1.2.3") == "Fixed in v1.2.3, thanks for reporting"
    assert mpi.is_fixed_comment("Fixed in v1.2.3, thanks for reporting\n")
    assert not mpi.is_fixed_comment("Fixed in v1.2.3, thanks for reporting\nsee the diff")
    with pytest.raises(ValueError):
        mpi.comment_for("https://source/repo/issues/7")
    with pytest.raises(ValueError):
        mpi.comment_for("v1.2.3\nextra")


def test_marker_is_exact_and_single():
    body = f"preamble\n<!-- public: {PUBLIC}#10 -->\n{SECRET}"
    assert mpi.parse_marker(body, PUBLIC) == mpi.Marker(10, None)
    assert mpi.parse_marker(f"<!--public:{PUBLIC}#10-->", PUBLIC).number == 10
    assert mpi.parse_marker("<!-- public: other/repo#10 -->", PUBLIC).error == "missing marker"
    doubled = f"<!-- public: {PUBLIC}#10 -->\n<!-- public: {PUBLIC}#11 -->"
    assert mpi.parse_marker(doubled, PUBLIC).error == "ambiguous marker"
    dirty = f"<!-- public: {PUBLIC}#10 {SECRET} -->"
    assert mpi.parse_marker(dirty, PUBLIC).error == "missing marker"


def test_version_is_the_earliest_release_after_the_close():
    releases = [
        mpi.Release("v0.70.1", "2026-10-15T00:00:00Z"),
        mpi.Release("v0.70.2", "2026-10-21T00:00:00Z"),
        mpi.Release("not-a-release", "2026-10-20T12:00:00Z"),
        mpi.Release("v0.70.3", "2026-10-22T00:00:00Z"),
    ]
    assert mpi.resolve_version("2026-10-20T00:00:00Z", releases, None) == "v0.70.2"
    assert mpi.resolve_version("2026-10-23T00:00:00Z", releases, None) is None
    assert mpi.resolve_version("2026-10-23T00:00:00Z", releases, "v9.9.9") == "v9.9.9"


def test_close_posts_only_the_sentence_and_closes(capsys):
    private, public = _pair(
        [_source()],
        [_release("v0.70.2", "2026-10-21T15:54:03Z")],
        _open_public(),
    )
    code = mpi.run_close(private, public, _cfg())
    assert code == 0
    assert public.comments == [(PUBLIC, 10, "Fixed in v0.70.2, thanks for reporting")]
    assert public.states == [(PUBLIC, 10, "closed", "completed")]
    assert SECRET not in capsys.readouterr().out
    assert private.created_labels[0][1] == "from-public"
    posted = public.comments[0][2]
    assert "http" not in posted
    assert "#7" not in posted
    assert "\n" not in posted


def test_second_run_does_not_comment_again():
    private, public = _pair(
        [_source()],
        [_release("v0.70.2", "2026-10-21T00:00:00Z")],
        {"number": 10, "state": "closed"},
        [{"body": "Fixed in v0.70.2, thanks for reporting\n"}],
    )
    private.labels.add("from-public")
    code = mpi.run_close(private, public, _cfg())
    assert code == 0
    assert public.comments == []
    assert public.states == []
    assert private.created_labels == []


def test_already_replied_open_issue_is_closed_without_a_second_comment():
    private, public = _pair(
        [_source()],
        [_release("v0.70.2", "2026-10-21T00:00:00Z")],
        _open_public(),
        [{"body": "Fixed in v0.70.1, thanks for reporting"}],
    )
    code = mpi.run_close(private, public, _cfg())
    assert code == 0
    assert public.comments == []
    assert public.states == [(PUBLIC, 10, "closed", "completed")]


def test_dry_run_writes_nothing(capsys):
    private, public = _pair(
        [_source()],
        [_release("v0.70.2", "2026-10-21T00:00:00Z")],
        _open_public(),
    )
    code = mpi.run_close(private, public, _cfg(dry_run=True))
    assert code == 0
    assert public.comments == []
    assert public.states == []
    assert private.created_labels == []
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "Fixed in v0.70.2, thanks for reporting" in out
    assert "would create label from-public" in out
    assert SECRET not in out


def test_not_planned_and_unreleased_are_not_thanked():
    private, public = _pair(
        [
            _source(number=1, state_reason="not_planned"),
            _source(number=2, closed_at="2026-10-22T00:00:00Z"),
        ],
        [_release("v0.70.2", "2026-10-21T00:00:00Z")],
        _open_public(),
    )
    code = mpi.run_close(private, public, _cfg())
    assert code == 0
    assert public.comments == []
    assert public.states == []


def test_release_event_tag_is_used_when_the_api_list_is_stale():
    private, public = _pair([_source()], [], _open_public())
    code = mpi.run_close(
        private,
        public,
        _cfg(release_tag="v0.71.0", release_published_at="2026-10-21T00:00:00Z"),
    )
    assert code == 0
    assert public.comments == [(PUBLIC, 10, "Fixed in v0.71.0, thanks for reporting")]


def test_mirror_run_without_a_push_does_nothing():
    private, public = _pair(
        [_source()],
        [_release("v0.70.2", "2026-10-21T00:00:00Z")],
        _open_public(),
        jobs=[{"status": "completed", "steps": [{"name": "Push to public mirror", "conclusion": "skipped"}]}],
    )
    code = mpi.run_close(private, public, _cfg(mirror_run_id="55"))
    assert code == 0
    assert public.comments == []
    assert private.created_labels == []


def test_mirror_run_with_a_recorded_push_comments():
    private, public = _pair(
        [_source()],
        [_release("v0.70.2", "2026-10-21T00:00:00Z")],
        _open_public(),
        jobs=[{
            "status": "completed",
            "steps": [{"name": mpi.PUSH_RECORDED_STEP, "conclusion": "success"}],
        }],
    )
    code = mpi.run_close(private, public, _cfg(mirror_run_id="55"))
    assert code == 0
    assert public.comments[0][2] == "Fixed in v0.70.2, thanks for reporting"


def test_missing_public_issue_fails_without_posting():
    private, public = _pair(
        [_source()],
        [_release("v0.70.2", "2026-10-21T00:00:00Z")],
        None,
    )
    code = mpi.run_close(private, public, _cfg())
    assert code == 1
    assert public.comments == []


def test_missing_mirror_token_refuses_to_write():
    private, public = _pair(
        [_source()],
        [_release("v0.70.2", "2026-10-21T00:00:00Z")],
        _open_public(),
    )
    public.token = ""
    code = mpi.run_close(private, public, _cfg())
    assert code == 1
    assert public.comments == []
    assert public.states == []


def test_bad_forced_version_writes_nothing():
    private, public = _pair([_source()], [], _open_public())
    code = mpi.run_close(private, public, _cfg(forced_version="https://evil.example/x"))
    assert code == 1
    assert public.comments == []


def test_two_source_issues_for_one_public_issue_comment_once():
    second = _source(number=8, body=f"<!-- public: {PUBLIC}#10 -->\n")
    private, public = _pair(
        [_source(), second],
        [_release("v0.70.2", "2026-10-21T00:00:00Z")],
        _open_public(),
    )
    code = mpi.run_close(private, public, _cfg())
    assert code == 0
    assert len(public.comments) == 1


def test_audit_lists_gaps_without_the_report_text():
    private, public = _pair(
        [
            _source(),
            _source(number=8, body="no marker here, and a pasted script()"),
            _source(number=9, state_reason="not_planned"),
        ],
        [],
        _open_public(),
    )
    code = mpi.run_audit(
        private,
        public,
        mpi.AuditConfig(PRIVATE, PUBLIC, dry_run=False, checked_at="2026-10-23T13:45Z"),
    )
    assert code == 0
    repo, created = private.created_issues[0]
    assert repo == PRIVATE
    body = created["body"]
    assert mpi.AUDIT_MARKER in body
    assert "| #7 | b-tree-labs/axiom-os#10 | no public reply |" in body
    assert "| #8 | — | missing marker |" in body
    assert "closed as not planned, no public reply" in body
    assert "SECRET TITLE" not in body
    assert "script()" not in body
    assert "https://" not in body
    assert public.comments == []


def test_audit_is_quiet_when_the_public_reply_exists_and_updates_in_place():
    private, public = _pair(
        [_source()],
        [],
        {"number": 10, "state": "closed"},
        [{"body": "Fixed in v0.70.2, thanks for reporting"}],
    )
    cfg = mpi.AuditConfig(PRIVATE, PUBLIC, dry_run=False, checked_at="2026-10-23T13:45Z")
    assert mpi.run_audit(private, public, cfg) == 0
    assert private.created_issues == []

    rows = mpi.plan_audit(
        [mpi.private_issue_from_api(_source(number=3, body="nope"))],
        {},
        PUBLIC,
    )
    body = mpi.render_audit(rows, PUBLIC, cfg.checked_at)
    private.tracking = [{"number": 40, "body": "stale", "state": "closed"}]
    private.issues = [_source(number=3, body="nope")]
    assert mpi.run_audit(private, public, cfg) == 0
    assert private.created_issues == []
    assert private.updated[0][0] == PRIVATE
    assert private.updated[0][1] == 40
    assert private.updated[0][2]["state"] == "open"
    assert private.updated[0][2]["body"] == body


def test_audit_dry_run_and_current_issue_do_not_write(capsys):
    private, public = _pair([_source()], [], _open_public())
    cfg = mpi.AuditConfig(PRIVATE, PUBLIC, dry_run=True, checked_at="2026-10-23T13:45Z")
    assert mpi.run_audit(private, public, cfg) == 0
    assert private.created_issues == []
    assert "dry-run would open" in capsys.readouterr().out

    body = mpi.render_audit(
        [mpi.AuditRow(7, 10, "no public reply")],
        PUBLIC,
        cfg.checked_at,
    )
    private.tracking = [{"number": 40, "body": body, "state": "open"}]
    assert mpi.run_audit(private, public, mpi.AuditConfig(PRIVATE, PUBLIC, False, cfg.checked_at)) == 0
    assert private.updated == []


def test_request_does_not_echo_the_token(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=30):
        captured["auth"] = req.get_header("Authorization")
        raise urllib.error.HTTPError(
            req.full_url, 403, "nope", hdrs=None, fp=io.BytesIO(b'{"message":"nope"}')
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = mpi.GitHub("super-secret-token")
    with pytest.raises(mpi.GitHubError) as exc:
        client._request("GET", "/repos/o/r")
    assert "super-secret-token" not in str(exc.value)
    assert captured["auth"] == "Bearer super-secret-token"


def test_next_link_and_token_redaction():
    header = '<https://api.github.com/next?page=2>; rel="next", <https://api.github.com/last>; rel="last"'
    assert mpi._next_link(header) == "https://api.github.com/next?page=2"
    assert mpi._next_link(None) is None
    text = mpi._safe_message(b"token super-secret-token leaked", "super-secret-token")
    assert "super-secret-token" not in text
    assert "[redacted]" in text


def test_pages_follow_the_link_header(monkeypatch):
    pages = {
        "https://api.github.com/repos/o/r/issues?x=1": (
            json.dumps([{"number": 1}]).encode(),
            '<https://api.github.com/page-2>; rel="next"',
        ),
        "https://api.github.com/page-2": (json.dumps([{"number": 2}]).encode(), None),
    }

    class Resp:
        def __init__(self, raw, link):
            self._raw = raw
            self.headers = {"Link": link} if link else {}

        def read(self):
            return self._raw

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(req, timeout=30):
        raw, link = pages[req.full_url]
        return Resp(raw, link)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = mpi.GitHub("t")
    assert client._get_pages("/repos/o/r/issues?x=1") == [{"number": 1}, {"number": 2}]


def test_dry_run_flag():
    assert mpi.dry_run_enabled(False, "1")
    assert mpi.dry_run_enabled(True, "0")
    assert not mpi.dry_run_enabled(False, "0")
    assert not mpi.dry_run_enabled(False, None)
