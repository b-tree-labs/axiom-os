# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Crosslink health — no dead links, and unreachable is unverified not healthy.

Phase 5, R11 link health. The checks are read-only (a GET/HEAD against the
tracker/endpoint), so every name here is invented and generic and every
checker is a fake — no unit test touches the wire. Three contracts are pinned:
a confirmed-dead link is a `dead_link` finding (detection only, with the
correct backlink recorded under `proposed_fix`); a link that resolves is no
finding; and a checker that cannot verify reports `unverified`, never
`healthy`. The findings flow through the change log as
`dead_link_opened`/`dead_link_cleared` and reach the drift read.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest

from axiom.extensions.builtins.program.model import ProgramData
from axiom.extensions.builtins.program.skills import _changelog as cl
from axiom.extensions.builtins.program.skills import status, sync
from axiom.extensions.builtins.program.skills._capture import check_links
from axiom.extensions.builtins.program.skills.sources import ConnectorReadiness, GitLabSource
from axiom.extensions.builtins.program.tests.conftest import GENERIC_PROGRAM
from axiom.infra.skills import SkillContext, SkillRegistry

CANONICAL = "https://home.example/program"


class FakeChecker:
    """An injected crosslink checker. Every verdict is a dial: True resolves,
    False is confirmed dead, None (the default for an unlisted link) could not
    be verified."""

    def __init__(self, *, issues=None, urls=None):
        self._issues = issues or {}
        self._urls = urls or {}

    def issue_readable(self, ref):
        return self._issues.get(ref)

    def url_resolves(self, url):
        return self._urls.get(url)


def _program(endpoints=None) -> ProgramData:
    data = copy.deepcopy(GENERIC_PROGRAM)
    # Bound items in the fixture: i-one (issue 42), i-three (issue 57).
    if endpoints is not None:
        data["program"]["endpoints"] = endpoints
    return ProgramData(raw=data)


# ---- check_links unit contract --------------------------------------------


class TestCheckLinks:
    def test_a_dead_issue_link_is_a_dead_link_finding(self):
        prog = _program(endpoints={"canonical": CANONICAL})
        checker = FakeChecker(issues={42: False, 57: True}, urls={CANONICAL: True})
        health = check_links(prog, checker, canonical=CANONICAL)
        dead = [f for f in health.findings if f["kind"] == "dead_link"]
        assert len(dead) == 1
        assert dead[0]["subject"] == "i-one"  # the item, not the bare ref
        assert dead[0]["ref"] == 42
        assert dead[0]["verified"] is True
        # detection only: the correct backlink target is recorded for later
        assert dead[0]["proposed_fix"] == {"canonical": CANONICAL}

    def test_a_healthy_link_is_no_finding(self):
        prog = _program(endpoints={"canonical": CANONICAL})
        checker = FakeChecker(issues={42: True, 57: True}, urls={CANONICAL: True})
        health = check_links(prog, checker, canonical=CANONICAL)
        assert health.findings == []
        assert health.verified is True
        assert health.as_block()["basis"] == "live"

    def test_an_unreachable_checker_is_unverified_never_healthy(self):
        prog = _program(endpoints={"canonical": CANONICAL})
        # No checker at all — the connector could not be reached.
        health = check_links(prog, None, canonical=CANONICAL)
        assert health.findings == []  # nothing confirmed dead...
        assert health.verified is False  # ...and nothing confirmed healthy
        assert health.as_block()["basis"] == "unverified"
        assert health.as_block()["unverified"] >= 1

    def test_a_link_the_checker_cannot_read_is_unverified_not_dead(self):
        prog = _program(endpoints={"canonical": CANONICAL})
        # issue 42 cannot be verified (None); 57 resolves.
        checker = FakeChecker(issues={42: None, 57: True}, urls={CANONICAL: True})
        health = check_links(prog, checker, canonical=CANONICAL)
        assert [f for f in health.findings if f["ref"] == 42] == []  # not flagged dead
        assert any(u["ref"] == 42 for u in health.unverified)
        assert health.verified is False  # an unverifiable link blocks a clean pass

    def test_a_dead_declared_endpoint_is_a_dead_link_finding(self):
        prog = _program(endpoints={"canonical": CANONICAL, "roadmap": "https://home.example/road"})
        checker = FakeChecker(
            issues={42: True, 57: True},
            urls={CANONICAL: True, "https://home.example/road": False},
        )
        health = check_links(prog, checker, canonical=CANONICAL)
        dead = [f for f in health.findings if f["kind"] == "dead_link"]
        assert len(dead) == 1
        assert dead[0]["subject"] == "roadmap"
        assert dead[0]["url"] == "https://home.example/road"


# ---- the feeder records crosslink findings in the capture block ------------


class FakeClient:
    """A read-only tracker client with no network (watcher fetch only)."""

    def __init__(self, *, issues=None):
        self._issues = issues or []

    def ping(self):
        return True

    def whoami(self):
        return "svc"

    def project_readable(self):
        return True

    def issues(self, since):
        return list(self._issues)

    def merge_requests(self, since):
        return []

    def commits(self, host, repo):
        return None

    def issue_readable(self, ref):
        return True


