# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""ActionIntent + IntentPattern — registered verb ontology.

Per spec-governance-fabric §1.3, actions name themselves with verbs from a
registered ontology, not free-form strings. The lint refuses an
`ActionEnvelope` whose intent isn't registered.

Authoring a new verb: add it to ``REGISTERED_INTENTS`` here for platform
primitives, or call ``register_intent`` from an extension's bootstrap for
extension-specific verbs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionIntent:
    """A registered verb identifying an action class.

    Stored as a dotted string ``primitive.verb`` or ``primitive.verb.qualifier``.
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value:
            raise ValueError("ActionIntent value cannot be empty")
        if "." not in self.value:
            raise ValueError(
                f"ActionIntent must be dotted (primitive.verb): {self.value!r}"
            )

    @property
    def primitive(self) -> str:
        return self.value.split(".", 1)[0]

    @property
    def verb(self) -> str:
        parts = self.value.split(".")
        return parts[1] if len(parts) > 1 else ""

    @property
    def qualifier(self) -> str | None:
        parts = self.value.split(".")
        return parts[2] if len(parts) > 2 else None

    def is_registered(self) -> bool:
        return self.value in REGISTERED_INTENTS

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class IntentPattern:
    """A pattern that matches a set of intents.

    Supported forms:

    - ``"*"``                  — matches any intent.
    - ``"primitive"``          — matches any intent under that primitive.
    - ``"primitive.*"``        — same as above; explicit form.
    - ``"primitive.verb"``     — exact match.
    """

    value: str

    def matches(self, intent: ActionIntent) -> bool:
        if self.value == "*":
            return True
        if self.value == intent.primitive:
            return True
        if self.value == f"{intent.primitive}.*":
            return True
        return self.value == intent.value


# Per spec §1.3 — the seeded ontology for the four primitives + the
# load-bearing platform verbs. Extensions register their own via
# `register_intent()`.
REGISTERED_INTENTS: set[str] = {
    # authz
    "authz.permit",
    "authz.deny",
    "authz.propose",
    # vault
    "vault.issue_capability",
    "vault.rotate_secret",
    "vault.revoke_capability",
    "vault.read_secret",
    # notifications
    "notification.send",
    "notification.deliver",
    "notification.receive",
    # schedule
    "schedule.fire",
    "schedule.skip",
    "schedule.retry",
    "schedule.dead_letter",
    # data platform
    "data_platform.read_silver",
    "data_platform.publish_gold",
    # extension dispatch
    "extension.invoke_tool",
    "extension.invoke_cmd",
    "extension.transition_state",
    # federation
    "federation.forward",
    "federation.admit_peer",
    "federation.share_fragment",
}


def register_intent(name: str) -> None:
    """Extend the registered ontology with a new intent.

    Called by an extension's bootstrap before any envelope uses the verb.
    """
    if "." not in name:
        raise ValueError(
            f"intent must be dotted (primitive.verb): {name!r}"
        )
    REGISTERED_INTENTS.add(name)


__all__ = [
    "ActionIntent",
    "IntentPattern",
    "REGISTERED_INTENTS",
    "register_intent",
]
