# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Channel rehydration — turn resolved config/secrets into live channels.

``SendContext.default()`` ships inbox-only. This module rehydrates the
EXTERNAL channels (Slack / Teams / Mattermost / Twilio / AWS SNS / Azure
ACS / email vendors / FCM) into a ``SendContext`` from a config source,
so ``axi notifications send`` can actually dispatch to them.

The concrete config source here is **environment variables** — the
value the operator hand-applies on the server (or that a future
secrets-backed resolver populates). This keeps the credential material
out of the codebase and out of git while giving ``send()`` a config dict
per channel to thread into ``provider.build(cfg)``.

FAIL-CLOSED CONTRACT (compliance-load-bearing):

- A channel is registered + configured ONLY when ALL of its required
  variables are present and non-empty. A partial or malformed config
  leaves the channel UNREGISTERED, so a send falls back to the inbox
  channel rather than erroring or, worse, half-sending.
- This function NEVER raises. A bad value for one channel is logged and
  skipped; the other channels (and inbox) are unaffected.
- Nothing here changes the classification ceiling. Every external channel
  keeps its ``INTERNAL`` ceiling from its capabilities(); the ceiling
  filter in ``send.py`` (``registry.admitted_for``) is the sole gate that
  keeps ``regulated`` / ``controlled`` (EC-controlled / ITAR) envelopes
  off these channels.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axiom.extensions.builtins.notifications.send import SendContext

_log = logging.getLogger(__name__)

_PREFIX = "AXIOM_HERALD_"

# Installed-config layer (ADR-016 layering: env > installed config). The
# operator-durable home is ``<config-dir>/herald.toml`` — written by
# ``axi notifications setup <channel>`` — so channel config survives the
# shell session it was pasted into. Env vars still override per-key, which
# keeps systemd EnvironmentFile / CI overrides working unchanged.
_CONFIG_FILENAME = "herald.toml"
_CONFIG_SECTION = "herald"


def _config_path():
    from pathlib import Path

    try:
        from axiom.infra.config import default_config_dir

        return Path(default_config_dir()) / _CONFIG_FILENAME
    except Exception:  # pragma: no cover - infra always present in-repo
        from pathlib import Path as _P

        return _P.home() / ".axi" / "config" / _CONFIG_FILENAME


