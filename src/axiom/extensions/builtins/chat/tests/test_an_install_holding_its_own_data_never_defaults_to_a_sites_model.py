# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A development node answers with the model its lane configured, never a live site's by default.

Seen live: ``chat --ask`` on a development lane (its own database, sample gold) sent the question
to the production site's model, because the machine also held a binding to that site and the
product's default model (``BrandingConfig.chat_provider_fn``) is "the bound site's chat". The bound
model already followed the serving mode (``read_target``): an install that holds its own medallion
is the node, not a laptop bound to one. The product's default did not.

Both defaults now follow the one serving mode (ADR-207): a site's model is offered by default only
to an install that is bound to it. An install holding its own data, or running labelled sample data,
uses the providers it configured (``--provider``, ``llm-providers.toml``) or says it has none.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat import bound_model
from axiom.extensions.builtins.data_platform import read_target
from axiom.infra import serving_mode

FACE = "https://site-node.example.org"


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """A development node: its own database, and a binding to a live site left on the machine."""
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(serving_mode.SAMPLE_DATA_ENV, raising=False)
    (tmp_path / read_target.STATE_FILENAME).write_text(
        json.dumps({"current": "live-site", "bindings": {"live-site": {"site": "live-site", "face_url": FACE}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AXIOM_DB_URL", "postgresql://lane@127.0.0.1:1/axiom_lane_x")
    site_chat = {"name": "live-site chat", "endpoint": f"{FACE}/v1", "model": "live-site-assistant"}
    monkeypatch.setattr(
        "axiom.infra.branding.get_branding", lambda: SimpleNamespace(chat_provider_fn=lambda: site_chat)
    )
    monkeypatch.setattr(bound_model, "_node_model", lambda *a, **k: ("live-site-assistant", ""))


def _gateway():
    gw = MagicMock()
    gw.providers = []
    gw.auto_discovered = set()
    gw._provider_override = None
    gw.preview_provider.return_value = None
    return gw


def test_a_node_holding_its_own_data_is_never_given_a_sites_model_by_default(lane):
    gw = _gateway()
    bound_model.choose_default_model(gw)
    offered = [c.args[0] for c in gw.add_provider.call_args_list]
    assert [p.endpoint for p in offered if FACE in (p.endpoint or "")] == []
