# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`dev up` learns whether the sign-in provider answers before it says "SSO" (#1842)."""

from __future__ import annotations

from axiom.extensions.builtins.dev import signin


def _client(provider: str) -> signin.SignInClient:
    return signin.SignInClient(
        credential="c", issuer="https://idp.example.test", client_id="x", provider=provider, public=True
    )


def test_an_issuer_that_refuses_the_connection_does_not_answer() -> None:
    def refused(url: str) -> None:
        raise OSError("connection refused")

    assert signin.answers(_client("https://idp.example.test"), fetch=refused) is False


def test_an_issuer_that_serves_its_document_answers_and_the_right_url_is_asked() -> None:
    asked: list[str] = []
    assert signin.answers(_client("https://idp.example.test/"), fetch=asked.append) is True
    assert asked == ["https://idp.example.test/.well-known/openid-configuration"]


def test_a_preset_provider_is_never_asked() -> None:
    def never(url: str) -> None:
        raise AssertionError("a preset's document is not read at start")

    assert signin.answers(_client("entra"), fetch=never) is True
