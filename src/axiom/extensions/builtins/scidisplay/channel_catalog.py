# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The resolver seam: what a chart spec is checked against.

A spec naming a channel that does not exist must fail loudly, naming the channel
and the site, rather than drawing an empty picture. That difference is the whole
distance between a tool a researcher trusts and one they check by hand every
time. Checking needs a catalog, and a catalog is domain-side, so this module
declares the interface and nothing more.

**This is a protocol, not an implementation.** Nothing here reaches a store; a
test pins that. An implementation is expected to union two sources with different
authority: a distinct-value query over a catalog table, authoritative for whether
a channel *exists*, and per-site channel maps keyed by schema reference,
authoritative for what a channel *means*. Hence :class:`ChannelInfo` carries a
name that is always known and three descriptive fields that may not be.

Three answers, not two. A channel that does not exist is ``None``. A catalog
that cannot answer right now raises :class:`ResolverUnavailable`, which callers
turn into "unverified" rather than "does not exist" -- an outage that read as a
missing channel would send somebody hunting for a naming bug that was never
there.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ChannelInfo:
    """What a catalog knows about one channel.

    Only ``name`` is guaranteed. The rest is description, and a catalog that is
    authoritative for existence may hold none of it.
    """

    name: str
    unit: str | None = None
    description: str | None = None
    stream: str | None = None


class ResolverUnavailable(RuntimeError):
    """The catalog cannot answer right now.

    Not a :class:`ValueError`: nothing about the spec is wrong. Raising this is
    how an implementation says "ask again later" without a caller mistaking an
    outage for a verdict about a channel.
    """


@runtime_checkable
class ChannelResolver(Protocol):
    """Where channel names are checked, and where completions come from."""

    def resolve_channel(self, site: str, channel: str) -> ChannelInfo | None:
        """Return what is known about ``channel`` at ``site``, or ``None``.

        ``None`` means the catalog is confident the channel does not exist.
        Raise :class:`ResolverUnavailable` when it cannot say.
        """
        ...

    def list_channels(self, site: str) -> Sequence[str]:
        """Every channel name at ``site``, for completion and for suggestions.

        Raise :class:`ResolverUnavailable` when the catalog cannot say. An empty
        sequence means the site is known and holds nothing.
        """
        ...


__all__ = ["ChannelInfo", "ChannelResolver", "ResolverUnavailable"]
