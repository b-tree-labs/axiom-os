# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Durable home for the principal record.

P0 persists to ``<config-dir>/principal.toml``, following the same
operator-durable pattern the notifications extension uses for its own channel
config. This is deliberate for P0 and *not* a permanent answer: the record is
install-shaped configuration (who this harness works for), and it must be
readable and editable by an operator without a database.

Phase 2 introduces threads, which are transactional and belong in the
extension's own schema via ``axiom.infra.db.session_for`` per ADR-052. The
principal record may migrate there with them; verification state is the part
that will want transactions once more than one process writes it.

The file holds contact addresses and reachability, so it is written ``0600``.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from .models import (
    PrincipalKind,
    ChannelPreference,
    ContactEndpoint,
    EndpointHealth,
    PrincipalProfile,
    PrincipalStatus,
)

__all__ = ["FILENAME", "load", "save", "record_path"]

FILENAME = "principal.toml"


def _default_dir() -> Path:
    try:
        from axiom.infra.config import default_config_dir

        return Path(default_config_dir())
    except Exception:  # noqa: BLE001 — config primitive is optional at this layer
        return Path(os.path.expanduser("~/.axi/config"))


def record_path(config_dir: Path | str | None = None) -> Path:
    return Path(config_dir or _default_dir()) / FILENAME


def _quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(principal: PrincipalProfile, *, config_dir: Path | str | None = None) -> Path:
    """Write the record, owner-readable only. Returns the path written."""
    path = record_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Axiom principal record (DESK). Written by `axi principal setup`.",
        "#",
        "# `verified_at` is set only by a completed round trip and is what routing",
        "# consults. Hand-editing it to fake a verification defeats the one check",
        "# that distinguishes a configured channel from a delivering one.",
        "",
        "[principal]",
        f"handle = {_quote(principal.handle)}",
        f"display_name = {_quote(principal.display_name)}",
        f"kind = {_quote(principal.kind.value)}",
        f"status = {_quote(principal.status.value)}",
    ]
    if principal.directory_ref:
        lines.append(f"directory_ref = {_quote(principal.directory_ref)}")
    if principal.quiet_hours:
        lines.append(
            f"quiet_hours = [{_quote(principal.quiet_hours[0])}, {_quote(principal.quiet_hours[1])}]"
        )

    for ep in principal.endpoints:
        lines += [
            "",
            "[[endpoint]]",
            f"kind = {_quote(ep.kind)}",
            f"address = {_quote(ep.address)}",
            f"health = {_quote(ep.health.value)}",
        ]
        if ep.verified_at:
            lines.append(f"verified_at = {_quote(ep.verified_at)}")
        if ep.health_reason:
            lines.append(f"health_reason = {_quote(ep.health_reason)}")
        if ep.last_receipt_id:
            lines.append(f"last_receipt_id = {_quote(ep.last_receipt_id)}")

    for pref in principal.preferences:
        ranked = ", ".join(_quote(k) for k in pref.ranked_kinds)
        lines += [
            "",
            "[[preference]]",
            f"topic_class = {_quote(pref.topic_class)}",
            f"ranked_kinds = [{ranked}]",
            f"urgency_floor = {pref.urgency_floor}",
        ]

    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return path


def load(*, config_dir: Path | str | None = None) -> PrincipalProfile | None:
    """Read the record, or ``None`` when the harness has no principal yet.

    Absence is a normal state, not an error: a fresh install has no principal
    until someone runs setup, and the caller decides what to do about it.
    """
    path = record_path(config_dir)
    if not path.exists():
        return None

    data = tomllib.loads(path.read_text())
    block = data.get("principal") or {}

    endpoints = []
    for raw in data.get("endpoint") or []:
        ep = ContactEndpoint(
            kind=raw["kind"],
            address=raw["address"],
            verified_at=raw.get("verified_at"),
            health_reason=raw.get("health_reason"),
            last_receipt_id=raw.get("last_receipt_id"),
        )
        try:
            ep.health = EndpointHealth(raw.get("health", "unknown"))
        except ValueError:
            ep.health = EndpointHealth.UNKNOWN
        endpoints.append(ep)

    preferences = [
        ChannelPreference(
            topic_class=raw["topic_class"],
            ranked_kinds=list(raw.get("ranked_kinds") or []),
            urgency_floor=int(raw.get("urgency_floor", 5)),
        )
        for raw in data.get("preference") or []
    ]

    quiet = block.get("quiet_hours")
    quiet_hours = (quiet[0], quiet[1]) if isinstance(quiet, list) and len(quiet) == 2 else None

    try:
        kind = PrincipalKind(block.get("kind", "human"))
    except ValueError:
        # An unreadable kind must not silently become a *person*: that would
        # route a service account into the interview and the voice model. Fail
        # loudly rather than defaulting into the higher-consequence branch.
        raise ValueError(
            f"{path}: unknown principal kind {block.get('kind')!r}; "
            f"expected one of {', '.join(k.value for k in PrincipalKind)}"
        ) from None

    principal = PrincipalProfile(
        handle=block.get("handle", ""),
        directory_ref=block.get("directory_ref"),
        display_name=block.get("display_name", ""),
        kind=kind,
        endpoints=endpoints,
        preferences=preferences,
        quiet_hours=quiet_hours,
    )
    try:
        principal.status = PrincipalStatus(block.get("status", "pending"))
    except ValueError:
        principal.status = PrincipalStatus.PENDING
    return principal
