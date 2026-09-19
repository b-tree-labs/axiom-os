# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Graph endpoint's one load-bearing behavior: writes carry If-Match,
and a concurrent remote save (412) surfaces as VersionConflict — never a
silent overwrite. Transport is injected; no network in this suite."""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.publishing.mirror import VersionConflict
from axiom.extensions.builtins.publishing.providers.sharepoint import (
    GraphEditorEndpoint,
)


class _Resp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Session:
    def __init__(self):
        self.puts = []
        self.put_response = _Resp(json_data={"eTag": "v2"})

    def get(self, url, headers=None):
        if "$select" in url:
            return _Resp(json_data={
                "eTag": "v1",
                "lastModifiedBy": {"application": {"displayName": "WebEditor"}},
            })
        if url.endswith("/content"):
            return _Resp(text="remote body\n")
        if "/versions" in url:
            return _Resp(json_data={"value": [
                {"id": "2.0", "lastModifiedBy": {"application": {"displayName": "WebEditor"}}},
                {"id": "1.0", "lastModifiedBy": {"user": {"displayName": "A Person"}}},
            ]})
        raise AssertionError(f"unexpected GET {url}")

    def put(self, url, headers=None, data=None):
        self.puts.append({"url": url, "headers": headers, "data": data})
        return self.put_response


@pytest.fixture
def endpoint():
    session = _Session()
    ep = GraphEditorEndpoint(
        share_url="https://example.sharepoint.com/:t:/g/doc",
        token_getter=lambda: "tok",
        session=session,
    )
    return ep, session


def test_read_returns_text_version_and_author(endpoint):
    ep, _ = endpoint
    doc = ep.read()
    assert doc.text == "remote body\n"
    assert doc.version == "v1"
    assert doc.author_app == "WebEditor"


def test_write_sends_if_match_and_returns_new_etag(endpoint):
    ep, session = endpoint
    new = ep.write("new body\n", expected_version="v1")
    assert new == "v2"
    assert session.puts[0]["headers"]["If-Match"] == "v1"
    assert session.puts[0]["data"] == b"new body\n"


def test_write_412_raises_version_conflict(endpoint):
    ep, session = endpoint
    session.put_response = _Resp(status_code=412)
    with pytest.raises(VersionConflict):
        ep.write("new body\n", expected_version="v1")


def test_versions_maps_ids_and_authors(endpoint):
    ep, _ = endpoint
    vs = ep.versions(limit=2)
    assert [v.version for v in vs] == ["2.0", "1.0"]
    assert vs[0].author_app == "WebEditor"
    assert vs[1].author_app == "A Person"


def test_from_share_url_token_getter_returns_the_bearer_string(monkeypatch):
    """Regression: `_acquire_token_interactive` returns the token STRING, so
    the from_share_url wrapper must not index it with ['access_token'] — that
    crashed the first live sync with "string indices must be integers"."""
    from axiom.extensions.builtins.publishing.providers import sharepoint

    monkeypatch.setattr(sharepoint.SharePointProvider, "__post_init__",
                        lambda self: None)
    monkeypatch.setattr(sharepoint.SharePointProvider,
                        "_acquire_token_interactive",
                        lambda self: "ey.the.bearer.token")
    endpoint = sharepoint.GraphEditorEndpoint.from_share_url(
        "https://example.sharepoint.com/personal/x/Documents/d.md")
    assert endpoint.token_getter() == "ey.the.bearer.token"
