# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Prebuilt groups: groups a node provides to everyone who may use its Charts page.

A saved group is one account's (``saved``). A prebuilt group is the node's: registered once
by configuration, built when asked for, and the same for every signed-in account, so nothing
is copied into anybody's saved-views store and a new account sees it with no setup. The page
lists these beside the account's own groups and opens them the same way; an edit made while
one is open belongs to the account's working page, never to the group.

A provider is a function ``(state_dir) -> [group document]``. The first is the chart-kinds
registry (``charts kinds-group --config``), the second the role groups (a product's template
filled by a site's role map, ``charts role-group --config``), the third the demo gallery (:mod:`.demo`);
another registry joins by appending to :data:`PROVIDERS`. Nothing here names what a group holds.

Every group says what it is and whether the Charts navigation lists it: ``kind`` (``demo``,
``site`` or ``personal``) and ``visibility`` (``shown`` or ``hidden``). A hidden group is still
listed here, because search and a direct link open it; only the navigation leaves it out.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

log = logging.getLogger("axi.charts.prebuilt")

Provider = Callable[[Path], "list[dict[str, Any]]"]


def _kinds(state_dir: Path) -> list[dict[str, Any]]:
    from . import kinds_group as kg

    out: list[dict[str, Any]] = []
    for cfg in kg.registered_all(state_dir):
        try:
            out.append(kg.build_group(cfg))
        except kg.KindsError as exc:  # e.g. the recorded day is not yet old enough to replay
            log.warning("prebuilt group for site %s not offered: %s", cfg.site, exc)
    return out


def _roles(state_dir: Path) -> list[dict[str, Any]]:
    from . import role_groups

    return role_groups.provider(state_dir)


def _demo(state_dir: Path) -> list[dict[str, Any]]:
    from . import demo
    from . import kinds_group as kg

    try:
        return demo.groups(state_dir)
    except kg.KindsError as exc:  # a broken shipped config is a bug, said in the log, never a dead page
        log.warning("the demo gallery is not offered: %s", exc)
        return []


#: Where prebuilt groups come from. Append a provider; do not special-case a group.
PROVIDERS: list[Provider] = [_kinds, _roles, _demo]


def listing(state_dir: Path) -> list[dict[str, Any]]:
    """Every prebuilt group on this node: the group documents the Charts page opens, by id and name."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for provider in PROVIDERS:
        for group in provider(Path(state_dir)):
            gid = str(group.get("id") or group.get("name") or "")
            if gid and gid not in seen:
                seen.add(gid)
                out.append({"kind": "site", "visibility": "shown", **group, "prebuilt": True})
    return out
