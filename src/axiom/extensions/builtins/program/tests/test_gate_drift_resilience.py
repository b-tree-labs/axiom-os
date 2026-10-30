# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""P14 arming gate — drift / resilience.

The CLERK's blast radius is the program data file and its change log:
reversible, visible, not safety-critical. This suite is the *resilience* third
of the arming gate (docs/working/program-phase7-plus-plan.md §P14): it proves
the contract the North Star rests on — **unverified is never rounded up to
synced**, a one-sided gap is **surfaced rather than silently dropped**, and the
coordinator **reconciles correctly after a gap**, losing and duplicating
nothing.

These are the properties that must hold *before* `autonomy.enabled` is flipped
on a node, so they are pinned here as real end-to-end reconciles over the sync
seam (real change log, real snapshot, real watermarks), not mocks.
"""

from __future__ import annotations

import json
import logging

import pytest

from axiom.extensions.builtins.program.model import ProgramData
from axiom.extensions.builtins.program.skills import _changelog as cl
from axiom.extensions.builtins.program.skills import changes, sync
from axiom.extensions.builtins.program.skills.sources import ConnectorReadiness, GitHubSource
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def node(tmp_path, data_dict):
    """A node state dir with the fixture program at the default data path and
    a gitlab tracker declared, plus a cli-surface context pointed at it."""
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    data_dict["program"]["tracker"] = {
        "kind": "gitlab",
        "host": "tracker.example.org",
        "project_id": 7,
    }
    (state / "program" / "data.json").write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.gate.resilience"),
        user_prompt=None,
        surface="cli",
    )


def _data_path(node):
    return node.state_dir / "program" / "data.json"


def _edit(node, mutate) -> None:
    path = _data_path(node)
    doc = json.loads(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(json.dumps(doc, indent=1), encoding="utf-8")


def _kind_counts(result) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in result.value["changes"]:
        out[c["kind"]] = out.get(c["kind"], 0) + 1
    return out


class FakeSource:
    """A pre-built ProgramSource injected via params['_sources'] (no network)."""

    def __init__(self, origin, *, data=None, readiness=None):
        self.origin = origin
        self._data = data
        self._readiness = readiness

    def verify(self):
        return self._readiness or ConnectorReadiness(self.origin, True, True, True, "ok")

    def load(self):
        return self._data


class FakeClient:
    """A cleanly-verifying, networkless tracker client (so a real feeder's
    ``verify()`` ladder passes without touching the wire)."""

    def ping(self):
        return True

    def whoami(self):
        return "svc-account"

    def project_readable(self):
        return True

    def issues(self, since):
        return []

    def merge_requests(self, since):
        return []

    def commits(self, host, repo):
        return None

    def issue_readable(self, ref):
        return None


def _capture(node, findings, *, source="github:mirror", system="github"):
    raw = json.loads(_data_path(node).read_text(encoding="utf-8"))
    raw["capture"] = {"source": source, "system": system, "verified": True, "findings": findings}
    return ProgramData(raw=raw)


# ---------------------------------------------------------------------------
# unverified != synced
# ---------------------------------------------------------------------------


class TestUnverifiedIsNeverSynced:
    def test_a_connector_unverified_source_is_skipped_not_rounded_to_synced(self, node):
        """A source that cannot climb the readiness ladder is recorded in
        ``skipped`` with its failed rung, never swallowed as 'synced, 0
        changes'. The data file is left exactly as it was."""
        before = _data_path(node).read_bytes()
        unready = ConnectorReadiness(
            "gitlab:x", reachable=True, authenticated=False, project_read=False
        )
        src = FakeSource("gitlab:x", readiness=unready, data=_capture(node, []))
        result = sync.run({"_sources": [src]}, node)

        assert result.ok
        assert result.value["count"] == 0
        # The signal is loud: a skipped list naming the rung, and an action line.
        assert result.value["skipped"] and result.value["skipped"][0]["failed_rung"] == "authenticated"
        assert any("skipped" in a for a in result.actions_taken)
        # unverified must not touch the authoritative file or log anything.
        assert _data_path(node).read_bytes() == before
        assert cl.read_changelog(cl.changelog_path(node)) == []

    def test_an_unverified_mirror_side_surfaces_as_stale_not_synced(self, node):
        """A mirror side that could not be read is `mirror_stale` with
        ``verified=False`` — surfaced on the change log, never rounded up to
        'in sync'."""
        finding = {
            "kind": "mirror_stale",
            "subject": "repo-x->repo-x",
            "detail": "a side was unreadable",
            "verified": False,
        }
        src = FakeSource("github:mirror", data=_capture(node, [finding]))
        result = sync.run({"_sources": [src]}, node)
        opened = [c for c in result.value["changes"] if c["kind"] == "drift_opened"]
        assert any(c["field"] == "mirror_stale" for c in opened)

    def test_a_feeder_due_date_never_overwrites_the_committed_date(self, node):
        """The overlay detects divergence; it does not 'sync' the human field.
        A tracker due date that disagrees with the committed date is recorded
        as a `date_mismatch` finding while the committed date stays put —
        unverified tracker state is never written over confirmed state."""
        # i-one is committed to end 2026-10-16; the feeder claims a due of -20.
        raw = json.loads(_data_path(node).read_text(encoding="utf-8"))
        raw["schedule"][0]["issue"] = 42
        raw["capture"] = {
            "source": "gitlab:x",
            "system": "gitlab",
            "verified": True,
            "findings": [
                {
                    "kind": "date_mismatch",
                    "subject": "i-one",
                    "detail": "committed 2026-10-16 disagrees with tracker due 2026-10-20",
                    "committed": "2026-10-16",
                    "tracker_due": "2026-10-20",
                }
            ],
        }
        src = FakeSource("gitlab:x", data=ProgramData(raw=raw))
        result = sync.run({"_sources": [src]}, node)
        assert result.ok
        # The committed end date on disk is unchanged — not "synced" to the tracker.
        on_disk = json.loads(_data_path(node).read_text(encoding="utf-8"))
        assert on_disk["schedule"][0]["end"] == "2026-10-16"
        # And the disagreement is a visible finding, not a silent reconciliation.
        assert any(
            c["kind"] == "drift_opened" and c["field"] == "date_mismatch"
            for c in result.value["changes"]
        )


