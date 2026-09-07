# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ``push`` source kind: registers like any other, pulls nothing, and its
connector config is what the rows face lands pushed batches under."""

from __future__ import annotations

import logging

from axiom.infra.skills import SkillContext, SkillRegistry

from ....agents.plinth.connectors import load_connector
from ....ingest_sink import PushRowBatch, tabular_sink_for_connector
from ....skills import register as register_skill
from ...registry import default_source_kind_registry
from .. import PushProvider, PushSource


def test_push_kind_is_registered_and_constructs_an_empty_source():
    reg = default_source_kind_registry()
    assert reg.get("push").kind == "push"
    assert isinstance(reg.get("push"), PushProvider)
    src = PushSource("feed")
    assert src.list_changed() == []
    try:
        src.fetch_rows("x")
    except KeyError:
        pass
    else:  # pragma: no cover
        raise AssertionError("push source must not fetch")


def test_register_a_push_connector_then_land_a_batch_through_its_sink(tmp_path):
    ctx = SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))
    res = register_skill.run(
        {
            "name": "pushed-feed",
            "kind": "push",
            "bronze_root": str(tmp_path / "bronze"),
            "default_disposition": "allow",
            "default_tier": "public",
            "kind_params": {"schema_ref": "feed/rows-v1"},
        },
        ctx,
    )
    assert res.ok, res.errors
    cfg = load_connector("pushed-feed", state_dir=tmp_path)
    assert cfg.kind == "push" and cfg.params["schema_ref"] == "feed/rows-v1"
    sink = tabular_sink_for_connector("pushed-feed", state_dir=tmp_path)
    out = sink.ingest_rows(
        "pushed-feed",
        [PushRowBatch(item_id="b1", schema_ref="feed/rows-v1", rows=[{"a": 1}, {"a": 2}])],
    )
    assert out.landed == 1 and out.rows_landed == 2
    assert list((tmp_path / "bronze" / "pushed-feed" / "_rows").rglob("*.jsonl"))
    bad = register_skill.run(
        {
            "name": "x",
            "kind": "push",
            "bronze_root": str(tmp_path / "b"),
            "default_disposition": "maybe",
        },
        ctx,
    )
    assert not bad.ok and "default-disposition" in bad.errors[0]
