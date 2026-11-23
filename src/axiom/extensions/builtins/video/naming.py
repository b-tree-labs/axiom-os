# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The command a person typed, for every fix and usage line the video extension writes, in any
of its processes.

A fix names the active distribution's CLI (``acme instrument video start cam-1`` under a
product branded ``acme``), never the platform's own, and names the verb where it now sits:
under the ``instrument`` noun, as kind ``video`` (ADR-256 D4). ``cli_name()`` alone is for
fixes that name another noun (``<cli> vault set``).

The CLI process knows the program from its branding. The capture service does not: it
is a separate process (``instrument video start`` spawns it, a boot unit starts it) that
runs under the platform's default branding, so every fix it wrote (a trigger that cannot
be attached, a login that does not resolve) named the wrong command. Whoever starts it
passes the name in :data:`ENV`, and this reads that first.
"""

from __future__ import annotations

import os

#: Set by ``instrument video start`` and by the boot unit for the capture process they start.
ENV = "AXIOM_VIDEO_CLI"

#: Where video's verbs sit on the command line: the noun, then the kind (ADR-256 D4).
COMMAND_PATH = "instrument video"


def cli_name() -> str:
    """The active distribution's program (``axi``, or a consumer's), in any video process."""
    named = os.environ.get(ENV, "").strip()
    if named:
        return named
    from axiom.infra.branding import get_branding

    return get_branding().cli_name


def command() -> str:
    """``<cli> instrument video``: what every fix and usage line names before a verb."""
    return f"{cli_name()} {COMMAND_PATH}"


__all__ = ["COMMAND_PATH", "ENV", "cli_name", "command"]
