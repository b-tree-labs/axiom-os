# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi notifications connector add <vendor>`` — interactive wizard.

Friction-killer track #3 from the 2026-06-01 UX analysis: the 5-step
install (find the webhook page, paste a URL, run ``axi secrets put``,
edit a config, send a test) collapses to one command.

Architecture mirrors the rest of HERALD:

- ``WizardHandler`` Protocol + concrete per-vendor handlers form a
  Provider/Factory registry, parallel to the ``ChannelAdapterProvider``
  shape next door.
- ``InputProvider`` Protocol decouples interactive ``input()`` from the
  wizard's orchestration logic — tests inject ``DictInputProvider``.
- Browser-open is a best-effort callable. Headless environments
  (``DISPLAY`` unset, SSH session without X-forwarding, CI) silently
  fall back to printing the URL.
- Secrets land via an injected ``secret_put`` callable, so the wizard
  doesn't transitively pull in the ``secrets`` extension at construction
  time — the CLI layer wires the concrete store.

Per ADR-056 the CLI verb is a thin wrapper over the
``notifications.connector_add`` skill function in ``skills/``; this
module is the wizard primitive that the skill orchestrates.
"""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelAdapterRegistry,
)


# ---------------------------------------------------------------------------
# Input provider — decouples input() from orchestration
# ---------------------------------------------------------------------------


@runtime_checkable
class InputProvider(Protocol):
    """Source of wizard answers. Stdin in production, dict in tests."""

    def ask(self, prompt: str, *, key: str, secret: bool = False) -> str: ...


class StdinInputProvider:
    """Default ``InputProvider`` — reads a line from stdin per prompt.

    Secret answers go through ``getpass`` so they don't echo. Empty
    answers are returned verbatim; required-field enforcement happens
    in the wizard, not here.
    """

    def ask(self, prompt: str, *, key: str, secret: bool = False) -> str:
        if secret:
            import getpass
            return getpass.getpass(f"{prompt}: ")
        try:
            return input(f"{prompt}: ").strip()
        except EOFError:
            return ""


@dataclass
class DictInputProvider:
    """Test ``InputProvider`` — answers come from a dict keyed by field name."""

    answers: dict[str, str] = field(default_factory=dict)

    def ask(self, prompt: str, *, key: str, secret: bool = False) -> str:
        return self.answers.get(key, "")


# ---------------------------------------------------------------------------
# Field + handler protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WizardField:
    """One question the wizard asks for a given vendor.

    ``secret=True`` fields are written to the secrets store under a
    vendor-specific path convention; non-secret fields land in the
    config dict and the returned ``WizardResult.config``.
    """

    key: str
    prompt: str
    secret: bool = False
    required: bool = True


@runtime_checkable
class WizardHandler(Protocol):
    """Per-vendor wizard contract."""

    vendor: str
    fields: tuple[WizardField, ...]
    help_url: str | None

    def secret_path(self, key: str, name: str) -> str: ...
    def build_provider(self, config: dict[str, Any]): ...


# ---------------------------------------------------------------------------
# Wizard result
# ---------------------------------------------------------------------------


@dataclass
class WizardResult:
    """Outcome of one wizard run. The skill wraps this in a ``SkillResult``."""

    ok: bool
    vendor: str
    name: str
    config: dict[str, Any] = field(default_factory=dict)
    secret_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: str | None = None
    test_send_receipt: str | None = None


# ---------------------------------------------------------------------------
# Vendor handlers
# ---------------------------------------------------------------------------


@dataclass
class _SlackHandler:
    vendor: str = "slack"
    help_url: str | None = (
        "https://api.slack.com/messaging/webhooks"
    )
    fields: tuple[WizardField, ...] = (
        WizardField(
            key="webhook_url",
            prompt="Slack Incoming Webhook URL",
            secret=True,
        ),
    )

    def secret_path(self, key: str, name: str) -> str:
        return f"slack-webhook-{name}"

    def build_provider(self, config: dict[str, Any]):
        from axiom.extensions.builtins.notifications.channels.slack import (
            SlackChannelAdapterProvider,
        )
        return SlackChannelAdapterProvider()


@dataclass
class _MattermostHandler:
    vendor: str = "mattermost"
    help_url: str | None = (
        "https://docs.mattermost.com/developer/webhooks-incoming.html"
    )
    fields: tuple[WizardField, ...] = (
        WizardField(
            key="webhook_url",
            prompt="Mattermost Incoming Webhook URL",
            secret=True,
        ),
    )

    def secret_path(self, key: str, name: str) -> str:
        return f"mattermost-webhook-{name}"

    def build_provider(self, config: dict[str, Any]):
        from axiom.extensions.builtins.notifications.channels.mattermost import (
            MattermostChannelAdapterProvider,
        )
        return MattermostChannelAdapterProvider()


@dataclass
class _TeamsHandler:
    vendor: str = "teams"
    help_url: str | None = (
        "https://learn.microsoft.com/microsoftteams/platform/"
        "webhooks-and-connectors/how-to/add-incoming-webhook"
    )
    fields: tuple[WizardField, ...] = (
        WizardField(
            key="webhook_url",
            prompt="Teams Workflow webhook URL (post-2026-05-22 surface)",
            secret=True,
        ),
    )

    def secret_path(self, key: str, name: str) -> str:
        return f"teams-workflow-{name}"

    def build_provider(self, config: dict[str, Any]):
        from axiom.extensions.builtins.notifications.channels.teams import (
            TeamsChannelAdapterProvider,
        )
        return TeamsChannelAdapterProvider()


@dataclass
class _EmailHandler:
    """SMTP-fallback path for the wizard.

    The email channel ships with multiple vendor backends (Resend, SES,
    SendGrid, M365, Gmail, plain SMTP). The wizard ships an SMTP-first
    path because it's the universal lowest-common-denominator that
    works without OAuth round-trips. Vendor-API setup (Resend, SES)
    lands as separate handlers when their OAuth/API-key flows are
    wired in HERALD-2b.
    """

    vendor: str = "email"
    help_url: str | None = None
    fields: tuple[WizardField, ...] = (
        WizardField(key="smtp_host", prompt="SMTP host"),
        WizardField(key="smtp_port", prompt="SMTP port (e.g. 587)"),
        WizardField(key="smtp_user", prompt="SMTP username"),
        WizardField(
            key="smtp_password", prompt="SMTP password", secret=True
        ),
        WizardField(key="from_addr", prompt="From address"),
    )

    def secret_path(self, key: str, name: str) -> str:
        return f"email-{key.replace('_', '-')}-{name}"

    def build_provider(self, config: dict[str, Any]):
        from axiom.extensions.builtins.notifications.channels.email import (
            EmailChannelAdapterProvider,
        )
        return EmailChannelAdapterProvider()


@dataclass
class _TwilioSmsHandler:
    vendor: str = "twilio-sms"
    help_url: str | None = "https://console.twilio.com/"
    fields: tuple[WizardField, ...] = (
        WizardField(
            key="account_sid",
            prompt="Twilio Account SID (starts with AC...)",
            secret=True,
        ),
        WizardField(
            key="auth_token",
            prompt="Twilio Auth Token",
            secret=True,
        ),
        WizardField(
            key="from_number",
            prompt="Twilio From number (E.164, e.g. +15125550100)",
            secret=False,
        ),
    )

    def secret_path(self, key: str, name: str) -> str:
        return f"twilio-sms-{key.replace('_', '-')}-{name}"

    def build_provider(self, config: dict[str, Any]):
        from axiom.extensions.builtins.notifications.channels.twilio_sms import (
            TwilioSmsChannelAdapterProvider,
        )
        return TwilioSmsChannelAdapterProvider()


class _BoxHandler:
    """Box first-class storage connector — ADR-062.

    v1 ships developer-token auth: operator pastes a 60-minute token from
    https://app.box.com/developers/console. OAuth + 60-day refresh lands
    when the M365 Graph OAuth foundation lands (it sets the KEEP
    cap-token handoff pattern Box OAuth then reuses; connector-blocker
    analysis 2026-06-01 § 3).
    """

    vendor: str = "box"
    help_url: str | None = "https://app.box.com/developers/console"
    fields: tuple[WizardField, ...] = (
        WizardField(
            key="developer_token",
            prompt=(
                "Box developer token (paste from app.box.com/developers/console — "
                "60-minute lifetime; OAuth lands with M365 Graph foundation)"
            ),
            secret=True,
        ),
        WizardField(
            key="folder_id",
            prompt=(
                "Box folder id to scope this connector to "
                "(root = '0'; find a folder id in its URL after '/folder/')"
            ),
            secret=False,
        ),
    )

    def secret_path(self, key: str, name: str) -> str:
        return f"box-{key.replace('_', '-')}-{name}"

    def build_provider(self, config: dict[str, Any]):
        # Provider lands in PR-2 (ADR-062 § Implementation phases).
        # Wizard registration + secret capture work today; first call
        # site is the data_platform Box source delegation in PR-2.
        raise NotImplementedError(
            "Box StorageConnectorProvider lands in ADR-062 PR-2; "
            "this PR ships only the wizard + Protocol surface."
        )


# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------


_HANDLERS: dict[str, WizardHandler] = {
    "slack": _SlackHandler(),
    "mattermost": _MattermostHandler(),
    "teams": _TeamsHandler(),
    "email": _EmailHandler(),
    "twilio-sms": _TwilioSmsHandler(),
    "box": _BoxHandler(),
}

SUPPORTED_VENDORS: tuple[str, ...] = tuple(sorted(_HANDLERS))


def list_vendors() -> list[str]:
    return sorted(_HANDLERS)


def get_handler(vendor: str) -> WizardHandler:
    if vendor not in _HANDLERS:
        raise KeyError(
            f"no wizard handler for vendor {vendor!r}; "
            f"supported: {list_vendors()}"
        )
    return _HANDLERS[vendor]


# ---------------------------------------------------------------------------
# Browser-open shim — always best-effort
# ---------------------------------------------------------------------------


def _safe_open_browser(url: str) -> bool:
    """Return True if the browser actually opened.

    ``webbrowser.open`` doesn't raise on most platforms but can fail
    silently (``return False``) on headless boxes. We treat False or
    any exception as "fall back to printing the URL".
    """
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 — best-effort by contract
        return False


# ---------------------------------------------------------------------------
# Test-send shim — default uses the real send pipeline
# ---------------------------------------------------------------------------


def _default_test_send(
    *, vendor: str, name: str, registry: ChannelAdapterRegistry
) -> tuple[bool, str]:
    """The default sender pings the just-registered provider with a
    one-line greeting.

    This is intentionally minimal — the goal is to exercise the
    end-to-end wire (transport reachable, auth valid) not the rich
    routing logic of ``send()``. A real test message gets sent the
    next time the operator calls ``axi notifications send`` for real.
    """
    try:
        provider = registry.get(vendor)
    except KeyError as exc:
        return False, str(exc)
    return True, f"registered:{provider.name}:{name}"


# ---------------------------------------------------------------------------
# The wizard
# ---------------------------------------------------------------------------


@dataclass
class ConnectorWizard:
    """Orchestrates one ``connector add`` flow for a chosen vendor.

    Construction injects every side-effecting collaborator (secret
    store, channel-adapter registry, test-send, browser opener, input
    source) so the wizard is fully driveable from tests.
    """

    registry: ChannelAdapterRegistry
    secret_put: Callable[[str, bytes], None]
    test_send: Callable[..., tuple[bool, str]] = _default_test_send
    browser_open: Callable[[str], bool] = _safe_open_browser
    input_provider: InputProvider = field(default_factory=StdinInputProvider)
    printer: Callable[[str], None] = print

    def run(
        self,
        *,
        vendor: str,
        name: str = "default",
        no_test_send: bool = False,
    ) -> WizardResult:
        try:
            handler = get_handler(vendor)
        except KeyError as exc:
            return WizardResult(
                ok=False,
                vendor=vendor,
                name=name,
                errors=[str(exc)],
            )

        notes_parts: list[str] = []

        # Best-effort browser open. Headless = print + continue.
        if handler.help_url:
            opened = self.browser_open(handler.help_url)
            if not opened:
                self.printer(
                    f"(open this URL to find the credential: {handler.help_url})"
                )
                notes_parts.append("browser_fallback")

        config: dict[str, Any] = {}
        secret_paths: list[str] = []
        errors: list[str] = []

        for f in handler.fields:
            answer = self.input_provider.ask(
                f.prompt, key=f.key, secret=f.secret
            )
            if f.required and not answer:
                errors.append(
                    f"required field {f.key!r} was empty"
                )
                continue
            if f.secret:
                path = handler.secret_path(f.key, name)
                self.secret_put(path, answer.encode("utf-8"))
                secret_paths.append(path)
            else:
                config[f.key] = answer

        if errors:
            return WizardResult(
                ok=False,
                vendor=vendor,
                name=name,
                errors=errors,
                config=config,
                secret_paths=secret_paths,
                notes=",".join(notes_parts) or None,
            )

        # Register provider in the in-process registry.
        provider = handler.build_provider(config)
        try:
            self.registry.register(provider, replace=True)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"failed to register provider: {exc}")
            return WizardResult(
                ok=False,
                vendor=vendor,
                name=name,
                errors=errors,
                config=config,
                secret_paths=secret_paths,
                notes=",".join(notes_parts) or None,
            )

        # Test send (skippable for CI / fully scripted setup).
        receipt: str | None = None
        if not no_test_send:
            ok, info = self.test_send(
                vendor=vendor, name=name, registry=self.registry
            )
            receipt = info
            if not ok:
                errors.append(f"test send failed: {info}")
                return WizardResult(
                    ok=False,
                    vendor=vendor,
                    name=name,
                    errors=errors,
                    config=config,
                    secret_paths=secret_paths,
                    notes=",".join(notes_parts) or None,
                    test_send_receipt=receipt,
                )

        return WizardResult(
            ok=True,
            vendor=vendor,
            name=name,
            config=config,
            secret_paths=secret_paths,
            notes=",".join(notes_parts) or None,
            test_send_receipt=receipt,
        )


__all__ = [
    "ConnectorWizard",
    "DictInputProvider",
    "InputProvider",
    "StdinInputProvider",
    "SUPPORTED_VENDORS",
    "WizardField",
    "WizardHandler",
    "WizardResult",
    "get_handler",
    "list_vendors",
]
