# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where a provider runs: ``local``, ``cloud`` or ``remote-node`` (ADR-168).

A user bound to a site node, or one who switches models, cannot otherwise tell whether a reply came
from a model on their own machine, a cloud service, or a remote node's. This module answers that
from the provider's configuration alone: no network call, no side effect.

Rules, in order:

1. An explicit ``placement`` setting wins (a federation adoption writes ``remote-node``).
2. A loopback endpoint is ``local``.
3. A subscription CLI provider (``claude-code``) is ``cloud``: the process runs here, the model does not.
4. A known cloud API host is ``cloud``.
5. A private address, a ``.local`` or single-label name, or ``requires_vpn`` is a ``remote-node``.
6. Anything else is a ``remote-node`` that is **not verified**: we know it is another machine, not
   whether it is a cloud service or a node. It is never reported as local.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

Placement = Literal["local", "cloud", "remote-node"]
PLACEMENTS: tuple[str, ...] = ("local", "cloud", "remote-node")

_LOOPBACK_NAMES = frozenset({"localhost", "ip6-localhost", "0.0.0.0", "::1", "::"})
_CLOUD_SUFFIXES = (
    "anthropic.com",
    "openai.com",
    "googleapis.com",
    "openai.azure.com",
    "azure.com",
    "amazonaws.com",
    "groq.com",
    "mistral.ai",
    "x.ai",
    "cohere.com",
    "cohere.ai",
    "openrouter.ai",
    "deepseek.com",
    "perplexity.ai",
    "fireworks.ai",
    "together.xyz",
    "together.ai",
    "huggingface.co",
    "cerebras.ai",
    "replicate.com",
    "sambanova.ai",
    "nvidia.com",
)


@dataclass(frozen=True)
class ProviderPlacement:
    placement: Placement
    host: str  # the machine a user would recognise; empty only when no endpoint is configured
    verified: bool  # False when this is a guess (an unrecognised public host)
    source: Literal["setting", "derived"]

    def label(self) -> str:
        """``LOCAL``, ``CLOUD api.example.com``, ``REMOTE node.example.edu`` (a trailing ``?`` marks a guess)."""
        word = {"local": "LOCAL", "cloud": "CLOUD", "remote-node": "REMOTE"}[self.placement]
        suffix = "" if self.verified else "?"
        if self.placement == "local" or not self.host:
            return word + suffix
        return f"{word}{suffix} {self.host}"


def _host_of(endpoint: str) -> str:
    if not isinstance(endpoint, str):  # a provider whose endpoint is not a string has no host to read
        return ""
    try:
        return (urlsplit(endpoint).hostname or "").lower()
    except ValueError:
        return ""


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_private_or_unqualified(host: str) -> bool:
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_private or addr.is_link_local
    except ValueError:
        pass
    return host.endswith((".local", ".internal", ".lan", ".home.arpa")) or (
        "." not in host and bool(host)
    )


def _is_cloud_host(host: str) -> bool:
    return any(host == s or host.endswith("." + s) for s in _CLOUD_SUFFIXES)


def placement_of(provider) -> ProviderPlacement:
    """The placement of ``provider``, from its configuration alone."""
    endpoint = (getattr(provider, "endpoint", "") or "").strip()
    host = _host_of(endpoint)
    setting = getattr(provider, "placement", "") or ""
    if setting in PLACEMENTS:
        shown = (
            getattr(provider, "node_host", "") or host or ("" if setting == "local" else endpoint)
        )
        return ProviderPlacement(setting, shown, True, "setting")  # type: ignore[arg-type]
    if endpoint.startswith("claude-code"):
        return ProviderPlacement("cloud", "claude-code subscription", True, "derived")
    if not host:
        return ProviderPlacement("remote-node", "", False, "derived")
    if _is_loopback(host):
        return ProviderPlacement("local", host, True, "derived")
    if _is_cloud_host(host):
        return ProviderPlacement("cloud", host, True, "derived")
    if getattr(provider, "requires_vpn", False) or _is_private_or_unqualified(host):
        return ProviderPlacement("remote-node", host, True, "derived")
    return ProviderPlacement("remote-node", host, False, "derived")
