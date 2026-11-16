# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``docker rm -f`` leaves a container's anonymous volumes behind; ``-v`` removes them.

The Postgres and TimescaleDB images declare ``VOLUME /var/lib/postgresql/data``, so every
test database and every kit sandbox stopped this way left a volume on the host. ``--rm`` on
``docker run`` does not help: it applies when the container stops, not when it is force-removed.

``-v`` touches only anonymous volumes. A named volume or a bind mount survives it, so a
vault that keeps its state in a bind mount loses nothing.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: argv forms (``"docker", "rm", "-f"``, ``_docker("rm", "-f"``) and the shell form.
LEAKS = re.compile(
    r"""["']docker["'],\s*["']rm["'],\s*["']-f["'](?!,\s*["']-v["'])"""
    r"""|_docker\(\s*["']rm["'],\s*["']-f["'](?!,\s*["']-v["'])"""
    r"""|docker rm -f(?! -v)(?=\s)"""
)
THIS_FILE = Path(__file__).resolve()


def _tracked() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.py", "*.sh", "*.yml", "*.yaml"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO / line for line in out.splitlines() if line]


def test_no_teardown_force_removes_a_container_and_leaves_its_volumes():
    offenders = []
    for path in _tracked():
        if path == THIS_FILE or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in LEAKS.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{path.relative_to(REPO)}:{line}")
    assert not offenders, "docker rm -f without -v leaks anonymous volumes:\n" + "\n".join(offenders)


def test_the_guard_can_fail():
    assert LEAKS.search('["docker", "rm", "-f", name]')
    assert LEAKS.search('_docker("rm", "-f", name, check=False)')
    assert LEAKS.search("docker rm -f tsdb-1 ")
    assert not LEAKS.search('["docker", "rm", "-f", "-v", name]')
    assert not LEAKS.search('_docker("rm", "-f", "-v", name, check=False)')
    assert not LEAKS.search("docker rm -f -v tsdb-1 ")
