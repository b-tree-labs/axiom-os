# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Built-in comms connector descriptors (the connect extension owns these).

Vendor specifics live HERE, not in core (axiom.infra.connector_fabric stays
vendor-agnostic). `register_builtin_connectors` registers them into a fabric.
"""
from __future__ import annotations

from axiom.infra.connector_fabric import (
    ArtifactClass,
    Availability,
    ConnectorDescriptor,
    ConnectorFabric,
    EnvVar,
    SetupSpec,
    TrustTier,
    default_fabric,
)

_META = "ai.axiom.registry"


def slack_connector_descriptor() -> ConnectorDescriptor:
    """The Slack comms connector — a bidirectional channel adapter.

    Socket Mode (outbound websocket → no inbound URL, works behind a
    restricted network). Credentials resolve from the keystore via the
    ``slack`` connection; the descriptor only *declares* what's needed.
    """
    return ConnectorDescriptor(
        name="ai.axiom.connector.slack",
        version="0.1.0",
        title="Slack",
        description="Bidirectional Slack channel (Socket Mode): post, receive "
        "messages/mentions, and interactive approvals for HITL workflows.",
        artifact_class=ArtifactClass.CONNECTOR,
        kind="channel_adapter",
        transport="socket-mode",
        connection_ref="slack",
        provider_entry="axiom.extensions.builtins.notifications.channels.slack_interactive:make_slack_channel",
        env=[
            EnvVar("SLACK_BOT_TOKEN", "Bot User OAuth token (xoxb-)", is_required=True, is_secret=True,
                   where="OAuth & Permissions → Bot User OAuth Token (click Copy)",
                   url="https://api.slack.com/apps/{app_id}/oauth"),
            EnvVar("SLACK_APP_TOKEN", "App-Level token for Socket Mode (xapp-)", is_required=True, is_secret=True,
                   where="Basic Information → App-Level Tokens → Generate Token and Scopes → add scope connections:write → Generate",
                   url="https://api.slack.com/apps/{app_id}/general"),
            EnvVar("SLACK_CHANNEL", "Target channel id or name", is_required=True),
        ],
        availability=Availability.AVAILABLE,
        setup=SetupSpec(
            install_kind="app_manifest",
            summary="Create a Slack app from a generated manifest, install it, paste two tokens.",
            needs=("Bot token (xoxb-)", "App-level token (xapp-)", "Channel id"),
            urls={
                "Create app (from manifest)": "https://api.slack.com/apps?new_app=1",
                "App configuration tokens": "https://api.slack.com/apps",
                "Socket Mode docs": "https://api.slack.com/apis/socket-mode",
            },
            # Setup copy as data — change when Slack changes, no code edit.
            credential_url_label="App configuration tokens",
            credential_hint="xoxe-",
            instructions=(
                "One-time App Configuration Token enables near-zero-touch install (it creates the app for you).",
                'On the opened page, do NOT click "Create New App" / the popup — close it if it appears.',
                'Find the "Your App Configuration Tokens" panel → click "Generate Token".',
                "Copy the Access Token (starts with xoxe-). Ignore the Refresh Token.",
            ),
            prompt="Paste the Access Token (xoxe-…) here, or press Enter to create the app in your browser instead: ",
            error_remedies=(
                ("missing_scope", "The bot is missing a scope (e.g. channels:read for channel lookup). "
                 "Quick fix: re-run with the channel ID — `--channel C0…` skips the name lookup. "
                 "Permanent fix: OAuth & Permissions → add the scopes Slack listed → Reinstall to Workspace."),
                ("not_in_channel", "The bot isn't in that channel. In Slack: `/invite @<your app>` in the channel, then re-run."),
                ("channel_not_found", "Channel not found — check the name/id; for a private channel the bot must be /invited first."),
                ("invalid_auth", "Bot token rejected — re-copy the Bot User OAuth Token (xoxb-) from OAuth & Permissions."),
                ("not_authed", "Missing/!invalid token — re-copy the Bot User OAuth Token (xoxb-)."),
            ),
            # In-place update copy (data) for `axi connector upgrade`.
            app_id_hint="Find the app id (A0…) in the app's console URL: api.slack.com/apps/<APP_ID>/…",
            update_prompt="Paste the App Configuration Access Token (xoxe-…) to update the app in place: ",
            reconsent_url="https://api.slack.com/apps/{app_id}/install-on-team",
            reconsent_note=(
                "Your xoxb-/xapp- tokens stay valid — no re-paste, the keystore secret_ref is "
                "unchanged. New scopes just need one workspace re-consent."
            ),
        ),
        meta={
            f"{_META}/trust_tier": TrustTier.FIRST_PARTY.value,
            f"{_META}/direction": "bidirectional",
            f"{_META}/classification": "internal",
            # external egress → flagged for stricter consent/policy (Phase 3).
            f"{_META}/egress": "external",
        },
    )
def teams_connector_descriptor() -> ConnectorDescriptor:
    """Microsoft Teams comms connector — PLANNED (catalogued with deep links
    so users see what's coming; provider lands next, runs the same workflow)."""
    return ConnectorDescriptor(
        name="ai.axiom.connector.teams",
        version="0.0.0",
        title="Microsoft Teams",
        description="Bidirectional Teams channel (bot + Graph): post, receive, and "
        "interactive approvals for HITL workflows. Planned.",
        artifact_class=ArtifactClass.CONNECTOR,
        kind="channel_adapter",
        connection_ref="teams",
        availability=Availability.PLANNED,
        env=[
            EnvVar("TEAMS_APP_ID", "Entra app (client) id", is_required=True),
            EnvVar("TEAMS_APP_PASSWORD", "Entra client secret", is_required=True, is_secret=True),
        ],
        setup=SetupSpec(
            install_kind="app_registration",
            summary="Register an Entra app + Teams app manifest, grant Graph permissions, install to a team.",
            needs=("Entra app id", "Entra client secret", "Team/channel"),
            urls={
                "Azure: register an app (Entra)": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
                "Teams Developer Portal": "https://dev.teams.microsoft.com/apps",
                "Graph permissions reference": "https://learn.microsoft.com/graph/permissions-reference",
            },
        ),
        meta={
            f"{_META}/trust_tier": TrustTier.FIRST_PARTY.value,
            f"{_META}/direction": "bidirectional",
            f"{_META}/vendor": "microsoft",
        },
    )


def sms_connector_descriptor() -> ConnectorDescriptor:
    """SMS comms connector (Twilio) — PLANNED. Reuses the twilio_sms channel."""
    return ConnectorDescriptor(
        name="ai.axiom.connector.sms",
        version="0.1.0",
        title="SMS (Twilio)",
        description="Bidirectional SMS: alerts, approve/deny (YES/NO), and DT "
        "verification/control replies (a number = measured value) from a phone.",
        artifact_class=ArtifactClass.CONNECTOR,
        kind="channel_adapter",
        transport="webhook",
        connection_ref="twilio",
        provider_entry="axiom.extensions.builtins.notifications.channels.twilio_interactive:make_twilio_channel",
        availability=Availability.AVAILABLE,
        env=[
            EnvVar("TWILIO_ACCOUNT_SID", "Twilio Account SID", is_required=True),
            EnvVar("TWILIO_AUTH_TOKEN", "Twilio auth token", is_required=True, is_secret=True),
            EnvVar("TWILIO_FROM", "Sending phone number (+1…)", is_required=True),
            EnvVar("TWILIO_TO", "Owner's phone number (+1…) the agent texts", is_required=True),
        ],
        setup=SetupSpec(
            install_kind="developer_token",
            summary="Create a Twilio messaging service, copy SID/token, set a sending number.",
            needs=("Account SID", "Auth token", "From number"),
            urls={
                "Twilio Console": "https://console.twilio.com/",
                "Messaging quickstart": "https://www.twilio.com/docs/messaging/quickstart",
            },
        ),
        meta={
            f"{_META}/trust_tier": TrustTier.FIRST_PARTY.value,
            f"{_META}/direction": "bidirectional",
            f"{_META}/vendor": "twilio",
            # SMS approvals are low-fidelity (YES/NO) — no rich buttons/threads.
            f"{_META}/interactivity": "text-reply",
        },
    )


def imessage_connector_descriptor() -> ConnectorDescriptor:
    """iMessage comms connector — PLANNED. Local, zero-credential channel when
    the user has a Mac endpoint (Messages.app via AppleScript / the local
    chat.db). Preferred over SMS when a Mac is available: no cloud, no tokens,
    free, end-to-end-encrypted, and richer than SMS."""
    return ConnectorDescriptor(
        name="ai.axiom.connector.imessage",
        version="0.0.0",
        title="iMessage",
        description="Local Mac iMessage channel (Messages.app). Zero-credential when a "
        "Mac endpoint is present; preferred over SMS there. Planned.",
        artifact_class=ArtifactClass.CONNECTOR,
        kind="channel_adapter",
        connection_ref="imessage",
        availability=Availability.PLANNED,
        env=[],  # local — no secrets
        setup=SetupSpec(
            install_kind="local_mac",
            summary="No cloud setup: uses the local Messages.app (AppleScript send + chat.db read). "
            "Requires a signed-in Mac endpoint + Full Disk Access for the agent process.",
            needs=("A signed-in Mac with Messages.app", "Full Disk Access (to read chat.db)"),
            urls={
                "Full Disk Access (System Settings)": "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
            },
        ),
        meta={
            f"{_META}/trust_tier": TrustTier.FIRST_PARTY.value,
            f"{_META}/direction": "bidirectional",
            f"{_META}/vendor": "apple",
            f"{_META}/requires_mac_endpoint": "true",
            f"{_META}/zero_credential": "true",
            # When a Mac endpoint is detected, prefer iMessage over SMS.
            f"{_META}/prefer_over": "ai.axiom.connector.sms",
            f"{_META}/interactivity": "text-reply",
        },
    )


def email_connector_descriptor() -> ConnectorDescriptor:
    """Email comms connector — bidirectional via reply ingest (B4). Outbound
    through the nested email provider factory (SMTP/Resend/…); inbound replies
    arrive through the shared webhook/IMAP receiver."""
    return ConnectorDescriptor(
        name="ai.axiom.connector.email",
        version="0.1.0",
        title="Email",
        description="Bidirectional email: alerts + approve/deny (reply YES/NO) and "
        "DT verification (reply a number = measured value), threaded via In-Reply-To.",
        artifact_class=ArtifactClass.CONNECTOR,
        kind="channel_adapter",
        transport="webhook",
        connection_ref="email",
        provider_entry="axiom.extensions.builtins.notifications.channels.email.interactive:make_email_channel",
        availability=Availability.AVAILABLE,
        env=[
            EnvVar("EMAIL_TO", "Owner's email address the agent writes to", is_required=True),
            EnvVar("EMAIL_FROM", "Sending address (the agent's From)", is_required=True),
            EnvVar("SMTP_HOST", "SMTP host (or use a provider key below)"),
            EnvVar("SMTP_PASSWORD", "SMTP password", is_secret=True),
            EnvVar("RESEND_API_KEY", "Resend API key (alternative to SMTP)", is_secret=True),
        ],
        setup=SetupSpec(
            install_kind="developer_token",
            summary="Configure an email provider (SMTP or Resend) + a From/To; "
            "point the provider's inbound webhook at the agent for replies.",
            needs=("From address", "To address", "An email provider (SMTP or Resend)"),
            urls={"Resend": "https://resend.com/", "SMTP docs": "https://www.rfc-editor.org/rfc/rfc5321"},
        ),
        meta={
            f"{_META}/trust_tier": TrustTier.FIRST_PARTY.value,
            f"{_META}/direction": "bidirectional",
            f"{_META}/vendor": "email",
            f"{_META}/interactivity": "text-reply",
            f"{_META}/egress": "external",
        },
    )


# ---------------------------------------------------------------------------
# Connector enabled/disabled state — users switch connectors on and off
# ---------------------------------------------------------------------------



def register_builtin_connectors(fabric: ConnectorFabric | None = None) -> ConnectorFabric:
    """Register the connect extension's comms connectors into ``fabric``
    (default: the platform default fabric). Idempotent."""
    fab = fabric or default_fabric()
    for fn in (slack_connector_descriptor, teams_connector_descriptor,
               sms_connector_descriptor, imessage_connector_descriptor,
               email_connector_descriptor):
        fab.register(fn(), replace=True)
    return fab


__all__ = [
    "slack_connector_descriptor",
    "teams_connector_descriptor",
    "sms_connector_descriptor",
    "imessage_connector_descriptor",
    "email_connector_descriptor",
    "register_builtin_connectors",
]
