# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""RotationProvider factory — issuer-side rotation for foreign credentials.

``GitLabPatProvider`` drives ``POST /api/v4/personal_access_tokens/self/
rotate`` (GitLab >= 16.0). ALL HTTP is mocked via ``httpx.MockTransport``
— no live issuer API is ever called from tests. ``GuidedRotationProvider``
is the interactive fallback for issuers without API rotation.
"""

from __future__ import annotations


import httpx
import pytest

from axiom.extensions.builtins.secrets.foreign.rotation_providers import (
    ForeignRotationError,
    GitLabPatProvider,
    GuidedRotationProvider,
    build_rotation_provider,
    rotation_provider_kinds,
)

BASE = "https://gitlab.example.org"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestFactory:
    def test_kinds(self):
        assert set(rotation_provider_kinds()) >= {"gitlab-pat", "guided"}

    def test_build_gitlab(self):
        p = build_rotation_provider("gitlab-pat", issuer_url=BASE)
        assert p.kind == "gitlab-pat"

    def test_build_unknown_raises_with_known_kinds(self):
        with pytest.raises(KeyError) as exc:
            build_rotation_provider("nope")
        assert "gitlab-pat" in str(exc.value)

    def test_unknown_kind_falls_back_is_not_silent(self):
        # The factory never silently substitutes guided for an unknown kind.
        with pytest.raises(KeyError):
            build_rotation_provider("github-pat")


class TestGitLabPatProvider:
    def test_rotate_posts_self_rotate_with_current_token(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["method"] = request.method
            seen["token_header"] = request.headers.get("PRIVATE-TOKEN")
            return httpx.Response(200, json={
                "id": 42, "name": "rotated",
                "token": "glpat-NEW", "expires_at": "2026-07-30",
            })

        provider = GitLabPatProvider(base_url=BASE, http=_client(handler))
        outcome = provider.rotate("glpat-OLD", expires_at="2026-07-30")
        assert outcome.new_value == "glpat-NEW"
        assert outcome.expires_at == "2026-07-30"
        assert outcome.handle == "42"
        assert seen["method"] == "POST"
        assert seen["url"].startswith(
            f"{BASE}/api/v4/personal_access_tokens/self/rotate"
        )
        assert "expires_at=2026-07-30" in seen["url"]
        assert seen["token_header"] == "glpat-OLD"

    def test_rotate_defaults_expiry_to_one_week_out(self):
        def handler(request: httpx.Request) -> httpx.Response:
            params = dict(request.url.params)
            assert "expires_at" in params  # +1 week default per owner directive
            return httpx.Response(200, json={
                "id": 1, "token": "glpat-NEW",
                "expires_at": params["expires_at"],
            })

        provider = GitLabPatProvider(base_url=BASE, http=_client(handler))
        outcome = provider.rotate("glpat-OLD")
        assert outcome.expires_at  # server-echoed default

    def test_rotate_error_never_leaks_token(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "401 Unauthorized"})

        provider = GitLabPatProvider(base_url=BASE, http=_client(handler))
        with pytest.raises(ForeignRotationError) as exc:
            provider.rotate("glpat-SENTINEL-OLD")
        msg = str(exc.value)
        assert "glpat-SENTINEL-OLD" not in msg
        assert "401" in msg

    def test_rotate_405_hints_gitlab_version(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(405, json={"message": "405 Method Not Allowed"})

        provider = GitLabPatProvider(base_url=BASE, http=_client(handler))
        with pytest.raises(ForeignRotationError) as exc:
            provider.rotate("glpat-OLD")
        assert "16.0" in str(exc.value)

    def test_probe_get_user(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert str(request.url) == f"{BASE}/api/v4/user"
            if request.headers.get("PRIVATE-TOKEN") == "glpat-GOOD":
                return httpx.Response(200, json={"username": "ben"})
            return httpx.Response(401, json={"message": "401"})

        provider = GitLabPatProvider(base_url=BASE, http=_client(handler))
        ok, detail = provider.probe("glpat-GOOD")
        assert ok is True
        bad_ok, bad_detail = provider.probe("glpat-BAD")
        assert bad_ok is False
        assert "glpat-BAD" not in bad_detail

    def test_requires_issuer_url(self):
        with pytest.raises(ForeignRotationError):
            GitLabPatProvider(base_url="", http=None).rotate("x")


class TestGuidedProvider:
    def test_rotate_prompts_human_for_paste(self):
        prompts = []

        def prompt(msg: str) -> str:
            prompts.append(msg)
            return "pasted-new-token"

        provider = GuidedRotationProvider(
            issuer_url="https://issuer.example/tokens", user_prompt=prompt,
        )
        outcome = provider.rotate("old-token")
        assert outcome.new_value == "pasted-new-token"
        assert any("issuer.example" in p for p in prompts)

    def test_rotate_headless_fails_closed(self):
        provider = GuidedRotationProvider(user_prompt=None)
        with pytest.raises(ForeignRotationError) as exc:
            provider.rotate("old")
        assert "interactive" in str(exc.value).lower()

    def test_empty_paste_fails(self):
        provider = GuidedRotationProvider(user_prompt=lambda _m: "")
        with pytest.raises(ForeignRotationError):
            provider.rotate("old")

    def test_probe_default_is_honest_about_not_probing(self):
        ok, detail = GuidedRotationProvider(user_prompt=lambda _m: "x").probe("v")
        assert ok is True
        assert "not probed" in detail.lower()