# ---------------------------------------------------------------------------
# a mirror gap is surfaced, persists, and clears — never silently dropped
# ---------------------------------------------------------------------------


class TestMirrorGapSurfacedNotDropped:
    def test_a_one_sided_gap_reaches_the_log_persists_then_clears(self, node):
        """Work present on the origin but absent from the mirror is surfaced as
        `mirror_gap` (drift_opened), is NOT re-logged while it persists
        (idempotent), and clears (drift_cleared) only once the mirror catches
        up. A gap is never silently dropped and never double-counted."""
        gap = {"kind": "mirror_gap", "subject": "repo-x->repo-x", "detail": "2 behind", "missing": 2}

        first = sync.run({"_sources": [FakeSource("github:mirror", data=_capture(node, [gap]))]}, node)
        assert any(
            c["kind"] == "drift_opened" and c["field"] == "mirror_gap" for c in first.value["changes"]
        )

        # A quiet cycle with the gap still open logs nothing new.
        second = sync.run({"_sources": [FakeSource("github:mirror", data=_capture(node, [gap]))]}, node)
        assert _kind_counts(second).get("drift_opened", 0) == 0
        assert _kind_counts(second).get("drift_cleared", 0) == 0

        # The mirror catches up → the gap clears, exactly once.
        third = sync.run({"_sources": [FakeSource("github:mirror", data=_capture(node, []))]}, node)
        cleared = [c for c in third.value["changes"] if c["kind"] == "drift_cleared"]
        assert [c["field"] for c in cleared] == ["mirror_gap"]

    def test_the_gap_flows_from_real_commit_identity_comparison(self, node):
        """End-to-end through the real GitHubSource: a declared origin/mirror
        pair whose commit sets differ produces the gap by SHA set-difference
        (agreeing commits collapse on SHA — no double count), and it reaches
        the change log through sync."""
        _edit(
            node,
            lambda d: d["program"].update(
                mirrors=[
                    {
                        "origin": {"host": "host-a", "repo": "repo-x"},
                        "mirror": {"host": "host-b", "repo": "repo-x"},
                    }
                ]
            ),
        )
        commit_sets = {
            ("host-a", "repo-x"): {"s1", "s2", "s3"},
            ("host-b", "repo-x"): {"s1"},  # mirror two behind
        }
        src = GitHubSource(
            _data_path(node),
            client=FakeClient(),  # a clean ladder, no network
            state_dir=node.state_dir,
            commit_reader=lambda host, repo: commit_sets.get((host, repo)),
        )
        result = sync.run({"_sources": [src]}, node)
        opened = [
            c for c in result.value["changes"] if c["kind"] == "drift_opened" and c["field"] == "mirror_gap"
        ]
        assert len(opened) == 1
        assert opened[0]["subject"] == "repo-x->repo-x"