def _load_installed() -> dict[str, str]:
    """Read herald.toml into PREFIXed keys. Never raises; {} on any problem."""
    import tomllib

    path = _config_path()
    try:
        with path.open("rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = doc.get(_CONFIG_SECTION)
    if not isinstance(section, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in section.items():
        if isinstance(value, list):
            value = ",".join(str(v) for v in value)
        out[_PREFIX + str(key).upper()] = str(value)
    return out


def resolved_config(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The layered channel config: installed herald.toml under env overrides.

    ``env=None`` means the real process environment. Passing an explicit
    ``env`` STILL layers over the installed config — callers that need a
    hermetic view (tests) monkeypatch ``_load_installed``.
    """
    merged = _load_installed()
    source = os.environ if env is None else env
    merged.update(
        {k: v for k, v in source.items() if k.startswith(_PREFIX) and v is not None}
    )
    return merged


def _get(env: Mapping[str, str], suffix: str) -> str:
    return (env.get(_PREFIX + suffix) or "").strip()


def _all(env: Mapping[str, str], *suffixes: str) -> bool:
    return all(_get(env, s) for s in suffixes)


def _resolve_email_backend(env: Mapping[str, str]) -> dict[str, Any] | None:
    """Return the backend-specific keys for the email channel, or None.

    Exactly one backend is selected, most-specific first. Returns None
    when no backend is fully configured (→ email channel not enabled).
    """
    if _get(env, "EMAIL_SES_REGION"):
        cfg: dict[str, Any] = {
            # explicit override so the IAM-role path (no keys) still selects SES
            "provider": "ses",
            "region": _get(env, "EMAIL_SES_REGION"),
        }
        if _all(env, "EMAIL_SES_ACCESS_KEY_ID", "EMAIL_SES_SECRET_ACCESS_KEY"):
            cfg["aws_access_key_id"] = _get(env, "EMAIL_SES_ACCESS_KEY_ID")
            cfg["aws_secret_access_key"] = _get(env, "EMAIL_SES_SECRET_ACCESS_KEY")
        return cfg
    if _get(env, "EMAIL_RESEND_API_KEY"):
        return {"resend_api_key": _get(env, "EMAIL_RESEND_API_KEY")}
    if _get(env, "EMAIL_ACS_CONNECTION_STRING"):
        return {"acs_connection_string": _get(env, "EMAIL_ACS_CONNECTION_STRING")}
    if _all(env, "EMAIL_ACS_ENDPOINT", "EMAIL_ACS_ACCESS_TOKEN"):
        return {
            "provider": "acs",
            "acs_endpoint": _get(env, "EMAIL_ACS_ENDPOINT"),
            "acs_access_token": _get(env, "EMAIL_ACS_ACCESS_TOKEN"),
        }
    if _get(env, "EMAIL_GMAIL_ACCESS_TOKEN"):
        return {"gmail_access_token": _get(env, "EMAIL_GMAIL_ACCESS_TOKEN")}
    if _all(
        env,
        "EMAIL_GMAIL_REFRESH_TOKEN",
        "EMAIL_GMAIL_CLIENT_ID",
        "EMAIL_GMAIL_CLIENT_SECRET",
    ):
        return {
            "gmail_refresh_token": _get(env, "EMAIL_GMAIL_REFRESH_TOKEN"),
            "gmail_client_id": _get(env, "EMAIL_GMAIL_CLIENT_ID"),
            "gmail_client_secret": _get(env, "EMAIL_GMAIL_CLIENT_SECRET"),
        }
    if _get(env, "EMAIL_SMTP_HOST"):
        cfg = {"smtp_host": _get(env, "EMAIL_SMTP_HOST")}
        if _get(env, "EMAIL_SMTP_PORT"):
            cfg["smtp_port"] = _get(env, "EMAIL_SMTP_PORT")
        if _get(env, "EMAIL_SMTP_USER"):
            cfg["smtp_user"] = _get(env, "EMAIL_SMTP_USER")
        if _get(env, "EMAIL_SMTP_PASSWORD"):
            cfg["smtp_password"] = _get(env, "EMAIL_SMTP_PASSWORD")
        return cfg
    return None


def _channel_specs(
    env: Mapping[str, str],
) -> list[tuple[str, Callable[[], Any], dict[str, Any]]]:
    """Return ``(name, provider_factory, config)`` for each configured channel.

    Only fully-configured channels appear. Provider factories are thunks
    that lazily import the adapter module (so an install without the
    optional cloud SDKs still imports this module).
    """
    specs: list[tuple[str, Callable[[], Any], dict[str, Any]]] = []

    if _get(env, "SLACK_WEBHOOK_URL"):
        from axiom.extensions.builtins.notifications.channels.slack import (
            SlackChannelAdapterProvider,
        )

        specs.append(
            (
                "slack",
                SlackChannelAdapterProvider,
                {"webhook_url": _get(env, "SLACK_WEBHOOK_URL")},
            )
        )

    if _get(env, "TEAMS_WEBHOOK_URL"):
        from axiom.extensions.builtins.notifications.channels.teams import (
            TeamsChannelAdapterProvider,
        )

        specs.append(
            (
                "teams",
                TeamsChannelAdapterProvider,
                {
                    "webhook_url": _get(env, "TEAMS_WEBHOOK_URL"),
                    "mention_upns": _get(env, "TEAMS_MENTION_UPNS"),
                },
            )
        )

    if _get(env, "MATTERMOST_WEBHOOK_URL"):
        from axiom.extensions.builtins.notifications.channels.mattermost import (
            MattermostChannelAdapterProvider,
        )

        specs.append(
            (
                "mattermost",
                MattermostChannelAdapterProvider,
                {"webhook_url": _get(env, "MATTERMOST_WEBHOOK_URL")},
            )
        )

    if _all(env, "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        from axiom.extensions.builtins.notifications.channels.twilio_sms import (
            TwilioSmsChannelAdapterProvider,
        )

        specs.append(
            (
                "twilio-sms",
                TwilioSmsChannelAdapterProvider,
                {
                    "account_sid": _get(env, "TWILIO_ACCOUNT_SID"),
                    "auth_token": _get(env, "TWILIO_AUTH_TOKEN"),
                    "from_number": _get(env, "TWILIO_FROM_NUMBER"),
                },
            )
        )

    if _get(env, "SNS_REGION"):
        from axiom.extensions.builtins.notifications.channels.aws_sns_sms import (
            AwsSnsSmsChannelAdapterProvider,
        )

        cfg: dict[str, Any] = {"region": _get(env, "SNS_REGION")}
        if _all(env, "SNS_AWS_ACCESS_KEY_ID", "SNS_AWS_SECRET_ACCESS_KEY"):
            cfg["aws_access_key_id"] = _get(env, "SNS_AWS_ACCESS_KEY_ID")
            cfg["aws_secret_access_key"] = _get(env, "SNS_AWS_SECRET_ACCESS_KEY")
        if _get(env, "SNS_SENDER_ID"):
            cfg["sender_id"] = _get(env, "SNS_SENDER_ID")
        specs.append(("aws-sns-sms", AwsSnsSmsChannelAdapterProvider, cfg))

    acs_cs = _get(env, "ACS_SMS_CONNECTION_STRING")
    acs_ep = _get(env, "ACS_SMS_ENDPOINT")
    acs_tok = _get(env, "ACS_SMS_ACCESS_TOKEN")
    acs_from = _get(env, "ACS_SMS_FROM_NUMBER")
    if acs_from and (acs_cs or (acs_ep and acs_tok)):
        from axiom.extensions.builtins.notifications.channels.acs_sms import (
            AcsSmsChannelAdapterProvider,
        )

        cfg = {"from_number": acs_from}
        if acs_cs:
            cfg["connection_string"] = acs_cs
        else:
            cfg["endpoint"] = acs_ep
            cfg["access_token"] = acs_tok
        specs.append(("acs-sms", AcsSmsChannelAdapterProvider, cfg))

    if _all(env, "FCM_PROJECT_ID", "FCM_ACCESS_TOKEN"):
        from axiom.extensions.builtins.notifications.channels.fcm_push import (
            FcmPushChannelAdapterProvider,
        )

        specs.append(
            (
                "fcm-push",
                FcmPushChannelAdapterProvider,
                {
                    "project_id": _get(env, "FCM_PROJECT_ID"),
                    "access_token": _get(env, "FCM_ACCESS_TOKEN"),
                },
            )
        )

    from_address = _get(env, "EMAIL_FROM_ADDRESS")
    if from_address:
        backend = _resolve_email_backend(env)
        if backend is not None:
            from axiom.extensions.builtins.notifications.channels.email import (
                EmailChannelAdapterProvider,
            )

            cfg = {"from_address": from_address, **backend}
            if _get(env, "EMAIL_FROM_NAME"):
                cfg["from_name"] = _get(env, "EMAIL_FROM_NAME")
            specs.append(("email", EmailChannelAdapterProvider, cfg))

    return specs


def rehydrate_from_env(
    ctx: SendContext, env: Mapping[str, str] | None = None
) -> list[str]:
    """Register + configure external channels on ``ctx`` from env vars.

    Mutates ``ctx.registry`` (registers each fully-configured provider,
    ``replace=True``) and ``ctx.channel_configs`` (stores the dispatch
    config, incl. secrets, that ``send()`` threads into ``build(cfg)``).

    Returns the list of channel names enabled. Never raises.

    Config resolves in layers (ADR-016): the installed ``herald.toml``
    provides the durable base; ``AXIOM_HERALD_*`` env vars override per-key.
    """
    resolved = resolved_config(env)
    enabled: list[str] = []
    try:
        specs = _channel_specs(resolved)
    except Exception:  # noqa: BLE001 — never let rehydration break send()
        _log.exception("channel rehydration failed to build specs; inbox-only")
        return enabled

    for name, factory, cfg in specs:
        try:
            provider = factory()
            ctx.registry.register(provider, replace=True)
            ctx.channel_configs[name] = cfg
            enabled.append(name)
        except Exception:  # noqa: BLE001 — fail closed: skip this channel
            _log.warning(
                "channel %r failed to rehydrate; falling back to inbox", name
            )
    return enabled


__all__ = ["rehydrate_from_env"]
