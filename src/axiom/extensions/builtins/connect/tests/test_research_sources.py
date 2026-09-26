# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Research-source connectors: Reddit (OAuth2 app-only) and X (bearer).

Contract under test: credentials resolve from declared env, auth happens
lazily and re-mints on expiry (the lesson of the capability-TTL
incident: a token minted once and cached forever loses authority under
a long-lived host), verification exercises a REAL search (not just a
token mint — "can I reach the API" is not "can I do the job"), and
results normalize to one shape so downstream analysis is
source-agnostic."""

from __future__ import annotations

import json

import pytest

from axiom.extensions.builtins.connect.research_sources import (
    Post,
    RedditSource,
    ResearchAuthError,
    XSource,
)


class _FakeHttp:
    """Records requests; serves queued responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, url, *, method="GET", headers=None, data=None, timeout=30):
        self.requests.append({"url": url, "method": method, "headers": headers or {}, "data": data})
        status, body = self.responses.pop(0)
        if status >= 400:
            raise ResearchAuthError(f"HTTP {status}: {body}")
        return json.loads(body)


def _reddit_token_response():
    return (200, json.dumps({"access_token": "tok-1", "expires_in": 3600, "token_type": "bearer"}))


def _reddit_search_response():
    return (
        200,
        json.dumps({
            "data": {
                "children": [
                    {"data": {
                        "title": "Claude Code ate my quota",
                        "permalink": "/r/ClaudeAI/comments/abc/claude_code_ate_my_quota/",
                        "score": 412, "num_comments": 208,
                        "created_utc": 1758400000, "subreddit": "ClaudeAI",
                        "selftext": "it spawned 40 subagents",
                    }},
                ]
            }
        }),
    )


class TestRedditSource:
    def _source(self, http):
        return RedditSource(
            client_id="cid", client_secret="csec",
            user_agent="axiom-research/0.1", http=http,
        )

    def test_search_mints_token_then_searches_with_it(self):
        http = _FakeHttp([_reddit_token_response(), _reddit_search_response()])
        posts = self._source(http).search("claude code quota", limit=5)
        assert http.requests[0]["url"].endswith("/api/v1/access_token")
        assert http.requests[1]["headers"]["Authorization"] == "Bearer tok-1"
        assert "oauth.reddit.com" in http.requests[1]["url"]
        (p,) = posts
        assert isinstance(p, Post)
        assert p.source == "reddit"
        assert p.title == "Claude Code ate my quota"
        assert p.url.startswith("https://www.reddit.com/r/ClaudeAI/")
        assert p.engagement == {"score": 412, "comments": 208}

    def test_token_reused_within_ttl_not_per_call(self):
        http = _FakeHttp([_reddit_token_response(), _reddit_search_response(), _reddit_search_response()])
        src = self._source(http)
        src.search("a")
        src.search("b")
        token_calls = [r for r in http.requests if r["url"].endswith("/api/v1/access_token")]
        assert len(token_calls) == 1

    def test_expired_token_is_reminted_not_presented(self):
        """The capability-TTL lesson, applied here by construction."""
        http = _FakeHttp([
            _reddit_token_response(), _reddit_search_response(),
            _reddit_token_response(), _reddit_search_response(),
        ])
        src = self._source(http)
        src.search("a")
        src._token_expires_at = 0  # force expiry
        src.search("b")
        token_calls = [r for r in http.requests if r["url"].endswith("/api/v1/access_token")]
        assert len(token_calls) == 2

    def test_verify_exercises_a_real_search(self):
        http = _FakeHttp([_reddit_token_response(), _reddit_search_response()])
        result = self._source(http).verify()
        assert result["ok"] is True
        assert result["sample_result_count"] == 1
        assert any("oauth.reddit.com" in r["url"] for r in http.requests)

    def test_missing_credentials_fail_closed_with_setup_pointer(self):
        src = RedditSource(client_id="", client_secret="", user_agent="ua", http=_FakeHttp([]))
        with pytest.raises(ResearchAuthError) as exc:
            src.search("anything")
        assert "reddit.com/prefs/apps" in str(exc.value)


def _x_search_response():
    return (
        200,
        json.dumps({
            "data": [
                {"id": "1", "text": "my agent said done. it was not done.",
                 "created_at": "2026-09-20T12:00:00Z",
                 "public_metrics": {"like_count": 950, "retweet_count": 210, "reply_count": 88}},
            ]
        }),
    )


class TestXSource:
    def test_search_uses_bearer_and_normalizes(self):
        http = _FakeHttp([_x_search_response()])
        posts = XSource(bearer_token="xb-1", http=http).search("agent said done", limit=10)
        req = http.requests[0]
        assert req["headers"]["Authorization"] == "Bearer xb-1"
        assert "api.x.com/2/tweets/search/recent" in req["url"]
        (p,) = posts
        assert p.source == "x"
        assert p.engagement == {"likes": 950, "reposts": 210, "replies": 88}

    def test_verify_exercises_a_real_search(self):
        http = _FakeHttp([_x_search_response()])
        result = XSource(bearer_token="xb-1", http=http).verify()
        assert result["ok"] is True
        assert result["sample_result_count"] == 1

    def test_missing_bearer_fails_closed_with_setup_pointer(self):
        with pytest.raises(ResearchAuthError) as exc:
            XSource(bearer_token="", http=_FakeHttp([])).search("q")
        assert "developer.x.com" in str(exc.value)


class TestDescriptorsRegistered:
    def test_both_descriptors_register_into_the_fabric(self):
        from axiom.extensions.builtins.connect.connectors import (
            reddit_connector_descriptor,
            x_connector_descriptor,
        )

        r = reddit_connector_descriptor()
        x = x_connector_descriptor()
        assert r.name == "ai.axiom.connector.reddit"
        assert x.name == "ai.axiom.connector.x"
        for d in (r, x):
            assert d.kind == "research_source"
            secret_envs = [e for e in d.env if e.is_secret]
            assert secret_envs, f"{d.name}: credentials must be declared secret"
            assert d.setup is not None and d.setup.urls, f"{d.name}: setup deep links required"
