# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One writer per program: ``program.writer`` names the install that writes.

Any other install running ``sync`` or ``publish`` (a laptop bound to the
writer, a second node) reads, reports what it would change, says it is
read-only, and writes nothing: not the data file, not the change log, not
the snapshot, not the freshness stamp, not its watcher cursors, not a wiki.
With no ``program.writer`` every install writes, as before.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest

from axiom.extensions.builtins.program.model import validate_program
from axiom.extensions.builtins.program.skills import publish, sync
from axiom.extensions.builtins.program.tests.conftest import GENERIC_PROGRAM
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture
def node(tmp_path):
    state = tmp_path / "state"
    (state / "program").mkdir(parents=True)
    raw = copy.deepcopy(GENERIC_PROGRAM)
    raw["program"]["writer"] = "node-a"
    (state / "program" / "data.json").write_text(json.dumps(raw, indent=1), encoding="utf-8")
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=state,
        logger=logging.getLogger("test.program.writer"),
        user_prompt=None,
        surface="cli",
    )


def _upstream(tmp_path, pct):
    raw = copy.deepcopy(GENERIC_PROGRAM)
    raw["program"]["writer"] = "node-a"
    raw["schedule"][0]["pct"] = pct
    path = tmp_path / "upstream.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def _files(node):
    program = node.state_dir / "program"
    return {
        p.relative_to(program).as_posix(): p.read_bytes() for p in program.rglob("*") if p.is_file()
    }


class TestSchema:
    def test_writer_must_be_a_non_empty_string(self):
        raw = copy.deepcopy(GENERIC_PROGRAM)
        raw["program"]["writer"] = ""
        assert any("program.writer" in e for e in validate_program(raw))
        raw["program"]["writer"] = "node-a"
        assert not any("program.writer" in e for e in validate_program(raw))


class TestSync:
    def test_the_named_writer_writes(self, node, tmp_path):
        result = sync.run({"source": str(_upstream(tmp_path, 60)), "_node_id": "node-a"}, node)
        assert result.ok and result.value["data_updated"] is True
        assert "read_only" not in result.value

    def test_another_install_reads_and_says_so_and_writes_nothing(self, node, tmp_path):
        assert sync.run({"_node_id": "node-a"}, node).ok  # the writer's baseline
        before = _files(node)
        result = sync.run({"source": str(_upstream(tmp_path, 60)), "_node_id": "laptop"}, node)
        assert result.ok, result.errors
        assert result.value["read_only"]["writer"] == "node-a"
        assert result.value["read_only"]["this_install"] == "laptop"
        assert result.value["data_updated"] is False and result.value["count"] == 0
        assert any(c["kind"] == "pct_changed" for c in result.value["would_change"])
        assert _files(node) == before  # nothing under the program state moved

        # the writer still sees the change: the read-only pass advanced nothing
        again = sync.run({"source": str(_upstream(tmp_path, 60)), "_node_id": "node-a"}, node)
        assert any(c["kind"] == "pct_changed" for c in again.value["changes"])

    def test_the_writer_match_ignores_case(self, node, tmp_path):
        result = sync.run({"source": str(_upstream(tmp_path, 60)), "_node_id": "NODE-A"}, node)
        assert "read_only" not in result.value

    def test_no_writer_declared_means_every_install_writes(self, node, tmp_path):
        path = node.state_dir / "program" / "data.json"
        raw = json.loads(path.read_text())
        raw["program"].pop("writer")
        path.write_text(json.dumps(raw))
        upstream = _upstream(tmp_path, 60)
        doc = json.loads(upstream.read_text())
        doc["program"].pop("writer")
        upstream.write_text(json.dumps(doc))
        result = sync.run({"source": str(upstream), "_node_id": "laptop"}, node)
        assert (
            result.ok and result.value["data_updated"] is True and "read_only" not in result.value
        )

    def test_a_read_only_live_source_keeps_no_cursor(self, node):
        class Live:
            origin = "github:example-org/tracker"
            persist_cursors = True

            def verify(self):
                from axiom.extensions.builtins.program.skills.sources import ConnectorReadiness

                return ConnectorReadiness(self.origin, True, True, True)

            def load(self):
                Live.seen_persist = self.persist_cursors
                return None

        result = sync.run({"_sources": [Live()], "_node_id": "laptop"}, node)
        assert result.ok and Live.seen_persist is False


class TestPublish:
    def _with_wiki(self, node):
        path = node.state_dir / "program" / "data.json"
        raw = json.loads(path.read_text())
        raw["program"]["publish"] = {"wiki": {"repo": "example-org/program-tracker"}}
        path.write_text(json.dumps(raw))

    def test_another_install_does_not_publish(self, node, tmp_path):
        self._with_wiki(node)
        remote = tmp_path / "remote"
        result = publish.run({"_remote": str(remote), "_token": "x", "_node_id": "laptop"}, node)
        assert result.ok
        assert result.value["read_only"]["writer"] == "node-a"
        assert result.value["published"] is False
        assert not remote.exists()

    def test_a_dry_run_still_renders_anywhere(self, node, tmp_path):
        self._with_wiki(node)
        out = tmp_path / "out"
        result = publish.run({"dry_run": True, "out": str(out), "_node_id": "laptop"}, node)
        assert result.ok and result.value["dry_run"] is True
