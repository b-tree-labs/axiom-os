# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LLM leftover classification and tiered GitHub hygiene actions."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from axiom.extensions.builtins.hygiene.classify import classify_candidates
from axiom.extensions.builtins.hygiene.github_reports import notice_body
from axiom.extensions.builtins.hygiene.skills.report import run
from axiom.infra.skills import SkillContext, SkillRegistry


NOW = datetime(2026, 11, 21, 14, 40, tzinfo=timezone.utc)
NOTICED = (NOW - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
STALE = (NOW - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
FRESH = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ctx(tmp_path: Path) -> SkillContext:
    import logging

    return SkillContext(
        registry=SkillRegistry(),
        state_dir=tmp_path,
        logger=logging.getLogger("test"),
        user_prompt=None,
    )


def _pr(number: int, *, title: str, updated: str, user: dict, labels: list | None = None) -> dict:
    return {
        "number": number,
        "draft": True,
        "title": title,
        "updated_at": updated,
        "labels": [{"name": n} for n in (labels or [])],
        "html_url": f"https://example/{number}",
        "user": user,
    }


class FakeGh:
    """Route ``gh api`` calls by method + path and record mutations."""

    def __init__(self, *, pulls=None, comments=None, notifications=None, subjects=None, packages=None, package_error=None):
        self.pulls = pulls or []
        self.comments = comments or {}  # number -> list
        self.notifications = notifications or []
        self.subjects = subjects or {}
        self.packages = packages
        self.package_error = package_error
        self.calls: list[tuple[str, str]] = []
        self.closed: list[int] = []
        self.noticed: list[int] = []
        self.labeled: list[int] = []
        self.marked: list[str] = []

    def __call__(self, args, **kwargs):
        argv = list(args)
        method = "GET"
        if "-X" in argv:
            method = argv[argv.index("-X") + 1]
        path = ""
        for i, part in enumerate(argv):
            if part in {"gh", "api", "-X", "GET", "POST", "PATCH", "PUT", "DELETE"}:
                continue
            if i > 0 and argv[i - 1] == "-X":
                continue
            if part == "-f" or (i > 0 and argv[i - 1] == "-f"):
                continue
            path = part
            break
        self.calls.append((method, path))

        if path.startswith("repos/") and "/pulls?" in path and method == "GET":
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.pulls), stderr="")
        if "/issues/" in path and path.endswith("/comments") and method == "POST":
            num = int(path.split("/issues/")[1].split("/")[0])
            self.noticed.append(num)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if "/issues/" in path and "/comments" in path and method == "GET":
            num = int(path.split("/issues/")[1].split("/")[0])
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.comments.get(num, [])), stderr="")
        if "/issues/" in path and path.endswith("/labels") and method == "POST":
            num = int(path.split("/issues/")[1].split("/")[0])
            self.labeled.append(num)
            return SimpleNamespace(returncode=0, stdout="[]", stderr="")
        if "/pulls/" in path and method == "PATCH":
            num = int(path.rstrip("/").split("/pulls/")[-1].split("?")[0])
            self.closed.append(num)
            return SimpleNamespace(returncode=0, stdout="{}", stderr="")
        if path.startswith("notifications") and "threads/" not in path and method == "GET":
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.notifications), stderr="")
        if path.startswith("notifications/threads/") and method == "PATCH":
            self.marked.append(path.rsplit("/", 1)[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if path in self.subjects:
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.subjects[path]), stderr="")
        if "api.github.com/" in path and path.split("api.github.com/", 1)[-1] in self.subjects:
            key = path.split("api.github.com/", 1)[-1]
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.subjects[key]), stderr="")
        if "/packages?" in path:
            if self.package_error:
                return SimpleNamespace(returncode=1, stdout=self.package_error, stderr="gh: HTTP 403")
            return SimpleNamespace(returncode=0, stdout=json.dumps(self.packages or []), stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected " + path)


class TestClassify:
    def test_parses_json_and_redacts_secret_shaped_rationale(self):
        def complete(_prompt: str) -> str:
            return json.dumps([
                {"id": "a1", "decision": "approval", "rationale": "API_KEY=supersecret looks risky"},
                {"id": "a2", "decision": "autonomous", "rationale": "rebuildable log"},
                {"id": "bad", "decision": "drop", "rationale": "ignored"},
            ])

        out = classify_candidates(
            [{"id": "a1", "name": "mystery"}, {"id": "a2", "name": "log"}],
            complete=complete,
        )
        assert [c.id for c in out] == ["a1", "a2"]
        assert out[0].decision == "approval"
        assert "supersecret" not in out[0].rationale
        assert "REDACTED" in out[0].rationale
        assert out[1].decision == "autonomous"

    def test_missing_model_or_gateway_error_returns_empty_and_redacts(self, caplog):
        class _NoGo:
            def complete(self, *args, **kwargs):
                raise RuntimeError("OPENROUTER_API_KEY=should-not-appear")

        out = classify_candidates([{"id": "a"}], completer=_NoGo())
        assert out == []
        assert "should-not-appear" not in caplog.text
        assert "OPENROUTER_API_KEY" not in caplog.text


class TestReports:
    def test_stale_drafts_get_a_notice_and_label(self, tmp_path):
        gh = FakeGh(
            pulls=[
                _pr(1, title="old draft", updated=STALE, user={"login": "alice", "type": "User"}, labels=["needs-review"]),
                _pr(2, title="fresh draft", updated=FRESH, user={"login": "alice", "type": "User"}),
                {**_pr(3, title="ready", updated=STALE, user={"login": "alice", "type": "User"}), "draft": False},
            ]
        )
        result = run(
            {"resource": "drafts", "repo": "acme/repo", "now": NOW, "stale_days": 7, "_runner": gh, "state_dir": tmp_path},
            _ctx(tmp_path),
        )
        assert result.ok
        assert result.value["count"] == 1
        assert result.value["items"][0]["number"] == 1
        assert result.value["items"][0]["labels"] == ["needs-review"]
        assert [i["number"] for i in result.value["noticed"]] == [1]
        assert result.value["closed"] == []
        assert gh.noticed == [1]
        assert gh.labeled == [1]
        assert gh.closed == []
        assert "noticed" in " ".join(result.actions_taken).lower()

    def test_dry_run_does_not_comment_or_close(self, tmp_path):
        gh = FakeGh(pulls=[_pr(1, title="old draft", updated=STALE, user={"login": "dependabot[bot]", "type": "Bot"})])
        result = run(
            {"resource": "drafts", "repo": "acme/repo", "now": NOW, "dry_run": True, "_runner": gh, "state_dir": tmp_path},
            _ctx(tmp_path),
        )
        assert result.ok
        assert result.value["act"] is False
        assert gh.noticed == []
        assert gh.closed == []
        assert "dry-run" in " ".join(result.actions_taken).lower()

    def test_bot_draft_closes_after_notice_expires(self, tmp_path):
        notice = notice_body(stale_days=7, now=NOW - timedelta(days=8))
        gh = FakeGh(
            pulls=[_pr(9, title="bot draft", updated=NOTICED, user={"login": "dependabot[bot]", "type": "Bot"}, labels=["tidy/stale-notice"])],
            comments={9: [{"body": notice, "created_at": NOTICED}]},
        )
        result = run(
            {"resource": "drafts", "repo": "acme/repo", "now": NOW, "_runner": gh, "state_dir": tmp_path},
            _ctx(tmp_path),
        )
        assert result.ok
        assert [i["number"] for i in result.value["closed"]] == [9]
        assert result.value["held"] == []
        assert gh.closed == [9]

    def test_human_draft_after_notice_is_needs_you_not_closed(self, tmp_path):
        notice = notice_body(stale_days=7, now=NOW - timedelta(days=8))
        gh = FakeGh(
            pulls=[_pr(4, title="human draft", updated=NOTICED, user={"login": "alice", "type": "User"}, labels=["tidy/stale-notice"])],
            comments={4: [{"body": notice, "created_at": NOTICED}]},
        )
        result = run(
            {"resource": "drafts", "repo": "acme/repo", "now": NOW, "_runner": gh, "state_dir": tmp_path, "recipients": ("@operator",), "_sender": lambda r, a: "ok"},
            _ctx(tmp_path),
        )
        assert result.ok
        assert result.value["closed"] == []
        assert gh.closed == []
        assert len(result.value["held"]) == 1
        assert result.value["held"][0]["number"] == 4

    def test_activity_after_notice_skips_close(self, tmp_path):
        notice = notice_body(stale_days=7, now=NOW - timedelta(days=8))
        gh = FakeGh(
            pulls=[_pr(5, title="bot draft", updated=NOTICED, user={"login": "dependabot[bot]", "type": "Bot"}, labels=["tidy/stale-notice"])],
            comments={
                5: [
                    {"body": notice, "created_at": NOTICED},
                    {"body": "still working", "created_at": (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")},
                ]
            },
        )
        result = run(
            {"resource": "drafts", "repo": "acme/repo", "now": NOW, "_runner": gh, "state_dir": tmp_path},
            _ctx(tmp_path),
        )
        assert result.ok
        assert gh.closed == []
        assert result.value["closed"] == []

    def test_notifications_mark_merged_or_closed_read(self, tmp_path):
        gh = FakeGh(
            notifications=[
                {
                    "id": "n1",
                    "reason": "review_requested",
                    "unread": True,
                    "repository": {"full_name": "acme/repo"},
                    "subject": {
                        "title": "Please review",
                        "type": "PullRequest",
                        "url": "https://api.github.com/repos/acme/repo/pulls/1",
                    },
                    "updated_at": "2026-11-21T00:00:00Z",
                },
                {
                    "id": "n2",
                    "reason": "mention",
                    "unread": True,
                    "repository": {"full_name": "acme/repo"},
                    "subject": {
                        "title": "Open still",
                        "type": "PullRequest",
                        "url": "https://api.github.com/repos/acme/repo/pulls/2",
                    },
                    "updated_at": "2026-11-21T00:00:00Z",
                },
            ],
            subjects={
                "repos/acme/repo/pulls/1": {"state": "closed", "merged_at": "2026-11-20T00:00:00Z"},
                "repos/acme/repo/pulls/2": {"state": "open"},
            },
        )
        result = run({"resource": "notifications", "_runner": gh, "state_dir": tmp_path}, _ctx(tmp_path))
        assert result.ok
        assert result.value["unread"] == 2
        assert result.value["marked_read"] == ["n1"]
        assert gh.marked == ["n1"]
        assert "n2" not in gh.marked

    def test_notifications_dry_run_does_not_mark_read(self, tmp_path):
        gh = FakeGh(
            notifications=[{
                "id": "n1", "reason": "review_requested", "unread": True,
                "repository": {"full_name": "acme/repo"},
                "subject": {"title": "Please review", "url": "https://api.github.com/repos/acme/repo/pulls/1"},
            }],
            subjects={"repos/acme/repo/pulls/1": {"state": "closed"}},
        )
        result = run({"resource": "notifications", "dry_run": True, "_runner": gh, "state_dir": tmp_path}, _ctx(tmp_path))
        assert result.ok
        assert result.value["marked_read"] == []
        assert gh.marked == []
        assert "dry-run" in " ".join(result.actions_taken).lower()

    def test_packages_missing_scope_is_a_note_not_a_crash(self, tmp_path):
        gh = FakeGh(package_error='{"message":"You need at least read:packages scope","status":"403"}')
        result = run({"resource": "packages", "org": "acme", "_runner": gh}, _ctx(tmp_path))
        assert result.ok
        assert result.value["packages"] == []
        assert any("read:packages" in e for e in result.value["errors"])
        assert "tier 2" in " ".join(result.actions_taken).lower()

    def test_classify_skill_uses_injected_complete_and_proposes_tier2(self, tmp_path):
        def complete(_prompt: str) -> str:
            return json.dumps([
                {"id": "x", "decision": "keep", "rationale": "release artifact"},
                {"id": "y", "decision": "approval", "rationale": "outside policy"},
            ])

        result = run(
            {
                "resource": "classify",
                "candidates": [{"id": "x", "name": "mystery"}, {"id": "y", "name": "odd"}],
                "_complete": complete,
                "state_dir": tmp_path,
            },
            _ctx(tmp_path),
        )
        assert result.ok
        decisions = {i["id"]: i["decision"] for i in result.value["items"]}
        assert decisions == {"x": "keep", "y": "approval"}
        assert [p["id"] for p in result.value["tier2_proposed"]] == ["y"]
        assert "label" in " ".join(result.actions_taken).lower()
