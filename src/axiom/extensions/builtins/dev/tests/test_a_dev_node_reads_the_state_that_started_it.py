# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A dev node reads the connector registry of the command that started it.

`neut dev up` starts the node as ``python -m axiom.axiom_cli serve``, which
carries the platform's branding, so the node read ``~/.axi`` while `neut data
enroll` wrote ``~/.neut``. The intake refused every row of an enrolled source as
"not registered for site", however often it was enrolled.
"""

from __future__ import annotations

from pathlib import Path

from axiom.extensions.builtins.data_platform.agents.plinth.connectors import (
    ConnectorConfig,
    save_connector,
)
from axiom.extensions.builtins.data_platform.ingest_sink import make_connector_site_resolver
from axiom.extensions.builtins.dev import node

CHECKOUT = Path(__file__).resolve().parents[6]


def test_a_source_enrolled_by_the_starting_command_is_one_the_node_knows(tmp_path, monkeypatch):
    product_state = tmp_path / "product"
    plan = node.build_plan(lane="a", root=CHECKOUT, port=1, state_dir=product_state, base_env={})
    save_connector(
        ConnectorConfig(name="alpha-pxi", kind="push", bronze_root=str(tmp_path), site="alpha"),
        state_dir=product_state,
    )
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "platform"))
    for key, value in plan.env.items():
        monkeypatch.setenv(key, value)
    assert make_connector_site_resolver()("alpha-pxi") == "alpha"
