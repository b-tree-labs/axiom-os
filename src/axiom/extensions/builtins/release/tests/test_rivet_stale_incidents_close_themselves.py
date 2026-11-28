# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""An auto-opened CI incident closes itself when its cause is gone (#2441, "one incident per cause").

The CI workflow files ``CI incident on <branch>`` under the marker ``<!-- ci-incident:branch=KEY -->``
for failures on main and on version tags, and nothing closed them: dozens stayed open after main was
green again and after later tags published. An incident is closed, with a comment naming the later
green run and commit, only when

* its key is main and a completed green run of the main workflow exists on a LATER commit of main
  than the incident's newest occurrence; or
* its key is a version tag and a LATER version tag has a completed successful publish run.

A person's comment is never hidden: the close comment says it. An incident whose key does not match
(the marker and the title disagree, a person filed it, no marker) is never touched. Anything unsure
(an unreadable commit, no later green in the runs read) is kept.

Recorded with read-only ``gh api`` (``fixtures/stale_incidents``): four real open incidents (a tag, three
on main, with their occurrence comments), the 100 newest main runs, the real ancestry answer for each main
incident's newest occurrence, and the publish runs of the newest tags. Synthetic pieces are marked.
"""

from __future__ import annotations

import json
from pathlib import Path

from axiom.extensions.builtins.release.ci_health import stale_incidents as SI
from axiom.extensions.builtins.release.ci_health.client import CIClientError
from axiom.extensions.builtins.release.ci_health.config import CIHealthConfig

FIX = Path(__file__).parent / "fixtures" / "stale_incidents"
REPO = "example-org/app"
CONFIG = CIHealthConfig()


def _json(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Forge:
    """Serves the recorded incidents, runs, ancestry and publish runs. Records every write."""

    def __init__(self, *, issues=None, comments=None) -> None:
        self.issues = issues if issues is not None else _json("issues.json")
        self.comments = comments if comments is not None else _json("comments.json")
        self.runs = _json("main_runs.json")
        self.commits = _json("commits.json")
        self.ancestry = _json("ancestry.json")
        self.tag_names = _json("tags.json")
        self.tag_runs = _json("tag_runs.json")
        self.written: list[tuple] = []

    def open_issues(self, repo, label):
        return [i for i in self.issues if any(lab["name"] == label for lab in i["labels"]) and i["state"] == "open"]

    def issue_comments(self, repo, number):
        return list(self.comments.get(str(number), []))

    def workflow_runs(self, repo, workflow, branch, *, per_page=30):
        if workflow == CONFIG.main_workflow and branch == "main":
            return self.runs
        if workflow == CONFIG.publish_workflow:
            return self.tag_runs.get(branch, [])
        return []

    def commit(self, repo, sha):
        full = self.commits.get(sha) or (sha if len(sha) == 40 else "")
        if not full:
            raise CIClientError(f"commit {sha}: not found")
        return {"sha": full, "message": "", "author_login": "", "parents": []}

    def is_ancestor(self, repo, older, newer):
        key = f"{older}:{newer}"
        assert key in self.ancestry, f"unrecorded ancestry question {key}"
        return self.ancestry[key]

    def tags(self, repo):
        return list(self.tag_names)

    def comment_issue(self, repo, number, body):
        self.written.append(("comment", number, body))

    def close_issue(self, repo, number):
        self.written.append(("close", number))


def _by_number(findings):
    return {f.number: f for f in findings}


def _newest_green() -> dict:
    greens = [r for r in _json("main_runs.json") if r["event"] == "push" and r["conclusion"] == "success"]
    return max(greens, key=lambda r: r["created_at"])


# --- the recorded incidents -----------------------------------------------------------------------------


def test_the_recorded_main_incidents_close_naming_the_later_green_run_and_commit():
    found = _by_number(SI.scan(Forge(), CONFIG, REPO))
    green = _newest_green()
    for number in (1389, 1206):
        finding = found[number]
        assert finding.verdict == SI.CLOSE, finding.reason
        assert str(green["id"]) in finding.comment and green["head_sha"][:10] in finding.comment
        assert "main" in finding.comment


def test_the_recorded_tag_incident_closes_naming_the_later_publish():
    finding = _by_number(SI.scan(Forge(), CONFIG, REPO))[1647]
    assert finding.verdict == SI.CLOSE, finding.reason
    assert "v0.70.50" in finding.comment and "publish" in finding.comment.lower()


def test_the_incident_that_is_the_current_cause_is_kept():
    # Synthetic: the incident's newest occurrence is the newest green commit itself, so no LATER green exists.
    forge = Forge()
    green = _newest_green()["head_sha"]
    forge.commits["4d45d9b"] = green
    forge.ancestry = {f"{green}:{green}": True, **{k: v for k, v in forge.ancestry.items() if k.startswith("93bb858")}}
    finding = _by_number(SI.scan(forge, CONFIG, REPO))[1389]
    assert finding.verdict == SI.KEEP
    assert "later commit" in finding.reason


def test_a_tag_with_no_later_successful_publish_is_kept():
    forge = Forge()
    forge.tag_runs = {t: [dict(r, conclusion="failure") for r in rows] for t, rows in forge.tag_runs.items()}
    finding = _by_number(SI.scan(forge, CONFIG, REPO))[1647]
    assert finding.verdict == SI.KEEP
    assert "publish" in finding.reason


def test_an_earlier_tag_never_supersedes_a_later_one():
    forge = Forge()
    forge.issues = [dict(i, title=i["title"].replace("v0.70.27", "v0.70.60"), body=i["body"].replace("v0.70.27", "v0.70.60")) for i in forge.issues]
    assert _by_number(SI.scan(forge, CONFIG, REPO))[1647].verdict == SI.KEEP


# --- a person's comment is never hidden ------------------------------------------------------------------


def test_a_person_who_commented_is_named_in_the_close_comment():
    # Synthetic: the recorded incidents were commented only by the workflow's bot.
    forge = Forge()
    forge.comments["1206"] = forge.comments["1206"] + [
        {"user": {"login": "reviewer", "type": "User"}, "created_at": "2026-10-10T09:00:00Z", "body": "looking at it"}
    ]
    finding = _by_number(SI.scan(forge, CONFIG, REPO))[1206]
    assert finding.verdict == SI.CLOSE
    assert "@reviewer" in finding.comment and "commented" in finding.comment
    assert "@reviewer" not in _by_number(SI.scan(Forge(), CONFIG, REPO))[1389].comment


def test_a_bot_only_thread_says_nothing_about_a_person():
    assert "commented" not in _by_number(SI.scan(Forge(), CONFIG, REPO))[1389].comment


# --- never touch what it does not key ---------------------------------------------------------------------


def test_an_incident_whose_marker_and_title_disagree_is_never_touched():
    forge = Forge()
    forge.issues = [dict(i, body=i["body"].replace("branch=v0.70.27", "branch=v0.70.28")) if i["number"] == 1647 else i for i in forge.issues]
    finding = _by_number(SI.scan(forge, CONFIG, REPO))[1647]
    assert finding.verdict == SI.SKIP and "does not match" in finding.reason


def test_an_issue_a_person_filed_is_never_touched_even_with_the_marker():
    forge = Forge()
    forge.issues = [dict(i, user={"login": "someone", "type": "User"}) if i["number"] == 1389 else i for i in forge.issues]
    assert _by_number(SI.scan(forge, CONFIG, REPO))[1389].verdict == SI.SKIP


def test_an_issue_without_the_marker_is_never_touched():
    forge = Forge()
    forge.issues = [dict(i, body="no marker here") if i["number"] == 1389 else i for i in forge.issues]
    assert _by_number(SI.scan(forge, CONFIG, REPO))[1389].verdict == SI.SKIP


def test_a_key_that_is_neither_main_nor_a_version_tag_is_never_touched():
    forge = Forge()
    forge.issues = [
        dict(i, title="🔴 CI incident on `feature-x` (first: abc1234)", body="<!-- ci-incident:branch=feature-x -->")
        if i["number"] == 1389
        else i
        for i in forge.issues
    ]
    finding = _by_number(SI.scan(forge, CONFIG, REPO))[1389]
    assert finding.verdict == SI.SKIP and "neither" in finding.reason


def test_an_unreadable_commit_keeps_the_incident():
    forge = Forge()
    forge.commits.pop("4d45d9b")
    assert _by_number(SI.scan(forge, CONFIG, REPO))[1389].verdict == SI.KEEP


# --- writing -----------------------------------------------------------------------------------------------


def test_scan_writes_nothing():
    forge = Forge()
    SI.scan(forge, CONFIG, REPO)
    assert forge.written == []


def test_close_comments_first_then_closes_only_the_closable_ones():
    forge = Forge()
    findings = SI.scan(forge, CONFIG, REPO)
    closed = SI.close(forge, REPO, findings)
    assert sorted(closed) == sorted(f.number for f in findings if f.verdict == SI.CLOSE)
    kinds = [w[0] for w in forge.written]
    assert kinds == ["comment", "close"] * len(closed)
    assert all(w[1] in closed for w in forge.written)


def test_the_render_lists_each_incident_and_why():
    text = SI.render(SI.scan(Forge(), CONFIG, REPO), act=False)
    assert "would close #1389" in text and "would close #1647" in text
    assert "nothing was closed" in text


# --- the watch --------------------------------------------------------------------------------------------


class _WatchForge(Forge):
    """The incident forge plus what the watch itself reads: the recorded green main, so no new incident."""

    def __init__(self) -> None:
        super().__init__()
        self.opened: list[tuple] = []

    def branch_head(self, repo, branch):
        return _newest_green()["head_sha"]

    def create_issue(self, repo, title, body, *, labels, assignees):
        self.opened.append((title,))
        return {"number": 1, "html_url": ""}

    def edit_issue(self, repo, number, *, body):
        self.written.append(("edit", number))


def _skill(params, tmp_path):
    from types import SimpleNamespace

    from axiom.extensions.builtins.release import skills as S

    return S.ci_main_watch({"repo": REPO, "quiet": True, **params}, SimpleNamespace(state_dir=tmp_path))


def test_the_dry_run_lists_what_would_close_and_writes_nothing_even_with_act(tmp_path):
    forge = _WatchForge()
    result = _skill({"_client": forge, "stale_incidents": "list", "act": True}, tmp_path)
    assert result.ok
    assert forge.written == [] and forge.opened == []
    would = {f["number"] for f in result.value["stale_incidents"] if f["verdict"] == "close"}
    assert {1647, 1389, 1206} <= would


def test_the_watch_closes_nothing_by_default(tmp_path):
    forge = _WatchForge()
    _skill({"_client": forge, "act": True}, tmp_path)
    assert not [w for w in forge.written if w[0] == "close"]


def test_the_watch_closes_with_the_flag_and_with_the_config(tmp_path):
    forge = _WatchForge()
    result = _skill({"_client": forge, "act": True, "stale_incidents": "close"}, tmp_path)
    assert sorted(result.value["closed_incidents"]) == sorted(w[1] for w in forge.written if w[0] == "close")
    assert result.value["closed_incidents"]
    config = tmp_path / "lander.toml"
    config.write_text("[ci_health]\nclose_stale_incidents = true\n", encoding="utf-8")
    forge = _WatchForge()
    result = _skill({"_client": forge, "act": True, "config": str(config)}, tmp_path)
    assert result.value["closed_incidents"]


def test_close_without_act_writes_nothing(tmp_path):
    forge = _WatchForge()
    result = _skill({"_client": forge, "stale_incidents": "close"}, tmp_path)
    assert result.ok and not [w for w in forge.written if w[0] == "close"]
