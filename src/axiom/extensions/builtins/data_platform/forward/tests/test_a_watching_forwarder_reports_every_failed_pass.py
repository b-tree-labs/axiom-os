# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A watching forwarder that cannot pass says so on every pass (#1344, finding 1).

In the M0 bundle rehearsal ``data forward --watch`` ran without the state
directory its connectors live in. It printed one ``no connector named ...``,
stayed Up, and wrote no status for eight minutes: a container that looked
healthy and forwarded nothing. A watch loop keeps going (one bad pass must not
end a service), but each failed pass is logged and lands in ``status.json``
with the error and how many passes in a row have failed; a good pass clears it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from axiom.extensions.builtins.data_platform.forward import Forwarder
from axiom.extensions.builtins.data_platform.skills import forward
from axiom.infra.skills import SkillContext, SkillRegistry


def _ctx(tmp_path: Path) -> SkillContext:
    return SkillContext(registry=SkillRegistry(), state_dir=tmp_path, logger=logging.getLogger("t"))


def test_each_failed_pass_is_logged_and_in_the_status_file(tmp_path, monkeypatch, caplog):
    def no_connector(self):
        raise FileNotFoundError("no connector named 'site-a' at /data/state/connectors/site-a.toml")

    monkeypatch.setattr(Forwarder, "forward_once", no_connector)
    monkeypatch.setattr(forward.time, "sleep", lambda s: None)
    outbox = tmp_path / "outbox"
    outbox.mkdir()

    with caplog.at_level(logging.WARNING):
        result = forward.run(
            {"outbox_dir": str(outbox), "state_dir": str(tmp_path / "state"),
             "box_remote": "drop:", "watch_s": 5, "max_passes": 3},
            _ctx(tmp_path),
        )

    assert result.ok is False
    logged = [r.getMessage() for r in caplog.records if "no connector named" in r.getMessage()]
    assert len(logged) == 3, "every failed pass is logged, not only the first"
    status = json.loads((tmp_path / "state" / "forward" / "status.json").read_text())
    assert "no connector named 'site-a'" in status["last_error"]
    assert status["failed_passes"] == 3
    assert status["last_error_at"]


def test_a_good_pass_clears_the_failure(tmp_path):
    from axiom.extensions.builtins.data_platform.forward import LocalOutbox
    from axiom.extensions.builtins.data_platform.sources.edge.puller import FileCursor

    (tmp_path / "outbox").mkdir()
    fwd = Forwarder(
        LocalOutbox(str(tmp_path / "outbox"), bronze_root_for=lambda s: tmp_path),
        cursor=FileCursor(tmp_path / "cursor.json"),
        intake=None,
        box=None,
        status_path=tmp_path / "status.json",
    )
    fwd.record_failure(FileNotFoundError("no connector"))
    assert json.loads((tmp_path / "status.json").read_text())["failed_passes"] == 1
    fwd.forward_once()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["last_error"] is None and status["failed_passes"] == 0
