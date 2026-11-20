# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Which providers may hold which content (ADR-054, ADR-168, issue #1136).

One rule, used by the model switch and by provider selection, so they cannot disagree:

    content tier       admitted by provider tier
    -----------------  -----------------------------------------
    public / any       public, any, private, export_controlled
    export_controlled  export_controlled ONLY (``any`` and ``private`` are not cleared for controlled content)

``private`` (RS-E5, issue #545) is a model on the private network. It is a tier of its own, not a
controlled one: the deprecated ``restricted`` normalises to it, not to ``export_controlled``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

CONTROLLED = "export_controlled"


def admits(provider_tier: str, content_tier: str) -> bool:
    """True when a provider of ``provider_tier`` may receive content of ``content_tier``."""
    if content_tier == CONTROLLED:
        return provider_tier == CONTROLLED
    return True


@dataclass(frozen=True)
class SwitchDecision:
    allowed: bool
    provider: str
    content_tier: str
    reason: str
    admitted: tuple[
        str, ...
    ] = ()  # the providers that WOULD be admitted, so a refusal is actionable


class ProviderNotAdmitted(PermissionError):
    """Raised by a provider switch the content's tier forbids. Carries the decision."""

    def __init__(self, decision: SwitchDecision):
        super().__init__(decision.reason)
        self.decision = decision


def decide_switch(providers: Iterable, name: str, content_tier: str = "any") -> SwitchDecision:
    """Whether the session may switch to provider ``name``, given the tier of what it already holds."""
    listed = list(providers)
    target = next((p for p in listed if p.name == name), None)
    if target is None:
        return SwitchDecision(False, name, content_tier, f"no provider named {name!r}")
    admitted = tuple(p.name for p in listed if admits(p.routing_tier, content_tier))
    if admits(target.routing_tier, content_tier):
        return SwitchDecision(True, name, content_tier, "admitted", admitted)
    options = ", ".join(admitted) if admitted else "none is configured"
    return SwitchDecision(
        False,
        name,
        content_tier,
        f"{name} (tier {target.routing_tier}) may not receive {content_tier} content that this session "
        f"already holds. Providers that may: {options}.",
        admitted,
    )


# ---- where an endpoint may send content (RS-E2: embeddings pass the same rule) -----------------

#: Hosts that are this machine: content sent there does not leave it.
_THIS_MACHINE = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})

_providers_cache: dict[str, tuple[int, tuple[tuple[str, str], ...]]] = {}


def on_this_machine(url: str) -> bool:
    """True when ``url`` names this machine, so what is sent there never leaves it."""
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _THIS_MACHINE


def _declared_providers() -> tuple[tuple[str, str], ...]:
    """``(host, routing_tier)`` for every provider in the gateway's llm-providers.toml.

    Read the way the gateway finds the file (``default_providers_dir``), with its tier aliases, and
    never written to. Cached until the file changes. Unreadable or absent: nothing is declared.
    """
    from urllib.parse import urlsplit

    from axiom.infra.toml_compat import tomllib
    from axiom.llm.gateway import _ROUTING_TIER_ALIASES, PROVIDERS_FILENAME, default_providers_dir

    try:
        base = default_providers_dir()
        path = base / PROVIDERS_FILENAME
        if not path.exists():
            path = base / "models.toml"
        stamp = path.stat().st_mtime_ns
    except (OSError, ValueError):
        return ()
    key = str(path)
    hit = _providers_cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        with open(path, "rb") as fh:
            providers = (tomllib.load(fh).get("gateway") or {}).get("providers") or []
    except (OSError, ValueError):
        return ()
    rows: list[tuple[str, str]] = []
    for p in providers:
        if not isinstance(p, dict) or not p.get("endpoint"):
            continue
        try:
            host = (urlsplit(str(p["endpoint"])).hostname or "").lower()
        except ValueError:
            continue
        if host:
            tier = str(p.get("routing_tier", "any") or "any")
            rows.append((host, _ROUTING_TIER_ALIASES.get(tier, tier)))
    _providers_cache[key] = (stamp, tuple(rows))
    return tuple(rows)


def endpoint_clearance(url: str) -> str:
    """The tier of content the machine at ``url`` may receive, read from the gateway's providers file.

    The clearance is the ``routing_tier`` of the providers declared on the same host. When several
    are, the host is cleared for controlled content only if every one of them says so (fail closed).
    A host the file does not name is ``public``: it may take any content but controlled content.
    """
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return "public"
    tiers = {tier for h, tier in _declared_providers() if h == host}
    return CONTROLLED if tiers == {CONTROLLED} else "public"


def endpoint_admits(url: str, content_tier: str) -> bool:
    """Whether content of ``content_tier`` may be sent to ``url``: this machine always; elsewhere, by ``admits``."""
    return on_this_machine(url) or admits(endpoint_clearance(url), content_tier)
