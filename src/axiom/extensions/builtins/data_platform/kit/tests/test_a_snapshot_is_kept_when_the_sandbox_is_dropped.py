# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A snapshot stays when the sandbox is dropped, and materializes the same rows twice."""

from __future__ import annotations

import subprocess

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit import medallion


def test_one_snapshot_materializes_twice_and_kit_down_leaves_it(tmp_path, monkeypatch):
    kit.init(tmp_path, "line-a")
    project = kit.load(tmp_path)
    rows = [
        {"site": "line-a", "feed": "line", "channel": "fill.volume",
         "ts": "2026-09-01T00:00:00+00:00", "value": 500, "unit": "mL"},
        {"site": "line-a", "feed": "line", "channel": "fill.volume",
         "ts": "2026-09-02T00:00:00+00:00", "value": 480, "unit": "mL"},
        {"site": "other", "feed": "line", "channel": "fill.volume",
         "ts": "2026-09-01T00:00:00+00:00", "value": 1, "unit": "mL"},
        {"site": "line-a", "feed": "line", "channel": "nozzle.height",
         "ts": "2026-09-01T00:00:00+00:00", "value": 180, "unit": "mm"},
    ]
    doc = kit.take_snapshot(
        project,
        site="line-a",
        channels=["fill.volume"],
        rows=rows,
        window={"start": "2026-09-01", "end": "2026-09-03"},
    )
    first = kit.rows_to_load(kit.load_snapshot(project, doc["id"]))
    second = kit.rows_to_load(kit.load_snapshot(project, doc["id"]))
    assert first == second
    assert [row["value"] for row in first] == [500, 480]
    assert all(row["site"] == "line-a" and row["channel"] == "fill.volume" for row in first)

    monkeypatch.setattr(
        medallion,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=""),
    )
    kit.down(project)
    path = project.folder("snapshots") / f"{doc['id']}.json"
    assert path.is_file()
    assert kit.rows_to_load(kit.load_snapshot(project, doc["id"])) == first
