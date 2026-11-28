# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The demo gallery: a prebuilt group of kind ``demo``, one chart of every kind, on any node.

It is the chart-kinds group (:mod:`.kinds_group`) built from :file:`demo_gallery.toml`, which
ships with the platform and reads only the generated ``sample`` source, so a bare node with no
site config and no gold has a Charts page that shows what every kind looks like. Every chart is
drawn by the real renderer through the same chart-data verb a site's group uses, and every chart
says SAMPLE.

**Shown or hidden is one switch:** the ``charts_demo_gallery`` flag (declared in this
extension's manifest). On by default, so a bare node lists the group in its Charts navigation; a
product or a site turns it off and the group is hidden there, while search and a direct link
still open it.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

#: The flag that decides whether the demo group is listed in the Charts navigation.
FLAG = "charts_demo_gallery"
#: The site id the shipped config names; resolving a chart of the group finds the config by it.
SITE = "sample"
GROUP_ID = f"kinds-{SITE}"
CONFIG_PATH = Path(__file__).with_name("demo_gallery.toml")


@lru_cache(maxsize=1)
def config():
    from .kinds_group import KindsConfig

    return KindsConfig.load(CONFIG_PATH)


def shown() -> bool:
    """Whether the flag lists the demo group in the navigation (an unreadable store keeps the default: on)."""
    from axiom.extensions.builtins.flags.service import env_override

    pinned = env_override(FLAG)
    if pinned is not None:
        return pinned == "on"
    try:
        from axiom.extensions.builtins.flags.service import FlagService

        return FlagService().resolve_flag(FLAG, node=os.environ.get("AXIOM_NODE") or None) == "on"
    except Exception:  # noqa: BLE001 - the flag store is optional; its default is what a bare node gets
        return True


def groups(state_dir: Path) -> list[dict[str, Any]]:
    """The demo gallery as a prebuilt group: a provider for :data:`.prebuilt.PROVIDERS`."""
    from .kinds_group import build_group

    group = build_group(config())
    group["visibility"] = "shown" if shown() else "hidden"
    return [group]


__all__ = ["CONFIG_PATH", "FLAG", "GROUP_ID", "SITE", "config", "groups", "shown"]
