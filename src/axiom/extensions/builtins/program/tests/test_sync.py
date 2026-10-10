# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.sync`` — reconcile the data file against its source, log the diff.

The self-update step. The load-bearing property is idempotency: the snapshot
is a content hash per item and per field, so running sync twice over an
unchanged source logs nothing the second time. That is the drift resilience —
a change seen twice is not logged twice — and nothing here assumes it is the
only writer.
"""

from __future__ import annotations

import json
import logging

import pytest

from axiom.extensions.builtins.program.skills import _changelog as cl
from axiom.extensions.builtins.program.skills import sync
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def node(tmp_path, data_dict):
    """A node state dir with the fixture program at the default data path,
    and a cli-surface context pointed at it."""
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    (state / "program" / "data.json").write_text(json.dumps(data_dict, indent=1), encoding="utf-8")
    ctx = SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.sync"),
        user_prompt=None,
        surface="cli",
    )
    return ctx


def _kinds(result) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in result.value["changes"]:
        out[c["kind"]] = out.get(c["kind"], 0) + 1
    return out


class TestSelfReconcile:
    def test_first_sync_logs_the_baseline(self, node):
        result = sync.run({}, node)
        assert result.ok
        counts = _kinds(result)
        assert counts == {"item_added": 4, "lane_added": 2, "drift_opened": 3}
        assert result.value["source"].startswith("file:")
        assert result.value["data_updated"] is False

    def test_second_sync_logs_nothing(self, node):
        """Idempotency: the second run over an unchanged source is a no-op."""
        sync.run({}, node)
        again = sync.run({}, node)
        assert again.ok
        assert again.value["count"] == 0
        assert again.value["changes"] == []

    def test_a_hand_edit_is_detected_on_the_next_sync(self, node):
        sync.run({}, node)  # establish baseline
        data_path = node.state_dir / "program" / "data.json"
        doc = json.loads(data_path.read_text(encoding="utf-8"))
        doc["schedule"][0]["status"] = "committed"
        doc["schedule"][0]["pct"] = 90
        data_path.write_text(json.dumps(doc, indent=1), encoding="utf-8")

        result = sync.run({}, node)
        kinds = _kinds(result)
        assert kinds.get("status_changed") == 1
        assert kinds.get("pct_changed") == 1
        subjects = {(c["kind"], c["subject"]) for c in result.value["changes"]}
        assert ("status_changed", "i-one") in subjects

    def test_every_logged_change_carries_seq_ts_and_source(self, node):
        sync.run({}, node)
        entries = cl.read_changelog(cl.changelog_path(node))
        assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1))
        for e in entries:
            assert e["ts"] and e["source"].startswith("file:")
            assert e["kind"] in cl.CHANGE_KINDS

    def test_seq_keeps_climbing_across_syncs(self, node):
        sync.run({}, node)
        n_after_first = len(cl.read_changelog(cl.changelog_path(node)))
        data_path = node.state_dir / "program" / "data.json"
        doc = json.loads(data_path.read_text(encoding="utf-8"))
        doc["schedule"][0]["owner"] = "@dana:example-org"
        data_path.write_text(json.dumps(doc, indent=1), encoding="utf-8")
        sync.run({}, node)
        entries = cl.read_changelog(cl.changelog_path(node))
        assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1))
        assert entries[-1]["seq"] == n_after_first + 1
        assert entries[-1]["kind"] == "owner_changed"


class TestDistinctSource:
    def test_a_distinct_source_updates_data_json(self, node, tmp_path, data_dict):
        sync.run({}, node)  # baseline from current data.json
        # an upstream file with one item's status committed
        upstream = tmp_path / "upstream.json"
        changed = json.loads(json.dumps(data_dict))
        changed["schedule"][1]["status"] = "proposed"  # was committed
        upstream.write_text(json.dumps(changed), encoding="utf-8")

        result = sync.run({"source": str(upstream)}, node)
        assert result.ok
        assert result.value["data_updated"] is True
        # data.json now reflects the upstream
        data_path = node.state_dir / "program" / "data.json"
        doc = json.loads(data_path.read_text(encoding="utf-8"))
        assert doc["schedule"][1]["status"] == "proposed"
        assert any(c["kind"] == "status_changed" for c in result.value["changes"])


class TestNullAndMissing:
    def test_missing_data_file_is_a_safe_noop(self, tmp_path):
        ctx = SkillContext(
            registry=SkillRegistry(),
            state_dir=tmp_path / "empty-state",
            logger=logging.getLogger("test.program.sync"),
            user_prompt=None,
            surface="cli",
        )
        result = sync.run({}, ctx)
        assert result.ok
        assert result.value["count"] == 0
        assert "nothing to reconcile" in result.value["note"]

    def test_an_invalid_source_is_a_typed_refusal(self, node, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"schema": "wrong"}), encoding="utf-8")
        result = sync.run({"source": str(bad)}, node)
        assert not result.ok
        assert result.value["refused"] == "no_data"


class TestServingSurfaceBoundary:
    def test_data_param_is_refused_off_the_cli(self, node, tmp_path):
        served = SkillContext(
            registry=node.registry,
            state_dir=node.state_dir,
            logger=node.logger,
            user_prompt=None,
            surface="mcp",
        )
        result = sync.run({"data": str(tmp_path / "x.json")}, served)
        assert not result.ok
        assert result.value["refused"] == "bad_request"