def _write_live_program(tmp_path, endpoints=None):
    data = copy.deepcopy(GENERIC_PROGRAM)
    data["program"]["tracker"] = {"kind": "gitlab", "host": "tracker.example.org", "project_id": 7}
    if endpoints is not None:
        data["program"]["endpoints"] = endpoints
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True, exist_ok=True)
    path = state / "program" / "data.json"
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return path


class TestFeederRecordsCrosslinkHealth:
    def test_a_dead_bound_issue_becomes_a_capture_dead_link_finding(self, tmp_path):
        path = _write_live_program(tmp_path, endpoints={"canonical": CANONICAL})
        src = GitLabSource(
            path,
            client=FakeClient(),
            state_dir=tmp_path / "state",
            link_checker=FakeChecker(issues={42: False, 57: True}, urls={CANONICAL: True}),
        )
        program = src.load()
        findings = program.raw["capture"]["findings"]
        dead = [f for f in findings if f["kind"] == "dead_link"]
        assert len(dead) == 1 and dead[0]["subject"] == "i-one"
        # link_health is recorded on the capture block
        assert program.raw["capture"]["link_health"]["dead"] == 1

    def test_all_links_healthy_records_no_dead_link(self, tmp_path):
        path = _write_live_program(tmp_path, endpoints={"canonical": CANONICAL})
        src = GitLabSource(
            path,
            client=FakeClient(),
            state_dir=tmp_path / "state",
            link_checker=FakeChecker(issues={42: True, 57: True}, urls={CANONICAL: True}),
        )
        program = src.load()
        assert [f for f in program.raw["capture"]["findings"] if f["kind"] == "dead_link"] == []
        assert program.raw["capture"]["link_health"]["verified"] is True


# ---- dead_link_opened then dead_link_cleared across two syncs --------------


@pytest.fixture
def node(tmp_path, data_dict):
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    (state / "program" / "data.json").write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.crosslink"),
        user_prompt=None,
        surface="cli",
    )


class _FakeSource:
    def __init__(self, origin, data):
        self.origin = origin
        self._data = data

    def verify(self):
        return ConnectorReadiness(self.origin, True, True, True, "ok")

    def load(self):
        return self._data


def _with_capture(node, findings):
    raw = json.loads((node.state_dir / "program" / "data.json").read_text())
    raw["capture"] = {
        "source": "gitlab:x",
        "system": "gitlab",
        "verified": True,
        "link_health": {"checked": 2, "dead": len(findings), "unverified": 0, "verified": True},
        "findings": findings,
    }
    return ProgramData(raw=raw)


class TestDeadLinkThroughTheChangeLog:
    def test_dead_link_opens_then_clears_across_two_syncs(self, node):
        dead = [
            {"kind": "dead_link", "subject": "i-one", "detail": "x", "ref": 42, "verified": True}
        ]
        sync.run({"_sources": [_FakeSource("gitlab:x", _with_capture(node, dead))]}, node)
        opened = [
            c
            for c in cl.read_changelog(cl.changelog_path(node))
            if c["kind"] == "dead_link_opened"
        ]
        assert len(opened) == 1
        assert opened[0]["subject"] == "i-one"
        assert opened[0]["field"] == "dead_link"

        # next cycle: the link resolves again → it clears, under its own kind
        result = sync.run({"_sources": [_FakeSource("gitlab:x", _with_capture(node, []))]}, node)
        cleared = [c for c in result.value["changes"] if c["kind"] == "dead_link_cleared"]
        assert len(cleared) == 1
        assert cleared[0]["subject"] == "i-one"
        # a dead_link does NOT masquerade as the generic drift kind
        assert not any(c["kind"] == "drift_opened" and c["field"] == "dead_link" for c in opened)


# ---- the drift read surfaces the crosslink facet ---------------------------


class TestDriftSurfacesCrosslink:
    def _drift(self, path, ctx):
        return status.run({"data": str(path), "scope": "drift"}, ctx)

    def test_no_feeder_run_is_unchecked_not_healthy(self, data_file, ctx):
        facet = self._drift(data_file, ctx).value["crosslink"]
        assert facet["basis"] == "unchecked"
        assert facet["checked"] is False
        assert facet["findings"] == []

    def test_captured_dead_link_and_mirror_gap_surface_in_drift(self, write_data, data_dict, ctx):
        data_dict["capture"] = {
            "source": "gitlab:x",
            "system": "gitlab",
            "link_health": {"checked": 2, "dead": 1, "unverified": 0, "verified": True},
            "findings": [
                {"kind": "dead_link", "subject": "i-one", "detail": "d", "ref": 42},
                {"kind": "mirror_gap", "subject": "repo-x->repo-x", "detail": "m", "missing": 2},
                {"kind": "account_missing", "subject": "@rowan:example-org", "detail": "a"},
            ],
        }
        result = self._drift(write_data(data_dict), ctx)
        facet = result.value["crosslink"]
        assert facet["basis"] == "capture"
        kinds = {f["kind"] for f in facet["findings"]}
        assert kinds == {"dead_link", "mirror_gap"}  # link + mirror health, not attribution
        # the data-file-only findings/basis are unchanged by the facet
        assert result.value["basis"] == "data-file-only"
