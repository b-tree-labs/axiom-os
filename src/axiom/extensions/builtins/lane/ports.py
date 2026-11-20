# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Choosing a port pair without colliding with anyone.

The precedence ladder and the probe are lifted from a working implementation
rather than reasoned out here, because this is a problem that looks trivial
and is not: the interesting cases are an explicit override that must win, a
canonical pair somebody has bookmarked, and a port that is free in the
registry but occupied in reality.

**Bands are deliberate.** Ports are picked from a known, narrow range so the
set a project can occupy is predictable — you can firewall it, document it,
and recognise it in `lsof`. A free-for-all across the ephemeral range is
easier to write and impossible to reason about afterwards.

**The probe is a TCP connect, not a process listing.** Parsing `lsof` tells
you who owns a socket, which `doctor` wants, but it needs `lsof` to exist and
it is slow. Asking whether the port accepts a connection is the actual
question here, and it is right even when the listener is something we cannot
see.

One deliberate difference from the source: when the whole band is busy this
RAISES rather than falling back to the base port. Falling back gives a
developer something that starts, which is the friendly choice for a single
app; for parallel lanes it hands two of them the same port and the second one
loses silently. That is the failure this module exists to prevent, so it must
not be the failure mode of last resort.
"""

from __future__ import annotations

import socket

from . import naming


def is_free(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    """True when nothing accepts a connection on `port`.

    A connect that is refused means free. Anything else — a live listener, a
    timeout, a host that filters — is treated as taken, because the cost of
    guessing "free" wrongly is two servers fighting over a socket.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        try:
            s.connect((host, port))
        except ConnectionRefusedError:
            return True
        except OSError:
            return False
        return False


class NoPortsFree(RuntimeError):
    """Every pair in the band is reserved, claimed or listening."""


def resolve(
    name: str,
    *,
    taken: set[int],
    override: tuple[int, int] | None = None,
    canonical: tuple[int, int] | None = None,
    probe=is_free,
    stride: int = 2,
) -> tuple[int, int]:
    """A free pair for `name`, by precedence.

    1. **`override`** — an explicit flag or environment variable. Honoured
       even if it looks busy: somebody asking for a specific port by name has
       a reason, and second-guessing them is how a tool becomes untrustworthy.
       They get the bind error, which says more than we could.
    2. **`canonical`** — the pair this lane had before, when it is genuinely
       free. Keeps a bookmarked URL working across a restart.
    3. **the name's preferred slot, then forward** — stable enough that a
       checkout tends to return to the same place, and probed so it never
       collides.
    """
    if override is not None:
        return override

    busy = set(taken)
    if canonical and not (set(canonical) & busy) and all(probe(p) for p in canonical):
        return canonical

    band = range(naming.FIRST_LANE_PORT, naming.LAST_LANE_PORT + 1, stride)
    start = naming.preferred_port(name, stride=stride)
    ordered = [p for p in band if p >= start] + [p for p in band if p < start]

    for front in ordered:
        api = front + 1
        if front in busy or api in busy:
            continue
        if probe(front) and probe(api):
            return front, api

    raise NoPortsFree(
        f"no free pair in {naming.FIRST_LANE_PORT}-{naming.LAST_LANE_PORT}. "
        "Release a lane, stop a stray server, or widen the band — this will not "
        "silently reuse a port somebody else is on."
    )


__all__ = ["NoPortsFree", "is_free", "resolve"]
