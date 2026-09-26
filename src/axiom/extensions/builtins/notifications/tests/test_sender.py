# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``SenderRegistry`` primitive (ADR-066 PR-1).

TDD-first per CLAUDE.md core invariants. Tests pin the contract:

- ``SenderIdentity`` is frozen and carries exactly the ADR-066 fields.
- ``SenderRegistry.from_manifest`` validates the ``[sender]`` block
  against ``sender.schema.json`` and builds a ``SenderIdentity``.
- A missing required ``display_name`` is a schema error, not a crash.
- ``render_for_channel`` produces the locale-correct possessive surface,
  and the possessive form exists ONLY in the render layer.
- Each channel receives a ``RenderedSender`` shaped to its vendor
  surface (Slack username/icon, email From, SMS body prefix, ...).
- Owner display derives deterministically from ``owner_handle`` when no
  federation-resolved name is supplied.
"""

from __future__ import annotations

import dataclasses

import pytest

from axiom.extensions.builtins.notifications.sender import (
    RenderedSender,
    SenderIdentity,
    SenderRegistry,
    render_for_channel,
)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _rivet_identity() -> SenderIdentity:
    return SenderIdentity(
        principal="@rivet:bens",
        display_name="RIVET",
        version="0.6.0",
        owner_handle="@ben.booth",
        avatar_uri="https://axiom.btreelabs.ai/avatars/rivet.png",
        from_address="rivet@axiom.btreelabs.ai",
    )


# --------------------------------------------------------------------------- #
# SenderIdentity contract
# --------------------------------------------------------------------------- #
def test_sender_identity_is_frozen():
    ident = _rivet_identity()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ident.display_name = "Mutated"  # type: ignore[misc]


def test_sender_identity_optional_fields_default_none():
    ident = SenderIdentity(
        principal="@tidy:bens",
        display_name="Tidy",
        version="1.0",
        owner_handle="@ben.booth",
    )
    assert ident.avatar_uri is None
    assert ident.from_address is None


def test_sender_identity_principal_keeps_adr020_form():
    # Version lives in metadata; it must never be embedded in the principal.
    ident = _rivet_identity()
    assert ident.principal == "@rivet:bens"
    assert ident.version not in ident.principal


# --------------------------------------------------------------------------- #
# from_manifest + schema validation
# --------------------------------------------------------------------------- #
def test_from_manifest_builds_identity():
    block = {
        "display_name": "RIVET",
        "avatar_uri": "https://axiom.btreelabs.ai/avatars/rivet.png",
        "from_address": "rivet@axiom.btreelabs.ai",
    }
    ident = SenderRegistry.identity_from_manifest(
        principal="@rivet:bens",
        version="0.6.0",
        owner_handle="@ben.booth",
        sender_block=block,
    )
    assert ident.display_name == "RIVET"
    assert ident.avatar_uri.endswith("rivet.png")
    assert ident.from_address == "rivet@axiom.btreelabs.ai"


def test_from_manifest_missing_display_name_is_schema_error():
    with pytest.raises(ValueError):
        SenderRegistry.identity_from_manifest(
            principal="@rivet:bens",
            version="0.6.0",
            owner_handle="@ben.booth",
            sender_block={"avatar_uri": "https://x/y.png"},
        )


def test_from_manifest_rejects_unknown_keys():
    with pytest.raises(ValueError):
        SenderRegistry.identity_from_manifest(
            principal="@rivet:bens",
            version="0.6.0",
            owner_handle="@ben.booth",
            sender_block={"display_name": "RIVET", "color": "blue"},
        )


# --------------------------------------------------------------------------- #
# Registry register / get
# --------------------------------------------------------------------------- #
def test_register_and_get():
    reg = SenderRegistry()
    ident = _rivet_identity()
    reg.register(ident)
    assert reg.get("@rivet:bens") is ident


def test_get_unknown_principal_raises():
    reg = SenderRegistry()
    with pytest.raises(KeyError):
        reg.get("@nobody:bens")


def test_double_register_rejected_without_replace():
    reg = SenderRegistry()
    reg.register(_rivet_identity())
    with pytest.raises(ValueError):
        reg.register(_rivet_identity())
    # replace=True succeeds
    reg.register(_rivet_identity(), replace=True)


# --------------------------------------------------------------------------- #
# Render layer — the ONLY place the possessive form exists
# --------------------------------------------------------------------------- #
def test_render_en_possessive():
    ident = _rivet_identity()
    rendered = render_for_channel(ident, "slack", locale="en", owner_display="Ben")
    assert rendered.display == "Ben's RIVET 0.6.0"


def test_render_owner_display_derives_from_handle_when_absent():
    ident = _rivet_identity()  # owner_handle "@ben.booth"
    rendered = render_for_channel(ident, "slack", locale="en")
    assert rendered.display == "Ben's RIVET 0.6.0"


def test_render_unknown_locale_falls_back_to_en():
    ident = _rivet_identity()
    rendered = render_for_channel(ident, "slack", locale="zz", owner_display="Ben")
    assert rendered.display == "Ben's RIVET 0.6.0"


def test_render_is_rendered_sender():
    rendered = render_for_channel(_rivet_identity(), "slack", owner_display="Ben")
    assert isinstance(rendered, RenderedSender)


# --------------------------------------------------------------------------- #
# Per-channel surface mapping (ADR-066 table)
# --------------------------------------------------------------------------- #
def test_slack_surface_username_and_icon():
    r = render_for_channel(_rivet_identity(), "slack", owner_display="Ben")
    assert r.username == "Ben's RIVET 0.6.0"
    assert r.icon_url == "https://axiom.btreelabs.ai/avatars/rivet.png"


def test_mattermost_surface_matches_slack_shape():
    r = render_for_channel(_rivet_identity(), "mattermost", owner_display="Ben")
    assert r.username == "Ben's RIVET 0.6.0"
    assert r.icon_url is not None


def test_sms_surface_body_prefix():
    r = render_for_channel(_rivet_identity(), "twilio_sms", owner_display="Ben")
    assert r.body_prefix == "[Ben's RIVET]"


def test_email_surface_from_address_used_when_set():
    r = render_for_channel(_rivet_identity(), "email", owner_display="Ben")
    assert r.from_address == "rivet@axiom.btreelabs.ai"
    assert r.display == "Ben's RIVET 0.6.0"


def test_email_surface_from_address_none_when_unset():
    ident = SenderIdentity(
        principal="@tidy:bens",
        display_name="Tidy",
        version="1.0",
        owner_handle="@ben.booth",
    )
    r = render_for_channel(ident, "email", owner_display="Ben")
    assert r.from_address is None


def test_inbox_surface_carries_principal():
    r = render_for_channel(_rivet_identity(), "inbox", owner_display="Ben")
    assert r.sender_principal == "@rivet:bens"
    assert r.display == "Ben's RIVET 0.6.0"


# --------------------------------------------------------------------------- #
# Manifest backfill smoke — every shipped agent extension has a valid block
# --------------------------------------------------------------------------- #
def test_all_agent_manifests_have_valid_sender_block():
    """Backfill guard: each kind=agent extension declares a schema-valid
    [sender] block. Lint is flipped to *required* in a later PR; this test
    pins the backfill so it cannot silently regress."""
    import tomllib
    from pathlib import Path

    builtins = (
        Path(__file__).resolve().parents[2]  # .../builtins
    )
    agent_manifests = []
    for toml_path in builtins.glob("*/axiom-extension.toml"):
        raw = toml_path.read_text(encoding="utf-8")
        data = tomllib.loads(raw)
        provides = data.get("extension", {}).get("provides", [])
        if any(p.get("kind") == "agent" for p in provides):
            agent_manifests.append((toml_path, data))

    assert agent_manifests, "expected at least one agent extension"
    for toml_path, data in agent_manifests:
        block = data.get("sender")
        assert block is not None, f"{toml_path} missing [sender] block"
        # Validates against the schema (raises ValueError on violation).
        SenderRegistry.identity_from_manifest(
            principal=f"@{toml_path.parent.name}:bens",
            version=data["extension"]["version"],
            owner_handle="@ben.booth",
            sender_block=block,
        )
