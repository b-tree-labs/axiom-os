# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""HERALD skills — invocable through the SkillRegistry per ADR-056.

Each verb of ``axi notifications`` resolves to a skill function::

    notifications.send       → send.run
    notifications.list       → list_inbox.run
    notifications.channels   → list_channels.run

Skills are the executable unit; agent personas + the CLI call the same
surface. Mirrors ``data_platform.skills``.
"""

from __future__ import annotations

from axiom.infra.skills import SkillRegistry, default_registry

from . import (
    list_channels,
    setup_channel,
    list_inbox,
    recipient_list,
    recipient_set,
    recipient_show,
    send,
)

_NAMESPACE = "notifications"

_SKILLS = {
    "send": send.run,
    "list": list_inbox.run,
    "channels": list_channels.run,
    "setup": setup_channel.run,
    "recipient_set": recipient_set.run,
    "recipient_show": recipient_show.run,
    "recipient_list": recipient_list.run,
}

# Optional connector_* skills (parallel feature track on main). Lazy
# import so the recipient-preferences track is independent if those
# modules aren't present in some branch.
try:  # pragma: no cover - parallel track wiring
    from . import connector_add, connector_reconnect, connector_status

    _SKILLS["connector_add"] = connector_add.run
    _SKILLS["connector_reconnect"] = connector_reconnect.run
    _SKILLS["connector_status"] = connector_status.run
except Exception:  # pragma: no cover
    pass


def bind(registry: SkillRegistry) -> None:
    for verb, fn in _SKILLS.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        registry.register(name, fn)


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


__all__ = ["bind", "bind_default"]
