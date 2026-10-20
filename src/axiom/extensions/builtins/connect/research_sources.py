# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Research signal sources: Reddit and X, as first-class connectors.

Neither platform ships an official MCP server, and community wrappers
add a supply-chain seam while still needing the same credentials — so
the integration lives HERE, as Axiom connectors, and reaches Claude
Code (and every other harness) through the ONE composed axiom MCP
rather than a third-party server.

Auth lessons applied by construction:
- Tokens are minted lazily and RE-MINTED on expiry, never cached
  forever (the capability-TTL incident, generalized).
- ``verify()`` exercises a real search — "can I reach the API" is not
  "can I do the job".
- Missing credentials fail closed with the setup URL in the error.

Both sources normalize to one :class:`Post` shape so downstream
analysis is source-agnostic.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


class ResearchAuthError(RuntimeError):
    """Credential or API failure — message carries the remediation."""


@dataclass(frozen=True)
class Post:
    source: str          # "reddit" | "x"
    title: str
    url: str
    created_at: str
    engagement: dict     # source-appropriate counts (score/comments, likes/reposts)
    text: str = ""
    subsource: str = ""  # subreddit / author handle when known


def _default_http(url: str, *, method: str = "GET", headers: dict | None = None,
                  data: bytes | None = None, timeout: int = 30) -> Any:
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed API hosts
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        raise ResearchAuthError(f"HTTP {exc.code} from {url.split('?')[0]}: {body}") from exc


@dataclass
class RedditSource:
    """Reddit via OAuth2 app-only (client_credentials): public read.

    Needs a (free) script app from https://www.reddit.com/prefs/apps —
    client id + secret. App-only auth reads public content, which is
    all research needs; no user context, no write scope.
    """

    client_id: str
    client_secret: str
    user_agent: str
    http: Callable = field(default=_default_http)
    _token: str | None = field(default=None, repr=False)
    _token_expires_at: float = 0.0

    _TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
    _API = "https://oauth.reddit.com"
    _RENEWAL_MARGIN_S = 60

    def _require_creds(self) -> None:
        if not self.client_id or not self.client_secret:
            raise ResearchAuthError(
                "Reddit credentials missing. Create a free 'script' app at "
                "https://www.reddit.com/prefs/apps and set REDDIT_CLIENT_ID / "
                "REDDIT_CLIENT_SECRET (see `axi connect show reddit`)."
            )

    def _fresh_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - self._RENEWAL_MARGIN_S:
            return self._token
        self._require_creds()
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        payload = self.http(
            self._TOKEN_URL,
            method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "User-Agent": self.user_agent,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data=b"grant_type=client_credentials",
        )
        self._token = payload["access_token"]
        self._token_expires_at = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    def search(self, query: str, *, subreddit: str | None = None,
               limit: int = 25, timeframe: str = "month", sort: str = "top") -> list[Post]:
        token = self._fresh_token()
        base = f"{self._API}/r/{subreddit}/search" if subreddit else f"{self._API}/search"
        qs = urllib.parse.urlencode({
            "q": query, "limit": limit, "t": timeframe, "sort": sort,
            **({"restrict_sr": "1"} if subreddit else {}),
        })
        payload = self.http(
            f"{base}?{qs}",
            headers={"Authorization": f"Bearer {token}", "User-Agent": self.user_agent},
        )
        posts = []
        for child in payload.get("data", {}).get("children", []):
            d = child.get("data", {})
            posts.append(Post(
                source="reddit",
                title=d.get("title", ""),
                url="https://www.reddit.com" + d.get("permalink", ""),
                created_at=time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(d.get("created_utc", 0))
                ),
                engagement={"score": d.get("score", 0), "comments": d.get("num_comments", 0)},
                text=(d.get("selftext") or "")[:2000],
                subsource=d.get("subreddit", ""),
            ))
        return posts

    def comments(self, permalink: str, *, limit: int = 40) -> list[dict]:
        """Top-level comments of one thread (bodies + scores)."""
        token = self._fresh_token()
        path = permalink if permalink.startswith("/") else "/" + permalink
        payload = self.http(
            f"{self._API}{path}.json?limit={limit}&sort=top",
            headers={"Authorization": f"Bearer {token}", "User-Agent": self.user_agent},
        )
        out = []
        try:
            for child in payload[1]["data"]["children"]:
                d = child.get("data", {})
                if d.get("body"):
                    out.append({"author": d.get("author", "?"), "score": d.get("score", 0),
                                "body": d["body"][:1500]})
        except (IndexError, KeyError, TypeError):
            pass
        return out

    def verify(self) -> dict:
        """Thorough verification: mint + one real search, count results."""
        posts = self.search("test", limit=1)
        return {"ok": True, "sample_result_count": len(posts)}


@dataclass
class XSource:
    """X API v2 recent search via app bearer token.

    Meaningful search requires a PAID developer tier (Basic or above);
    the free tier's read quota is too small for research. Token from
    https://developer.x.com (Projects & Apps → Keys and tokens).
    """

    bearer_token: str
    http: Callable = field(default=_default_http)

    _API = "https://api.x.com/2/tweets/search/recent"

    def search(self, query: str, *, limit: int = 25) -> list[Post]:
        if not self.bearer_token:
            raise ResearchAuthError(
                "X bearer token missing. Create an app at https://developer.x.com "
                "(search requires a paid tier) and set X_BEARER_TOKEN "
                "(see `axi connect show x`)."
            )
        qs = urllib.parse.urlencode({
            "query": query,
            "max_results": max(10, min(limit, 100)),
            "tweet.fields": "created_at,public_metrics,author_id",
        })
        payload = self.http(
            f"{self._API}?{qs}",
            headers={"Authorization": f"Bearer {self.bearer_token}"},
        )
        posts = []
        for t in payload.get("data", []) or []:
            m = t.get("public_metrics", {})
            posts.append(Post(
                source="x",
                title=(t.get("text") or "")[:120],
                url=f"https://x.com/i/status/{t.get('id')}",
                created_at=t.get("created_at", ""),
                engagement={
                    "likes": m.get("like_count", 0),
                    "reposts": m.get("retweet_count", 0),
                    "replies": m.get("reply_count", 0),
                },
                text=(t.get("text") or "")[:2000],
                subsource=str(t.get("author_id", "")),
            ))
        return posts

    def verify(self) -> dict:
        posts = self.search("the", limit=10)
        return {"ok": True, "sample_result_count": len(posts)}


__all__ = ["Post", "RedditSource", "XSource", "ResearchAuthError"]
