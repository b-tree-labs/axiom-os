# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A site node's `fetch` link in a dev topology: the platform's ``data.edge_pull``, on a timer.

It pulls the relay's outbox into the site node's own bronze, through an
``edge`` connector registered in that node's own state, exactly as a
deployment's schedule would. The cursor only advances past what is durable,
and rows deduplicate by hash, so a relay that goes away mid-stream loses
nothing. The key is read from ``$DEV_FETCH_TOKEN`` (the connector's
``credential_ref``) and never written anywhere.

Run as ``python -m axiom.extensions.builtins.dev.fetch_runner <config.json>``.
"""

from __future__ import annotations

import logging
import signal
import sys
import threading
import time
from pathlib import Path

from .edge_runner import read_json, write_json

TOKEN_ENV = "DEV_FETCH_TOKEN"


def main(argv: list[str]) -> int:
    from axiom.extensions.builtins.data_platform.skills import edge_pull
    from axiom.infra.skills import SkillContext, SkillRegistry

    cfg_path = Path(argv[0])
    cfg = read_json(cfg_path)
    status_file = cfg_path.with_name(cfg_path.stem.removesuffix("-config") + "-status.json")
    state_dir = Path(cfg["state_dir"])
    ctx = SkillContext(
        registry=SkillRegistry(), state_dir=state_dir, logger=logging.getLogger("dev.fetch")
    )
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    landed = 0
    while not stop.is_set():
        r = edge_pull.run({"connector": cfg["connector"], "state_dir": str(state_dir)}, ctx)
        if r.ok:
            landed += int(r.value.get("rows_landed") or 0)
        write_json(
            status_file,
            {
                "ok": r.ok,
                "rows_landed_total": landed,
                "cursor": (r.value or {}).get("cursor"),
                "error": "; ".join(r.errors)[:300] if not r.ok else "",
                "at": time.time(),
            },
        )
        stop.wait(float(cfg.get("interval_s") or 1.0))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
