# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Declarative provisioning — the path for a principal that is not a person.

Spec §3.3. The setup interview is the *human* path: it asks someone, in their own
words, how they want to be reached. A service account, an agent, or a node has no
one to ask, so it is declared instead: handle, kind, endpoints, topic routing.

The rule this module enforces is that the two paths stay separate. A human
principal cannot be provisioned declaratively, because a record assembled from
defaults would *look* interviewed without anyone having been asked, and
"configured therefore fine" is precisely the failure this extension exists to
prevent. The interview refusing to run headless and this refusing to run for a
human are the same rule seen from both sides.

A declared principal is born ``UNVERIFIED``, never ``ACTIVE``. Declaring an
endpoint is not evidence it delivers; only the round trip in ``verify.py`` is.
"""

from __future__ import annotations

from typing import Any

from axiom.vega.identity import parse_handle

from .models import (
    ChannelPreference,
    ContactEndpoint,
    PrincipalKind,
    PrincipalProfile,
    PrincipalStatus,
)

__all__ = ["ProvisionError", "provision"]


class ProvisionError(ValueError):
    """A declaration that cannot become a principal record, and why."""


def _kind(raw: Any) -> PrincipalKind:
    if isinstance(raw, PrincipalKind):
        return raw
    try:
        return PrincipalKind(str(raw))
    except ValueError:
        known = ", ".join(k.value for k in PrincipalKind)
        raise ProvisionError(f"unknown principal kind {raw!r}; expected one of {known}") from None


def provision(declaration: dict[str, Any]) -> PrincipalProfile:
    """Build a principal record from a declaration, with no prompting.

    Raises :class:`ProvisionError` rather than filling in a plausible default:
    the caller is a config file or an install script, and a silent default here
    becomes an unreachable principal discovered during an incident.
    """
    kind = _kind(declaration.get("kind", "service"))
    if kind.is_human:
        raise ProvisionError(
            "a human principal is created through the setup interview, not declared — "
            "run principal.setup so the person is actually asked"
        )

    handle = str(declaration.get("handle") or "").strip()
    try:
        parse_handle(handle)
    except ValueError as exc:
        # Reuse the identity layer's message rather than inventing a second
        # description of the same grammar.
        raise ProvisionError(f"invalid handle: {exc}") from None

    raw_endpoints = declaration.get("endpoints") or []
    if not raw_endpoints:
        raise ProvisionError(
            f"{handle} needs at least one endpoint — a principal with no way to be "
            "reached is a record, not a channel"
        )

    endpoints: list[ContactEndpoint] = []
    for entry in raw_endpoints:
        try:
            endpoints.append(
                ContactEndpoint(kind=str(entry["kind"]), address=str(entry["address"]))
            )
        except KeyError as exc:
            raise ProvisionError(f"endpoint entry missing {exc.args[0]!r}: {entry!r}") from None
        except ValueError as exc:
            raise ProvisionError(str(exc)) from None

    preferences = [
        ChannelPreference(
            topic_class=str(p.get("topic_class", "*")),
            ranked_kinds=[str(k) for k in (p.get("ranked_kinds") or [])],
            urgency_floor=int(p.get("urgency_floor", 5)),
        )
        for p in (declaration.get("preferences") or [])
    ]

    principal = PrincipalProfile(
        handle=handle,
        display_name=str(declaration.get("display_name") or handle),
        kind=kind,
        endpoints=endpoints,
        preferences=preferences,
        directory_ref=declaration.get("directory_ref"),
    )
    # Declared, not proven. The round trip is what moves this to ACTIVE.
    principal.status = PrincipalStatus.UNVERIFIED
    return principal
