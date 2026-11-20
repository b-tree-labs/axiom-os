# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A switch hands the new copy the env file as it is NOW (#703).

A long-lived supervisor kept the environment it was started with, and every
copy it switched in inherited that: a key added to the unit's EnvironmentFile
never reached the serving copy until a full restart. With ``env_files`` (the
``--env-file`` option) the supervisor re-reads each file at every spawn, so a
deploy that edits the file and switches is enough. Real processes.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from pathlib import Path

from axiom.infra.switch import Supervisor, read_env_file

WORKER = textwrap.dedent(
    """
    import json, os, signal, time
    out = os.environ["OUT"]
    with open(out, "a") as fh:
        fh.write(json.dumps({k: os.environ.get(k) for k in ("A", "B")}) + "\\n")
    stop = False
    def on_term(*_):
        global stop
        stop = True
    signal.signal(signal.SIGTERM, on_term)
    fd = int(os.environ.pop("AXI_READY_FD"))
    os.write(fd, b"R"); os.close(fd)
    while not stop:
        time.sleep(0.05)
    """
)


def test_read_env_file_parses_systemd_style_lines(tmp_path: Path):
    f = tmp_path / "site.env"
    f.write_text("# comment\nA=1\nexport B='two words'\nC=\"q\"\n\nbad line\n")
    assert read_env_file(f) == {"A": "1", "B": "two words", "C": "q"}
    assert read_env_file(tmp_path / "missing.env") == {}


def test_a_switch_picks_up_a_key_added_to_the_env_file(tmp_path: Path):
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    out = tmp_path / "seen.jsonl"
    env_file = tmp_path / "site.env"
    env_file.write_text("A=1\n")
    sup = Supervisor(
        [sys.executable, str(script)],
        env={**os.environ, "OUT": str(out)},
        env_files=[env_file],
        listen=False,
        drain_s=2.0,
        ready_timeout_s=20.0,
    )
    sup.start()
    try:
        env_file.write_text("A=1\nB=added-after-boot\n")
        assert sup.switch(subject="deploy").outcome == "switched"
        time.sleep(0.2)
    finally:
        sup.stop()
    first, second = (json.loads(line) for line in out.read_text().splitlines())
    assert first == {"A": "1", "B": None}
    assert second == {"A": "1", "B": "added-after-boot"}
