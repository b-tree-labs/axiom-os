# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-prompt provider-override parsing (spec-chat-model-picker §3).

Two syntaxes (mutually exclusive per turn) let users override the
gateway's session/default routing for a single prompt:

    @anthropic summarize the tradeoffs between these two designs
    /m local-qwen summarize the tradeoffs between these two designs

After parsing, callers validate the extracted name against the live
gateway provider list (see `resolve_provider_name`). Unknown names get
an inline error via `format_unknown_provider_error` before any turn
work begins.

This module is intentionally pure: no gateway, no agent, no I/O. The
agent (chat extension) wires it into `turn()` and applies the resolved
override via `gateway.set_provider_override` for the duration of one
turn only.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

# Provider names: alphanumeric + hyphen + underscore, per spec §3.1.
_AT_PREFIX_RE = re.compile(r"^\s*@([A-Za-z0-9_\-]+)(?:\s+(.*))?$", re.DOTALL)
# `/m <name> <prompt>` — requires the space after `/m` so `/model` does
# not match. Per spec §3.2.
_SLASH_M_RE = re.compile(r"^\s*/m\s+([A-Za-z0-9_\-]+)(?:\s+(.*))?$", re.DOTALL)


PickerSyntax = Literal["at", "slash_m"]


@dataclass(frozen=True)
class OverrideParseResult:
    """Outcome of attempting to extract a per-prompt override from user input.

    - `override_name`: the literal name as the user typed it, or None
      if no override syntax matched. Caller resolves to canonical form
      via `resolve_provider_name`.
    - `stripped_prompt`: the prompt with the override prefix removed,
      ready to send to the LLM.
    - `syntax`: which form matched, for audit-log fidelity.
    """

    override_name: str | None
    stripped_prompt: str
    syntax: PickerSyntax | None


def parse_per_prompt_override(text: str) -> OverrideParseResult:
    """Extract an `@provider` or `/m provider` prefix from a chat prompt.

    Pure parser — no validation of the provider name. Returns a result
    with `override_name=None` when no override syntax matches.
    """
    m = _SLASH_M_RE.match(text)
    if m:
        return OverrideParseResult(
            override_name=m.group(1),
            stripped_prompt=(m.group(2) or "").strip(),
            syntax="slash_m",
        )
    m = _AT_PREFIX_RE.match(text)
    if m:
        return OverrideParseResult(
            override_name=m.group(1),
            stripped_prompt=(m.group(2) or "").strip(),
            syntax="at",
        )
    return OverrideParseResult(
        override_name=None, stripped_prompt=text, syntax=None
    )


def resolve_provider_name(
    typed_name: str, known_provider_names: list[str]
) -> str | None:
    """Match a user-typed name against the live provider list.

    Per spec §3.1: case-insensitive against the gateway provider `name`
    field. Returns the canonical (configured) name on match, or None
    when nothing matches.
    """
    lowered = typed_name.lower()
    for name in known_provider_names:
        if name.lower() == lowered:
            return name
    return None


def format_unknown_provider_error(
    typed_name: str, known_provider_names: list[str]
) -> str:
    """Build the inline error message shown when a per-prompt override
    names a provider the gateway doesn't know about.

    Spec §3.1 example:
        Unknown provider 'foo'. Known: anthropic, local-qwen, openai.
        Use /model to list with status.
    """
    if known_provider_names:
        known = ", ".join(known_provider_names)
        return (
            f"Unknown provider {typed_name!r}. Known: {known}. "
            "Use /model to list with status."
        )
    return (
        f"Unknown provider {typed_name!r}. No providers are configured. "
        "Use /model to list with status."
    )


# ---------------------------------------------------------------------------
# One-turn override applier — context manager wrapper for agent.turn()
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _PickerOutcome:
    """Result the context manager yields into the `with` block.

    - `stripped_prompt`: prompt minus the override prefix; what the agent
      sends to the LLM.
    - `error_message`: human-readable error when the user typed an
      unknown provider name; agent returns this to the chat instead of
      sending a turn.
    - `override_applied`: canonical provider name now active on the
      gateway, or None when no override was requested.
    """

    stripped_prompt: str
    error_message: str | None
    override_applied: str | None


@contextmanager
def apply_per_prompt_override(user_input: str, gateway: Any) -> Iterator[_PickerOutcome]:
    """Parse + apply a per-prompt override for the duration of one turn.

    On enter: parses the prefix; if a valid override was requested,
    stashes the gateway's current `_provider_override` and switches to
    the resolved name. If the user named an unknown provider, the
    outcome carries an `error_message` and the gateway is left
    untouched — the agent should return the message instead of running
    the turn.

    On exit (success or exception): restores the gateway's prior
    `_provider_override` value via `set_provider_override`. The
    override applies only to this single turn.
    """
    parsed = parse_per_prompt_override(user_input)

    if parsed.override_name is None:
        yield _PickerOutcome(
            stripped_prompt=parsed.stripped_prompt,
            error_message=None,
            override_applied=None,
        )
        return

    known = [p.name for p in getattr(gateway, "providers", [])]
    canonical = resolve_provider_name(parsed.override_name, known)
    if canonical is None:
        yield _PickerOutcome(
            stripped_prompt=parsed.stripped_prompt,
            error_message=format_unknown_provider_error(parsed.override_name, known),
            override_applied=None,
        )
        return

    prior = getattr(gateway, "_provider_override", None)
    gateway.set_provider_override(canonical)
    try:
        yield _PickerOutcome(
            stripped_prompt=parsed.stripped_prompt,
            error_message=None,
            override_applied=canonical,
        )
    finally:
        gateway.set_provider_override(prior)
