# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Which copy wins is a property of the node, set by the site (ADR-180 §3).

``record_of_truth = "upstream"`` is contributor mode: the host holds the shared
record. ``"local"`` is local-first: the site's node is the record and the host
holds a copy of what is shared. It is declared, never inferred from which
functions happen to be on, and reconcile reads it.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.features import cli as features_cli
from axiom.infra import node_functions as nf


@pytest.fixture
def node(tmp_path, monkeypatch):
    path = tmp_path / "node.toml"
    monkeypatch.setenv(nf.CONFIG_ENV, str(path))
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    return path


def test_an_undeclared_node_is_a_contributor(node):
    assert nf.load(node).record_of_truth_in_force == "upstream"


def test_it_is_written_read_and_switched_back(node):
    assert nf.set_record_of_truth("local", path=node).record_of_truth_in_force == "local"
    assert nf.load(node).record_of_truth_in_force == "local"
    assert 'record_of_truth = "local"' in node.read_text()
    assert nf.set_record_of_truth("upstream", path=node).record_of_truth_in_force == "upstream"


def test_switching_keeps_everything_else_the_node_declared(node):
    nf.set_agent_policy("assist", path=node)
    nf.set_maintenance_level("requests", path=node)
    nf.set_record_of_truth("local", path=node)
    cfg = nf.load(node)
    assert (cfg.agent_policy, cfg.maintenance_level) == ("assist", "requests")


def test_anything_else_is_refused(node):
    with pytest.raises(nf.UnknownFeature):
        nf.set_record_of_truth("both", path=node)


def test_a_hand_edited_unknown_value_reads_as_the_default_and_says_so(node):
    node.write_text('[node]\nfunctions = []\nfeatures = []\nrecord_of_truth = "mine"\n')
    cfg = nf.load(node)
    assert cfg.record_of_truth_in_force == "upstream"
    assert "record_of_truth" in cfg.problem


def test_the_cli_sets_it_and_shows_it(node, capsys):
    assert features_cli.main(["record-of-truth", "local"]) == 0
    out = capsys.readouterr().out
    assert "local" in out
    assert nf.load(node).record_of_truth_in_force == "local"
    assert features_cli.main(["--json"]) == 0
    assert '"record_of_truth": "local"' in capsys.readouterr().out


def test_it_is_never_offered_to_an_ai_client():
    from axiom.extensions.builtins.features.skills import _SPECS

    spec = next(s for s in _SPECS if s.name == "features.record_of_truth")
    assert "mcp" not in spec.surfaces and "agent_tool" not in spec.surfaces
