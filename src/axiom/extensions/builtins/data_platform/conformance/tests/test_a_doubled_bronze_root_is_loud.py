# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A bronze root that holds no rows is loud, never "clean" (contingency C-51).

A doubled folder (the data under ``.../bronze/bronze`` while conform reads
``.../bronze``, or the reverse) made conform find 0 rows and report
``clean: 0 rows conformed``. Silver stopped growing for the site and nothing
said why. Now the pass says which root was empty and why it looks wrong: in
the command's output, in the node's status, and in the log. A missing or
doubled root fails a strict run; a root that is simply empty (a node before
its first reading) warns without failing.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from axiom.extensions.builtins.data_platform.conformance import (
    NormalizerRegistry,
    conform_rows,
)
from axiom.extensions.builtins.data_platform.conformance.runner import conform_verdict

ROW = {"schema_ref": "s/v1", "row_hash": "h1", "row": {"ts": "2026-10-08T12:00:00Z", "v": 1}}


def _land(root: Path, connector: str = "site-a-live") -> None:
    day = root / connector / "_rows" / "2026-10-08"
    day.mkdir(parents=True)
    (day / "item-1.jsonl").write_text(json.dumps(ROW) + "\n")


def _verdict(root: Path, *, strict: bool = True):
    stats = conform_rows(root, NormalizerRegistry(), {"site-a-live": "site-a"}, upsert=lambda r: None)
    return stats, conform_verdict(stats, strict=strict)


def test_a_root_one_folder_short_of_the_data_is_not_clean(tmp_path):
    # The data landed under bronze/bronze; conform reads bronze.
    _land(tmp_path / "bronze" / "bronze")
    stats, v = _verdict(tmp_path / "bronze")
    assert stats["rows_in"] == 0
    assert not v.ok
    assert not any(m.startswith("clean") for m in v.messages)
    said = " ".join(v.messages)
    assert "doubled" in said and str(tmp_path / "bronze" / "bronze") in said


def test_a_doubled_path_that_does_not_exist_is_not_clean(tmp_path):
    # The reverse: the data is under bronze; conform reads bronze/bronze.
    _land(tmp_path / "bronze")
    _, v = _verdict(tmp_path / "bronze" / "bronze")
    assert not v.ok
    said = " ".join(v.messages)
    assert "doubled" in said and "bronze/bronze" in said


def test_a_genuinely_empty_root_warns_without_failing(tmp_path):
    (tmp_path / "bronze").mkdir()
    _, v = _verdict(tmp_path / "bronze")
    assert v.ok  # a node before its first reading is not misconfigured
    assert not any(m.startswith("clean") for m in v.messages)
    assert any("holds no rows" in m for m in v.messages)


def test_a_root_with_rows_is_unchanged(tmp_path):
    _land(tmp_path / "bronze")
    stats, v = _verdict(tmp_path / "bronze")
    assert stats["rows_in"] == 1 and stats["connectors_with_rows"] == ["site-a-live"]
    assert not any("holds no rows" in m for m in v.messages)


def test_the_real_pass_says_so_in_its_output_the_node_status_and_the_log(
    tmp_path, timescale_dsn, monkeypatch, caplog, capsys
):
    """End to end on a real database: the CLI verb, then `status`, then the log."""
    from axiom.extensions.builtins.data_platform import cli
    from axiom.extensions.builtins.status import function_status

    state = tmp_path / "state"
    monkeypatch.setenv("AXI_STATE_DIR", str(state))
    _land(tmp_path / "bronze" / "bronze")

    with caplog.at_level(logging.WARNING):
        code = cli.main(["conform-run", "--bronze-root", str(tmp_path / "bronze"),
                         "--dsn", timescale_dsn])
    out = capsys.readouterr().out

    assert code != 0, out
    assert "doubled" in out and "clean" not in out
    assert any("doubled" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)

    sections = function_status.sections(SimpleNamespace(confined=True, settings={}))
    conform = next(s for s in sections if s["title"] == "Conform")
    row = conform["rows"][0]
    assert row["label"] == str(tmp_path / "bronze")
    assert row["state"] == "fail" and "doubled" in row["value"]


@pytest.fixture(autouse=True)
def _no_ambient_dsn(monkeypatch):
    # Never the developer's database: the real-DB test passes its own DSN.
    for name in ("DP1_RAG_DSN", "DATABASE_URL", "AXIOM_DB_URL"):
        monkeypatch.delenv(name, raising=False)