# ---------------------------------------------------------------------------
# reconcile after a gap: no loss, no duplicate
# ---------------------------------------------------------------------------


class TestReconcileAfterAGap:
    def test_several_edits_during_a_missed_window_all_land_exactly_once(self, node):
        """CLERK was down (or a webhook was missed) while the file took three
        distinct edits. The next reconcile captures every one of them, each
        exactly once, and the cycle after converges to silence."""
        sync.run({}, node)  # establish the baseline snapshot

        _edit(node, lambda d: d["schedule"][0].__setitem__("owner", "@dana:example-org"))
        _edit(node, lambda d: d["schedule"][1].__setitem__("status", "proposed"))
        _edit(
            node,
            lambda d: d["schedule"].append(
                {"id": "i-five", "label": "Recovered", "date": "2026-11-20", "lane": "alpha"}
            ),
        )

        recovered = sync.run({}, node)
        counts = _kind_counts(recovered)
        assert counts.get("owner_changed") == 1
        assert counts.get("status_changed") == 1
        assert counts.get("item_added") == 1
        subjects = {(c["kind"], c["subject"]) for c in recovered.value["changes"]}
        assert ("owner_changed", "i-one") in subjects
        assert ("item_added", "i-five") in subjects

        # Converged: the very next cycle logs nothing (no drift, no re-emission).
        assert sync.run({}, node).value["count"] == 0

    def test_the_change_log_has_no_duplicate_or_gapped_sequence_numbers(self, node):
        """Across a baseline, a gap of edits, and a reconcile, the append-only
        log stays a strictly contiguous 1..N with no repeats — the exactly-once
        invariant the per-consumer watermark depends on."""
        sync.run({}, node)
        _edit(node, lambda d: d["schedule"][0].__setitem__("pct", 95))
        _edit(node, lambda d: d["schedule"][2].__setitem__("status", "committed"))
        sync.run({}, node)
        sync.run({}, node)  # idempotent tail

        seqs = [e["seq"] for e in cl.read_changelog(cl.changelog_path(node))]
        assert seqs == list(range(1, len(seqs) + 1))  # contiguous, no gaps
        assert len(seqs) == len(set(seqs))  # no duplicates


# ---------------------------------------------------------------------------
# the per-consumer watermark resumes across a gap
# ---------------------------------------------------------------------------


class TestWatermarkResumesAcrossAGap:
    def test_a_consumer_sees_exactly_the_new_entries_after_a_gap(self, node):
        """A principal reads-and-advances, a gap of changes accumulates, and the
        next read returns exactly the entries after the watermark — no loss, no
        replay of what was already seen."""
        who = "@casey:example-org"
        sync.run({}, node)
        first = changes.run({"principal": who}, node)  # cli surface advances
        assert first.value["advanced"] is True
        seen = first.value["count"]
        assert seen > 0

        # A gap: two more edits reconciled while this consumer did not look.
        _edit(node, lambda d: d["schedule"][0].__setitem__("owner", "@dana:example-org"))
        sync.run({}, node)
        _edit(node, lambda d: d["schedule"][1].__setitem__("pct", 80))
        sync.run({}, node)

        resumed = changes.run({"principal": who}, node)
        # Exactly the two new entries — resumes from the mark, no re-delivery.
        assert resumed.value["count"] == 2
        kinds = {c["kind"] for c in resumed.value["changes"]}
        assert kinds == {"owner_changed", "pct_changed"}

        # Immediately re-reading now yields nothing (the mark caught up).
        assert changes.run({"principal": who}, node).value["count"] == 0

    def test_a_peek_never_loses_entries_for_the_next_reader(self, node):
        """A served peek reports the deltas without advancing, so a later
        advancing read still delivers them — a read can never silently consume
        a consumer's backlog."""
        who = "@dana:example-org"
        sync.run({}, node)
        peek = changes.run({"principal": who, "peek": True}, node)
        assert peek.value["advanced"] is False and peek.value["count"] > 0
        # Nothing was marked seen, so the advancing read delivers the same set.
        advanced = changes.run({"principal": who}, node)
        assert advanced.value["count"] == peek.value["count"]
