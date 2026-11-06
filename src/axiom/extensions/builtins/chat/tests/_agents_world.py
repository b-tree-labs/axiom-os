# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A tiny real extension tree on disk for the /agents tests: real manifests, real discovery, real files."""

from __future__ import annotations

import json
from pathlib import Path

from axiom.extensions.discovery import discover_extensions

SCHEDULED = """
[extension]
name = "{name}"
version = "0.1.0"
description = "{name} extension"
builtin = true

[[extension.provides]]
kind = "agent"
name = "{name}"
persona = "persona.md"
description = "{name} does one job."

[agent]
{interval}
startup = "daemon"
heartbeat_command = "{name} beat"
default_consent = "core"
"""

ON_DEMAND = """
[extension]
name = "{name}"
version = "0.1.0"
description = "{name} extension"
builtin = true

[[extension.provides]]
kind = "agent"
name = "{name}"
persona = "persona.md"
description = "{name} answers when asked."
"""


def make_ext(root: Path, name: str, *, scheduled: bool = True, interval: int | None = 300,
             persona: str | None = "# {n} - The Tester\n\n{n} checks things.\nIt never sleeps.\n") -> Path:
    d = root / name
    d.mkdir(parents=True)
    if scheduled:
        line = f"heartbeat_interval = {interval}" if interval is not None else ""
        (d / "axiom-extension.toml").write_text(SCHEDULED.format(name=name, interval=line))
    else:
        (d / "axiom-extension.toml").write_text(ON_DEMAND.format(name=name))
    if persona is not None:
        (d / "persona.md").write_text(persona.format(n=name.upper()))
    return d


def discover(root: Path) -> list:
    return list(discover_extensions(root))


def write_dispatch_state(agents_dir: Path, times: dict[str, float]) -> None:
    state = agents_dir / ".background-service" / "state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps(times))
