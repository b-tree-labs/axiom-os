# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``VendorHttpClient`` — stdlib JSON-over-HTTPS for vendor key APIs (#16).

The client is the transport a vendor-API rotation strategy calls to mint and
revoke credentials. Two properties matter beyond "it makes the request":

- it carries the vendor admin credential in an auth header, and
- it NEVER surfaces that credential — or a response body — in an error, because
  a mint response *is* the new secret.

All exercised against a fake opener; no network.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from axiom.extensions.builtins.secrets.rotation.vendor_client import (
    VendorHTTPError,
    VendorHttpClient,
)


class FakeResp:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload


class RecordingOpener:
    """Stands in for ``urllib.request.urlopen``; records the Request objects."""

    def __init__(self, payload: bytes = b"{}", error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list = []

    def __call__(self, req, timeout=None):
        self.calls.append(req)
        if self.error is not None:
            raise self.error
        return FakeResp(self.payload)


def _client(opener) -> VendorHttpClient:
    return VendorHttpClient("https://api.vendor.com/", "TOKEN", opener=opener)


class TestRequestShape:
    def test_post_sends_json_body_and_auth_header(self):
        op = RecordingOpener(payload=b'{"api_key_id":"K1","api_key":"SG.x"}')
        out = _client(op).post("/v3/api_keys", {"name": "n"})
        assert out == {"api_key_id": "K1", "api_key": "SG.x"}
        req = op.calls[0]
        assert req.full_url == "https://api.vendor.com/v3/api_keys"
        assert req.get_method() == "POST"
        assert req.data == json.dumps({"name": "n"}).encode("utf-8")
        # urllib capitalizes header keys: "Authorization", "Content-type"
        assert req.headers["Authorization"] == "Bearer TOKEN"
        assert req.headers["Content-type"] == "application/json"

    def test_get_has_no_body(self):
        op = RecordingOpener(payload=b'{"result":[]}')
        assert _client(op).get("/v3/api_keys") == {"result": []}
        assert op.calls[0].data is None
        assert op.calls[0].get_method() == "GET"

    def test_delete_returns_none_and_tolerates_empty_body(self):
        op = RecordingOpener(payload=b"")
        assert _client(op).delete("/v3/api_keys/K1") is None
        assert op.calls[0].get_method() == "DELETE"

    def test_base_url_trailing_slash_not_doubled(self):
        op = RecordingOpener()
        _client(op).get("/p")
        assert op.calls[0].full_url == "https://api.vendor.com/p"

    def test_custom_auth_header_and_scheme(self):
        op = RecordingOpener()
        VendorHttpClient(
            "https://x", "TT", auth_header="X-Api-Key", auth_scheme="", opener=op
        ).get("/p")
        # empty scheme → raw token, no "Bearer " prefix
        assert op.calls[0].headers["X-api-key"] == "TT"


class TestErrorsAreRedacted:
    def test_http_error_maps_without_leaking_body_or_token(self):
        err = urllib.error.HTTPError(
            "https://x/p", 401, "Unauthorized", {}, io.BytesIO(b'{"secret":"leak"}')
        )
        with pytest.raises(VendorHTTPError) as ei:
            _client(RecordingOpener(error=err)).post("/p", {})
        msg = str(ei.value)
        assert "401" in msg
        assert "leak" not in msg  # response body never surfaced
        assert "TOKEN" not in msg  # nor the admin token

    def test_urlerror_maps_to_vendorhttperror(self):
        with pytest.raises(VendorHTTPError):
            _client(RecordingOpener(error=urllib.error.URLError("boom"))).get("/p")

    def test_non_json_success_body_is_tolerated(self):
        assert _client(RecordingOpener(payload=b"OK")).post("/p", {}) == {}
