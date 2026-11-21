# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Attribution, the proxy-assignee rule, and mirror agreement.

The domain-free judgement the feeders share. Every name here is invented and
generic — example principals, example account usernames, placeholder hosts
and repos. Real accounts and real mirror pairs are deployment data, never
code.
"""

from __future__ import annotations

import copy

from axiom.extensions.builtins.program.model import ProgramData
from axiom.extensions.builtins.program.skills._capture import (
    MergeRequest,
    MirrorPair,
    TrackerIssue,
    attribute_and_overlay,
    check_mirror,
    declared_mirror_pairs,
    intended_assignee,
)
from axiom.extensions.builtins.program.tests.conftest import GENERIC_PROGRAM


def _program() -> ProgramData:
    data = copy.deepcopy(GENERIC_PROGRAM)
    data["program"]["deputy"] = "@casey:example-org"
    data["program"]["tracker"] = {"kind": "gitlab", "host": "tracker.example.org", "project_id": 7}
    data["lanes"][0]["lead"] = "@casey:example-org"  # alpha lane lead HAS an account
    # casey: full accounts; dana: gitlab only; rowan: no accounts at all.
    data["people"][0]["accounts"] = {"gitlab": "casey42", "github": "casey-gh"}
    data["people"][1]["accounts"] = {"gitlab": "dana7"}
    # Make every item owned + bound so attribution has something to chew on.
    #   i-one  owner casey (direct account), issue 42
    #   i-two  owner dana  (direct account), bind to issue 50
    #   i-three owner casey, issue 57
    #   i-four  no owner originally — give it rowan (NO account), lane alpha, issue 60
    data["schedule"][1]["issue"] = 50
    data["schedule"][3]["owner"] = "@rowan:example-org"
    data["schedule"][3]["lane"] = "alpha"
    data["schedule"][3]["issue"] = 60
    return ProgramData(raw=data)


class TestProxyAssigneeRule:
    def test_direct_when_the_owner_has_an_account(self):
        data = _program()
        item = data.item("i-one")
        a = intended_assignee(data, item, system="gitlab")
        assert a == {"real_owner": "@casey:example-org", "via": "direct", "account": "casey42", "proxy": None}

    def test_lane_lead_proxy_when_owner_has_none_but_lead_does(self):
        data = _program()
        # i-four: owner rowan (no account), lane alpha whose lead is casey (has account)
        a = intended_assignee(data, data.item("i-four"), system="gitlab")
        assert a["real_owner"] == "@rowan:example-org"  # real owner still named
        assert a["via"] == "lane_lead"
        assert a["proxy"] == "@casey:example-org"
        assert a["account"] == "casey42"

    def test_deputy_proxy_when_neither_owner_nor_lane_lead_has_an_account(self):
        data = _program()
        raw = copy.deepcopy(data.raw)
        # Put rowan's item in the beta lane (no lead); deputy is casey (has account).
        raw["schedule"][3]["lane"] = "beta"
        data2 = ProgramData(raw=raw)
        a = intended_assignee(data2, data2.item("i-four"), system="gitlab")
        assert a["via"] == "deputy"
        assert a["proxy"] == "@casey:example-org"
        assert a["real_owner"] == "@rowan:example-org"

    def test_none_when_no_one_in_the_chain_has_an_account(self):
        data = _program()
        raw = copy.deepcopy(data.raw)
        raw["schedule"][3]["lane"] = "beta"  # no lane lead
        raw["program"]["deputy"] = "@rowan:example-org"  # deputy has no account either
        data2 = ProgramData(raw=raw)
        a = intended_assignee(data2, data2.item("i-four"), system="gitlab")
        assert a["via"] == "none"
        assert a["proxy"] is None
        assert a["real_owner"] == "@rowan:example-org"  # still named


class TestAttributionAndOverlay:
    def test_an_mr_is_attributed_to_the_principal_behind_the_account(self):
        data = _program()
        issues = [TrackerIssue(ref=42, title="First", state="opened", assignee="casey42")]
        mrs = [MergeRequest(ref=9, author="dana7", issue_refs=(42,), sha="abc", state="merged")]
        result = attribute_and_overlay(data, issues, mrs, system="gitlab")
        item = result.program.item("i-one")
        assert item["activity"][0]["author"] == "@dana:example-org"  # dana7 -> principal
        assert item["activity"][0]["author_account"] == "dana7"
        assert item["activity"][0]["landed"] is True

    def test_an_mr_by_an_unknown_account_attributes_to_no_principal(self):
        data = _program()
        issues = [TrackerIssue(ref=42)]
        mrs = [MergeRequest(ref=9, author="stranger", issue_refs=(42,), sha="z")]
        result = attribute_and_overlay(data, issues, mrs, system="gitlab")
        act = result.program.item("i-one")["activity"][0]
        assert act["author"] is None
        assert act["author_account"] == "stranger"

    def test_tracker_facts_overlay_without_clobbering_committed_fields(self):
        data = _program()
        before_owner = data.item("i-one")["owner"]
        before_status = data.item("i-one")["status"]
        issues = [
            TrackerIssue(ref=42, state="closed", assignee="casey42", milestone="M1", due="2026-10-16")
        ]
        result = attribute_and_overlay(data, issues, [], system="gitlab")
        item = result.program.item("i-one")
        # tracker facts land in a namespaced block...
        assert item["tracker"]["state"] == "closed"
        assert item["tracker"]["assignee"] == "casey42"
        # ...and the human-committed fields are untouched.
        assert item["owner"] == before_owner
        assert item["status"] == before_status

    def test_account_missing_is_one_finding_per_owner(self):
        data = _program()
        # rowan owns i-four (bound, no account) → exactly one account_missing.
        issues = [TrackerIssue(ref=60, assignee=None)]
        result = attribute_and_overlay(data, issues, [], system="gitlab")
        missing = [f for f in result.findings if f["kind"] == "account_missing"]
        assert len(missing) == 1
        assert missing[0]["subject"] == "@rowan:example-org"
        assert missing[0]["proxy"] == "@casey:example-org"  # proxied via lane lead
        assert missing[0]["state"] == "pending"

    def test_a_direct_owner_raises_no_account_missing(self):
        data = _program()
        issues = [TrackerIssue(ref=42, assignee="casey42")]  # casey owns i-one, has account
        result = attribute_and_overlay(data, issues, [], system="gitlab")
        assert [f for f in result.findings if f["subject"] == "@casey:example-org"] == []

    def test_a_due_date_disagreement_is_detected_not_overwritten(self):
        data = _program()
        committed = data.item("i-one")["end"]  # 2026-10-16
        issues = [TrackerIssue(ref=42, due="2026-11-30")]
        result = attribute_and_overlay(data, issues, [], system="gitlab")
        mismatch = [f for f in result.findings if f["kind"] == "date_mismatch"]
        assert len(mismatch) == 1
        assert mismatch[0]["subject"] == "i-one"
        assert mismatch[0]["committed"] == committed
        assert mismatch[0]["tracker_due"] == "2026-11-30"
        # the item's committed end is NOT changed
        assert result.program.item("i-one")["end"] == committed

    def test_a_tracker_issue_with_no_schedule_item_is_an_orphan(self):
        data = _program()
        issues = [TrackerIssue(ref=999, title="Rogue work")]  # bound to no item
        result = attribute_and_overlay(data, issues, [], system="gitlab")
        orphans = [f for f in result.findings if f["kind"] == "untracked_issue"]
        assert len(orphans) == 1
        assert orphans[0]["subject"] == 999

    def test_overlay_is_idempotent(self):
        data = _program()
        issues = [TrackerIssue(ref=42, state="closed", assignee="casey42")]
        first = attribute_and_overlay(data, issues, [], system="gitlab")
        # Re-running against the already-overlaid program yields the same doc.
        second = attribute_and_overlay(first.program, issues, [], system="gitlab")
        assert first.raw == second.raw


class TestMirrorAgreement:
    def test_pairs_are_read_from_program_mirrors(self):
        data = _program()
        raw = copy.deepcopy(data.raw)
        raw["program"]["mirrors"] = [
            {"origin": {"host": "host-a", "repo": "repo-x"}, "mirror": {"host": "host-b", "repo": "repo-x"}}
        ]
        pairs = declared_mirror_pairs(ProgramData(raw=raw))
        assert len(pairs) == 1
        assert pairs[0].origin_repo == "repo-x" and pairs[0].mirror_host == "host-b"

    def test_a_commit_through_both_sides_is_not_double_counted(self):
        pair = MirrorPair("host-a", "repo-x", "host-b", "repo-x")
        commits = {
            ("host-a", "repo-x"): {"sha1", "sha2", "sha3"},
            ("host-b", "repo-x"): {"sha1", "sha2", "sha3"},  # same SHAs, mirror
        }
        findings = check_mirror([pair], commits)
        assert findings == []  # perfect agreement, no gap

    def test_a_mirror_missing_commits_is_a_gap(self):
        pair = MirrorPair("host-a", "repo-x", "host-b", "repo-x")
        commits = {
            ("host-a", "repo-x"): {"sha1", "sha2", "sha3"},
            ("host-b", "repo-x"): {"sha1"},  # missing sha2, sha3
        }
        findings = check_mirror([pair], commits)
        assert len(findings) == 1
        assert findings[0]["kind"] == "mirror_gap"
        assert findings[0]["missing"] == 2

    def test_a_stale_mirror_job_is_flagged(self):
        pair = MirrorPair("host-a", "repo-x", "host-b", "repo-x")
        commits = {
            ("host-a", "repo-x"): {"sha1"},
            ("host-b", "repo-x"): {"sha1"},
        }
        findings = check_mirror([pair], commits, recent={pair.subject: False})
        assert [f["kind"] for f in findings] == ["mirror_stale"]

    def test_an_unreadable_side_is_unverified_never_assumed_synced(self):
        pair = MirrorPair("host-a", "repo-x", "host-b", "repo-x")
        commits = {("host-a", "repo-x"): {"sha1"}, ("host-b", "repo-x"): None}
        findings = check_mirror([pair], commits)
        assert [f["kind"] for f in findings] == ["mirror_stale"]
        assert findings[0]["verified"] is False

    def test_a_tracker_repo_with_no_mirror_pair_is_flagged_when_expected(self):
        findings = check_mirror(
            [],
            {},
            tracker_repo=("host-a", "repo-x"),
            expect_mirror=True,
        )
        assert [f["kind"] for f in findings] == ["mirror_missing"]
        assert findings[0]["subject"] == "repo-x"

    def test_mirror_check_is_idempotent(self):
        pair = MirrorPair("host-a", "repo-x", "host-b", "repo-x")
        commits = {("host-a", "repo-x"): {"s1", "s2"}, ("host-b", "repo-x"): {"s1"}}
        assert check_mirror([pair], commits) == check_mirror([pair], commits)
