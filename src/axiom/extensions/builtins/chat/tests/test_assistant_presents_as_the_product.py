# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The assistant introduces itself as the product the user installed.

Asked "who are you?", a new user's assistant said it was "the 'Loop' agent
(AXI)". The identity layer carried the platform's agent design notes verbatim
— REPL roles, the internal roster, a film analogy — and nothing said what the
user should be told. A model reads the most specific name it is given, and the
only names it was given were internal ones.

The platform does not know which product it is running under; branding does.
So the first thing in the identity layer is a sentence built from the active
branding, and the internal names are marked as architecture rather than
something to introduce oneself as.

Also: a user who types the platform's command on a machine where a product is
installed is told the product's command, so they are not left talking to a
generic assistant without knowing there is another door.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat.agent import ChatAgent
from axiom.infra import branding
from axiom.infra.bus import EventBus
from axiom.infra.gateway import Gateway
from axiom.infra.orchestrator.session import Session


@pytest.fixture
def acme_brand():
    branding.register(
        branding.BrandingConfig(
            cli_name="acme",
            product_name="Acme OS",
            mascot_name="Ace",
            package_name="acme-os",
        )
    )
    yield
    branding.reset()


def _prompt(tmp_path) -> str:
    agent = ChatAgent(
        gateway=MagicMock(spec=Gateway),  # the LLM boundary; not called
        bus=EventBus(log_path=tmp_path / "events.jsonl"),
        session=Session(),
    )
    return agent._build_system_prompt()


def test_a_branded_install_names_its_product_first(tmp_path, acme_brand):
    prompt = _prompt(tmp_path)
    head = prompt[:400]
    assert "Ace" in head and "Acme OS" in head, head


def test_internal_agent_names_are_not_an_introduction(tmp_path, acme_brand):
    prompt = _prompt(tmp_path)
    head = prompt[:800]
    assert "never introduce yourself as" in head.lower(), head


def test_the_platform_default_presents_as_the_platform(tmp_path):
    branding.reset()
    head = _prompt(tmp_path)[:400]
    assert "Axi" in head and "Axiom" in head, head


class TestTheProductsOwnCommandIsNamed:
    def _member(self, package, product, cli=""):
        return branding.PortfolioMember(
            package_name=package,
            product_name=product,
            wrapper_binary=f"{product}-Background-Service",
            cli_name=cli,
        )

    def test_a_product_installed_under_the_platform_command_is_named(self):
        from axiom.extensions.builtins.chat.cli import installed_product_hint

        hint = installed_product_hint(
            branding.BrandingConfig(),
            [
                self._member("axiom-os-lm", "Axiom", "axi"),
                self._member("acme-os", "Acme OS", "acme"),
            ],
        )
        assert hint is not None
        assert "Acme OS" in hint and "acme chat" in hint

    def test_running_as_the_product_says_nothing(self):
        from axiom.extensions.builtins.chat.cli import installed_product_hint

        hint = installed_product_hint(
            branding.BrandingConfig(cli_name="acme", product_name="Acme OS", package_name="acme-os"),
            [self._member("acme-os", "Acme OS", "acme")],
        )
        assert hint is None

    def test_a_product_that_declares_no_command_is_not_guessed(self):
        from axiom.extensions.builtins.chat.cli import installed_product_hint

        hint = installed_product_hint(
            branding.BrandingConfig(), [self._member("acme-os", "Acme OS")]
        )
        assert hint is None
