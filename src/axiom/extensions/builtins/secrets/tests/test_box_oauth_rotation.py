# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Rotating a credential that rotates BY USE, without spending it to look at it.

A Box OAuth refresh token is not like a GitLab PAT. It is **single-use**: every
refresh returns a new refresh token and invalidates the one presented, and the
replacement lives 60 days from that moment. Nothing external ages it out. It
dies of *disuse*, which is exactly how this deployment lost one — the ingest
stopped, nothing refreshed for sixty days, and the token was gone before anyone
looked.

Two consequences shape this provider, and both are the opposite of the GitLab
one:

1. **The refresh IS the rotation.** There is no separate rotate endpoint to
   call. Exercising the credential on a schedule is what keeps it alive, so
   KEEP running this hourly is not hygiene, it is the mechanism.

2. **You cannot probe it without consuming it.** ``GitLabPatProvider.probe``
   does a harmless ``GET /user``. The equivalent here would be a refresh, which
   mutates. A probe that spends the thing it is checking is not a probe, so
   this one reads the cached ACCESS token instead, and when there is none it
   reports *unknown* rather than guessing. Returning False would say DEAD about
   a perfectly live credential.
"""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.secrets.foreign.rotation_providers import (
    BoxOAuthProvider,
    ForeignRotationError,
)


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


class _Http:
    """Records calls so a test can assert what was NOT called."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def request(self, method, url, **kw):
        self.calls.append((method, url))
        return self._responses.pop(0) if self._responses else _Resp(500)


def _provider(http, token_store=None):
    return BoxOAuthProvider(
        client_id="cid", client_secret="csec", token_store=token_store, http=http
    )


class TestRotation:
    def test_it_is_unattended(self):
        assert BoxOAuthProvider.kind == "box-oauth"
        assert BoxOAuthProvider.interactive is False

    def test_rotate_returns_the_new_refresh_token(self):
        http = _Http(_Resp(200, {"refresh_token": "NEW", "access_token": "AT", "expires_in": 3600}))
        out = _provider(http).rotate("OLD")

        assert out.new_value == "NEW", "the ROTATED token, never the presented one"
        assert http.calls == [("POST", "https://api.box.com/oauth2/token")]

    def test_expiry_is_sixty_days_out_and_not_negotiable(self):
        """Box sets the refresh lifetime; we cannot ask for a different one.
        Recording it anyway is what lets the audit warn instead of the
        credential sitting in the no_expiry bucket where the last one died."""
        http = _Http(_Resp(200, {"refresh_token": "NEW", "access_token": "AT"}))
        out = _provider(http).rotate("OLD", expires_at="2099-01-01")

        assert out.expires_at != "2099-01-01", "a requested expiry is a fiction here"
        assert out.expires_at is not None

    def test_a_response_without_a_refresh_token_is_an_error_not_a_silent_reuse(self):
        """Reusing the presented token would look like success and leave a
        credential Box has already invalidated."""
        http = _Http(_Resp(200, {"access_token": "AT"}))
        with pytest.raises(ForeignRotationError, match="refresh"):
            _provider(http).rotate("OLD")

    def test_failure_never_echoes_the_token(self):
        http = _Http(_Resp(400, {"error": "invalid_grant"}))
        with pytest.raises(ForeignRotationError) as exc:
            _provider(http).rotate("SECRET-TOKEN-VALUE")

        assert "SECRET-TOKEN-VALUE" not in str(exc.value)

    def test_an_expired_grant_says_so_in_the_terms_of_the_real_failure(self):
        http = _Http(_Resp(400, {"error": "invalid_grant"}))
        with pytest.raises(ForeignRotationError) as exc:
            _provider(http).rotate("OLD")

        msg = str(exc.value).lower()
        assert "60" in msg or "disuse" in msg or "re-run the login" in msg


class TestProbeMustNotSpendTheCredential:
    """The property that distinguishes this from every other provider."""

    def test_probe_does_not_call_the_token_endpoint(self, tmp_path):
        store = tmp_path / "box.json"
        store.write_text(json.dumps({"refresh_token": "RT", "access_token": "AT"}))
        http = _Http(_Resp(200, {"login": "someone@example.edu"}))

        alive, detail = _provider(http, token_store=str(store)).probe("RT")

        assert alive is True
        assert all("oauth2/token" not in url for _, url in http.calls), (
            "probing must never refresh — that would consume the credential"
        )
        assert "someone@example.edu" in detail

    def test_no_cached_access_token_reports_unknown_not_dead(self, tmp_path):
        store = tmp_path / "box.json"
        store.write_text(json.dumps({"refresh_token": "RT"}))
        http = _Http()

        alive, detail = _provider(http, token_store=str(store)).probe("RT")

        assert alive is None, "unknown is not DEAD; the CLI renders None as blank"
        assert http.calls == [], "must not touch the network to say 'I cannot tell'"
        assert "without consuming" in detail

    def test_a_rejected_access_token_is_reported_dead(self, tmp_path):
        store = tmp_path / "box.json"
        store.write_text(json.dumps({"access_token": "STALE"}))
        http = _Http(_Resp(401))

        alive, _ = _provider(http, token_store=str(store)).probe("RT")
        assert alive is False

    def test_a_missing_store_is_unknown_rather_than_an_exception(self, tmp_path):
        alive, detail = _provider(_Http(), token_store=str(tmp_path / "nope")).probe("RT")
        assert alive is None
        assert detail
