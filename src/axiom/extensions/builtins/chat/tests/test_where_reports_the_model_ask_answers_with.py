# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``chat --where`` reports the model ``chat --ask`` answers with.

Found live on a bound laptop: ``--where`` said "Model <a local file> ... Runs LOCAL ... Sources
BOUND (<site>): no model" while ``--ask`` answered on the node's model. ``--where`` built a bare
gateway and never made the choice ``--ask`` makes (the bound node's model, else the product's
default); both now go through ``bound_model.choose_default_model``.
"""

from __future__ import annotations

import io
import json

import pytest

from axiom.extensions.builtins.chat import bound_model, cli_flags, where
from axiom.extensions.builtins.chat.commands.cli import get_parser
from axiom.extensions.builtins.data_platform import read_target
from axiom.llm import gateway as gw

FACE = "https://node.example.org"


@pytest.fixture
def bound(tmp_path, monkeypatch):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    for name in ("DATABASE_URL", "DP1_RAG_DSN", "AXIOM_DB_URL", where.SAMPLE_DATA_ENV):
        monkeypatch.delenv(name, raising=False)
    (state / read_target.STATE_FILENAME).write_text(json.dumps({
        "current": "alpha-site",
        "bindings": {"alpha-site": {"site": "alpha-site", "face_url": FACE}},
    }), encoding="utf-8")
    monkeypatch.setattr(read_target, "holds_medallion", lambda: False)
    monkeypatch.setattr(read_target, "bound_key_for", lambda url: "k")
    monkeypatch.setattr(bound_model, "_node_model", lambda face, key, **kw: ("node-model", ""))
    # No configured provider: this machine has nothing of its own to answer with.
    monkeypatch.setattr(gw.Gateway, "_load_config", lambda self: None)
    monkeypatch.setattr(bound_model, "_CHOSEN", {})


def test_where_names_the_bound_nodes_model_not_this_machines(bound):
    out = io.StringIO()
    args = get_parser().parse_args(["--where"])
    assert cli_flags.run_headless(args, out=out) == 0
    text = out.getvalue()
    assert "node-model" in text, text
    assert "BOUND (alpha-site): model node-model on node.example.org" in text, text
    assert "no model" not in text and "Runs        LOCAL" not in text, text
