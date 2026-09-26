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

from axiom.infra.skills import (
    SkillRegistry,
    SkillSpec,
)
from axiom.infra.skills import (
    default_registry as default_registry,  # re-export
)

from . import (
    ack,
    alert,
    list_channels,
    list_inbox,
    recipient_list,
    recipient_set,
    recipient_show,
    send,
    setup_channel,
)

_NAMESPACE = "notifications"

#: Every capability, DECLARED. These were registered bare — no `SkillSpec`, no
#: `surfaces` — which made all of them CLI-only by OMISSION rather than by
#: decision. The monitor case is precisely "an agent notices something and
#: tells a human", and the agent could not reach the verb that does it.
#:
#: `surfaces` is the bounded-exposure control (ADR-072 §4.9.4): a capability
#: reaches a surface only by naming it. `side_effects` is what puts a verb
#: behind the confirm gate on every surface at once — withholding a surface
#: HIDES a dangerous verb where declaring the effect GATES it, and those are
#: not the same safety property.
_ALL = ("cli", "mcp", "agent_tool", "skill_md")
_HUMAN_ONLY = ("cli", "skill_md")

_SPECS: dict[str, dict] = {
    "alert": {
        "fn": alert.run,
        "description": "Post a durable alert to a principal's inbox.",
        "inputs": {
            "recipient": "principal to alert (@name:context)",
            "summary": "what happened, as the operator will read it",
            "priority": "low | normal | high | urgent",
            "classification": "public | internal | regulated | controlled",
            "actor": "which monitor is reporting",
            "link": "optional URL for more information or to act",
        },
        "surfaces": _ALL,
        "side_effects": True,
        "idempotent": False,
    },
    "list": {
        "fn": list_inbox.run,
        "description": "List inbox rows for a recipient.",
        "inputs": {
            "recipient": "whose inbox to read",
            "unread_only": "only rows not yet read",
            "max_classification": "ceiling to filter by",
            "limit": "maximum rows",
        },
        "surfaces": _ALL,
        "side_effects": False,
        "idempotent": True,
    },
    "ack": {
        "fn": ack.run,
        "description": "Acknowledge an alert — record that a person took it up.",
        "inputs": {
            "id": "alert id (the short form shown in chat is enough)",
            "recipient": "who is acknowledging",
        },
        # NOT agent-reachable, and this is the sharpest call here.
        # Acknowledging means a PERSON took responsibility. If an agent can
        # ack, the record "somebody picked this up" becomes false — and that
        # record is the only reason the column is worth having. Withholding
        # the surface is right precisely because the harm is not a side effect
        # to be gated; it is the meaning of the data being destroyed.
        "surfaces": _HUMAN_ONLY,
        "side_effects": True,
        "idempotent": True,
    },
    "channels": {
        "fn": list_channels.run,
        "description": "List configured notification channels.",
        "inputs": {},
        "surfaces": _ALL,
        "side_effects": False,
        "idempotent": True,
    },
    "send": {
        "fn": send.run,
        "description": "Dispatch a notification through channel routing.",
        "inputs": {
            "recipient": "principal to notify",
            "summary": "short summary surfaced in the inbox",
            "body": "optional longer body",
            "classification": "public | internal | regulated | controlled",
            "priority": "low | normal | high | urgent",
            "intent": "notification intent",
            "dedup_key": "suppress duplicates within the window",
        },
        # Agent-reachable WITH the side effect declared, rather than hidden.
        "surfaces": _ALL,
        "side_effects": True,
        "idempotent": False,
    },
    "setup": {
        "fn": setup_channel.run,
        # Writes credentials. Identity-adjacent: an agent that can change how
        # a human is reached can change WHO is reached.
        "description": "Configure a notification channel (writes credentials).",
        "inputs": {"channel": "channel to configure", "mode": "setup mode"},
        "surfaces": _HUMAN_ONLY,
        "side_effects": True,
        "idempotent": False,
    },
    "recipient_set": {
        "fn": recipient_set.run,
        "description": "Set where a recipient is reached (writes routing).",
        "inputs": {"recipient": "principal", "channels": "ordered channel list"},
        "surfaces": _HUMAN_ONLY,
        "side_effects": True,
        "idempotent": False,
    },
    "recipient_show": {
        "fn": recipient_show.run,
        "description": "Show a recipient's channel preferences.",
        "inputs": {"recipient": "principal"},
        "surfaces": _ALL,
        "side_effects": False,
        "idempotent": True,
    },
    "recipient_list": {
        "fn": recipient_list.run,
        "description": "List known recipients.",
        "inputs": {},
        "surfaces": _ALL,
        "side_effects": False,
        "idempotent": True,
    },
}

_SKILLS = {verb: spec["fn"] for verb, spec in _SPECS.items()}

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
        declared = _SPECS.get(verb)
        if declared is None:
            # A connector_* skill from the parallel track: registered bare
            # until it declares, rather than given surfaces it did not ask for.
            registry.register(name, fn)
            continue
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=fn,
                description=declared["description"],
                inputs=declared["inputs"],
                surfaces=declared["surfaces"],
                side_effects=declared["side_effects"],
                idempotent=declared["idempotent"],
            )
        )


def bind_default() -> SkillRegistry:
    reg = default_registry()
    bind(reg)
    return reg


__all__ = ["bind", "bind_default"]
