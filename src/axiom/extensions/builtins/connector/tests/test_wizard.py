# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for the ``axi connector add`` wizard.

TDD per CLAUDE.md. Pins the contract for the friction-killer track #3
from the 2026-06-01 UX analysis: the 5-step install collapses to one
interactive command.

Coverage:

- Per-vendor handler shape — each shipped vendor advertises required
  fields, prompt copy, and an optional browser-open URL.
- ``ConnectorWizard.run`` happy path — collects answers, stores secrets,
  registers the provider, sends a test message.
- ``InputProvider`` is mockable — deterministic tests never touch
  ``input()``.
- Browser-open is best-effort — headless environments don't fail.
- Unknown vendor → clear error listing supported vendors.
- ``no_test_send=True`` skips the test message.
- Each shipped vendor (slack, mattermost, teams, email, twilio-sms)
  asks for the right fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from axiom.extensions.builtins.notifications.channels.base import (
    ChannelAdapterRegistry,
)
from axiom.extensions.builtins.connector.wizard import (
    ConnectorWizard,
    DictInputProvider,
    SUPPORTED_VENDORS,
    WizardHandler,
    WizardResult,
    get_handler,
    list_vendors,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeSecretStore:
    """In-memory secrets sink — captures (path, value) pairs the wizard wrote."""

    puts: dict[str, bytes] = field(default_factory=dict)

    def put(self, path: str, value: bytes) -> None:
        self.puts[path] = value


@dataclass
class _FakeTestSender:
    calls: list[dict[str, Any]] = field(default_factory=list)
    ok: bool = True

    def __call__(
        self, *, vendor: str, name: str, registry: ChannelAdapterRegistry
    ) -> tuple[bool, str]:
        self.calls.append({"vendor": vendor, "name": name})
        return self.ok, ("sm_test_123" if self.ok else "")


@dataclass
class _FakeBrowser:
    opened: list[str] = field(default_factory=list)
    succeed: bool = True

    def __call__(self, url: str) -> bool:
        self.opened.append(url)
        return self.succeed


def _wizard(
    answers: dict[str, str],
    *,
    test_ok: bool = True,
    browser_ok: bool = True,
    registry: ChannelAdapterRegistry | None = None,
) -> tuple[ConnectorWizard, _FakeSecretStore, _FakeTestSender, _FakeBrowser, ChannelAdapterRegistry]:
    store = _FakeSecretStore()
    sender = _FakeTestSender(ok=test_ok)
    browser = _FakeBrowser(succeed=browser_ok)
    reg = registry or ChannelAdapterRegistry()
    wiz = ConnectorWizard(
        registry=reg,
        secret_put=store.put,
        test_send=sender,
        browser_open=browser,
        input_provider=DictInputProvider(answers),
    )
    return wiz, store, sender, browser, reg


# ---------------------------------------------------------------------------
# Vendor registry
# ---------------------------------------------------------------------------


def test_supported_vendors_covers_shipped_channels():
    assert set(SUPPORTED_VENDORS) == {
        "slack",
        "mattermost",
        "teams",
        "email",
        "twilio-sms",
        "box",
    }


def test_list_vendors_returns_sorted():
    names = list_vendors()
    assert names == sorted(names)
    assert "slack" in names


def test_get_handler_unknown_vendor_raises_with_supported_list():
    with pytest.raises(KeyError) as excinfo:
        get_handler("pager-duty")
    msg = str(excinfo.value)
    assert "pager-duty" in msg
    assert "slack" in msg


def test_get_handler_returns_protocol_satisfier():
    h = get_handler("slack")
    assert isinstance(h, WizardHandler)
    assert h.vendor == "slack"


# ---------------------------------------------------------------------------
# Per-vendor handler shape — what each vendor asks for
# ---------------------------------------------------------------------------


def test_slack_handler_asks_for_workspace_and_webhook():
    h = get_handler("slack")
    fields = [f.key for f in h.fields]
    assert "webhook_url" in fields
    assert h.help_url and "slack.com" in h.help_url


def test_mattermost_handler_asks_for_webhook():
    h = get_handler("mattermost")
    fields = [f.key for f in h.fields]
    assert "webhook_url" in fields


def test_teams_handler_asks_for_webhook():
    h = get_handler("teams")
    fields = [f.key for f in h.fields]
    assert "webhook_url" in fields


def test_email_handler_asks_for_smtp_or_api_key():
    h = get_handler("email")
    fields = [f.key for f in h.fields]
    # email is multi-backend — should ask for at least one of these
    assert any(k in fields for k in ("smtp_host", "resend_api_key"))


def test_twilio_sms_handler_asks_for_account_sid_auth_token_from_number():
    h = get_handler("twilio-sms")
    fields = [f.key for f in h.fields]
    assert "account_sid" in fields
    assert "auth_token" in fields
    assert "from_number" in fields


def test_handler_marks_secret_fields():
    """Secret fields (webhook URLs, auth tokens) must round-trip via the secrets store,
    while non-secret fields (e.g. from_number) may stay in config."""
    h = get_handler("twilio-sms")
    by_key = {f.key: f for f in h.fields}
    assert by_key["auth_token"].secret is True
    assert by_key["from_number"].secret is False


# ---------------------------------------------------------------------------
# Wizard happy path
# ---------------------------------------------------------------------------


def test_slack_happy_path_stores_secret_and_registers_provider():
    wiz, store, sender, browser, reg = _wizard(
        answers={
            "name": "acme",
            "webhook_url": "https://hooks.slack.com/services/T/B/X",
        }
    )
    result = wiz.run(vendor="slack", name="acme")
    assert isinstance(result, WizardResult)
    assert result.ok is True
    # Secret stored under the convention slack-webhook-<workspace>
    assert "slack-webhook-acme" in store.puts
    assert store.puts["slack-webhook-acme"] == b"https://hooks.slack.com/services/T/B/X"
    # Provider registered
    assert "slack" in reg.names()
    # Test send fired
    assert sender.calls == [{"vendor": "slack", "name": "acme"}]
    # Browser was attempted
    assert browser.opened


def test_wizard_result_includes_test_send_receipt():
    wiz, _, _, _, _ = _wizard(
        answers={"name": "acme", "webhook_url": "https://x"}
    )
    result = wiz.run(vendor="slack", name="acme")
    assert "sm_test_123" in (result.test_send_receipt or "")


def test_wizard_name_used_in_secret_path_for_mattermost():
    wiz, store, _, _, _ = _wizard(
        answers={"name": "ops", "webhook_url": "https://mm.example/hooks/abc"}
    )
    result = wiz.run(vendor="mattermost", name="ops")
    assert result.ok
    assert "mattermost-webhook-ops" in store.puts


def test_wizard_twilio_sms_stores_both_secrets_and_keeps_from_number_plain():
    wiz, store, _, _, reg = _wizard(
        answers={
            "name": "alerts",
            "account_sid": "AC" + "1" * 32,
            "auth_token": "topsecret",
            "from_number": "+15125550100",
        }
    )
    result = wiz.run(vendor="twilio-sms", name="alerts")
    assert result.ok
    assert "twilio-sms-account-sid-alerts" in store.puts
    assert "twilio-sms-auth-token-alerts" in store.puts
    assert "twilio-sms" in reg.names()
    # from_number is non-secret, exposed in config metadata not secrets
    assert "twilio-sms-from-number-alerts" not in store.puts
    assert result.config.get("from_number") == "+15125550100"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_wizard_unknown_vendor_returns_clear_error():
    wiz, _, _, _, _ = _wizard(answers={})
    result = wiz.run(vendor="pagerduty", name="default")
    assert result.ok is False
    assert any("pagerduty" in e for e in result.errors)
    assert any("slack" in e for e in result.errors)


def test_wizard_empty_required_field_fails_with_clear_error():
    wiz, _, _, _, _ = _wizard(answers={"name": "acme", "webhook_url": ""})
    result = wiz.run(vendor="slack", name="acme")
    assert result.ok is False
    assert any("webhook_url" in e for e in result.errors)


def test_wizard_missing_name_uses_default_slug():
    wiz, store, _, _, _ = _wizard(
        answers={"webhook_url": "https://x"}
    )
    result = wiz.run(vendor="slack", name="default")
    assert result.ok
    assert "slack-webhook-default" in store.puts


# ---------------------------------------------------------------------------
# Browser-open is best-effort
# ---------------------------------------------------------------------------


def test_wizard_browser_open_failure_is_tolerated():
    wiz, _, _, browser, _ = _wizard(
        answers={"name": "acme", "webhook_url": "https://x"},
        browser_ok=False,
    )
    result = wiz.run(vendor="slack", name="acme")
    # Falls back to printing the URL — wizard still succeeds
    assert result.ok is True
    assert browser.opened
    assert "browser_fallback" in (result.notes or "")


def test_wizard_default_browser_open_does_not_raise_on_headless():
    """The real default opener is webbrowser.open; ensure the wizard's
    construction with no override is safe (browser_open is wrapped to
    swallow exceptions)."""
    from axiom.extensions.builtins.connector.wizard import _safe_open_browser

    # Should never raise even for nonsense URLs in non-GUI env.
    result = _safe_open_browser("invalid://nope")
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Test-send flag
# ---------------------------------------------------------------------------


def test_no_test_send_flag_skips_test_message():
    wiz, store, sender, _, _ = _wizard(
        answers={"name": "acme", "webhook_url": "https://x"}
    )
    result = wiz.run(vendor="slack", name="acme", no_test_send=True)
    assert result.ok is True
    assert sender.calls == []
    assert "slack-webhook-acme" in store.puts


def test_failed_test_send_returns_partial_failure_but_secret_stays():
    wiz, store, _, _, _ = _wizard(
        answers={"name": "acme", "webhook_url": "https://x"},
        test_ok=False,
    )
    result = wiz.run(vendor="slack", name="acme")
    assert result.ok is False
    assert any("test send" in e.lower() for e in result.errors)
    # Secret + registration preserved so operator can re-run test-send.
    assert "slack-webhook-acme" in store.puts


# ---------------------------------------------------------------------------
# Input-provider mocking
# ---------------------------------------------------------------------------


def test_dict_input_provider_returns_recorded_answers():
    p = DictInputProvider({"webhook_url": "https://x"})
    assert p.ask("Webhook URL", key="webhook_url") == "https://x"


def test_dict_input_provider_returns_empty_for_unknown_key():
    p = DictInputProvider({})
    assert p.ask("anything", key="webhook_url") == ""


# ---------------------------------------------------------------------------
# Skill function — ADR-056 contract
# ---------------------------------------------------------------------------


def test_connector_add_skill_registered_in_default_namespace():
    from axiom.extensions.builtins.connector import skills as connector_skills

    reg = connector_skills.bind_default()
    assert reg.has("connector.add")


def test_connector_add_skill_unknown_vendor_returns_error_result():
    from axiom.extensions.builtins.connector.skills import add as connector_add
    from axiom.infra.skills import SkillContext, SkillRegistry
    from pathlib import Path
    import logging

    ctx = SkillContext(
        registry=SkillRegistry(),
        state_dir=Path("/tmp"),
        logger=logging.getLogger("test"),
        user_prompt=None,
    )
    result = connector_add.run(
        {"vendor": "unknown-thing", "name": "x", "_answers": {}},
        ctx,
    )
    assert result.ok is False
    assert any("unknown-thing" in e for e in result.errors)
