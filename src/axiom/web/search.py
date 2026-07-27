# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""In-enclave web search + fetch — an EC-compatible replacement for a model's
cloud-side web tools.

Export-controlled development is *not* air-gapped: the enclave has outbound
internet, and developers may run web searches (it is not a SCIF). What EC
forbids is sending *controlled technical data* to an uncleared party — so the
model stays in-enclave and only a short search **query** leaves, to a chosen
search provider. The caller (see the ``axiom_web__*`` MCP tools) classifies the
query first and withholds it if it carries controlled terms.

Pluggable backend (config ``web.search.provider``; per-provider API key from the
vault or ``<PROVIDER>_API_KEY``). All five providers are preconfigured; the
default is the no-key option (DuckDuckGo) so it works immediately.

    from axiom.web.search import search, fetch
    search("anthropic responses api spec", k=5)      # -> {provider, results:[{title,url,snippet}]}
    fetch("https://example.com/page")                # -> {url, text, ok}
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

__all__ = ["search", "fetch", "available_providers", "resolve_provider"]

_DEFAULT_PROVIDER = "duckduckgo"
_TIMEOUT = 20

# provider -> (needs_key, key_env). Key also resolvable from the vault by name.
_PROVIDERS: dict[str, tuple[bool, str]] = {
    "duckduckgo": (False, ""),
    "tavily": (True, "TAVILY_API_KEY"),
    "brave": (True, "BRAVE_API_KEY"),
    "exa": (True, "EXA_API_KEY"),
    "serpapi": (True, "SERPAPI_API_KEY"),
}


def available_providers() -> list[str]:
    return list(_PROVIDERS)


def _api_key(provider: str, key_env: str) -> str | None:
    if key_env and os.environ.get(key_env):
        return os.environ[key_env]
    try:
        from axiom.infra.connections import get_credential

        return get_credential(f"web-search:{provider}") or get_credential(provider)
    except Exception:  # noqa: BLE001
        return None


def resolve_provider(explicit: str | None = None) -> str:
    """Configured provider: explicit arg → settings ``web.search.provider`` →
    default. Falls back to the no-key default if a keyed provider has no key."""
    name = explicit
    if not name:
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            name = SettingsStore().get("web.search.provider", _DEFAULT_PROVIDER)
        except Exception:  # noqa: BLE001
            name = _DEFAULT_PROVIDER
    name = (name or _DEFAULT_PROVIDER).lower()
    if name not in _PROVIDERS:
        return _DEFAULT_PROVIDER
    needs_key, key_env = _PROVIDERS[name]
    if needs_key and not _api_key(name, key_env):
        return _DEFAULT_PROVIDER  # graceful: no key → free default
    return name


def _http_json(url: str, *, data: dict | None = None, headers: dict | None = None) -> Any:
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.loads(r.read())


# --- per-provider search (each returns list[{title,url,snippet}]) ------------


def _search_duckduckgo(query: str, k: int, _key: str | None) -> list[dict[str, str]]:
    # No-key HTML endpoint (lite). Best-effort parse; quality < keyed providers.
    import re

    url = "https://lite.duckduckgo.com/lite/"
    req = urllib.request.Request(
        url, data=urllib.parse.urlencode({"q": query}).encode(),
        headers={"User-Agent": "Mozilla/5.0 (Axiom web search)"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        html = r.read().decode("utf-8", "ignore")
    out: list[dict[str, str]] = []
    # DDG lite markup: <a rel=... href="URL" class='result-link'>TITLE</a>
    # (href precedes class; class uses single quotes). Snippets follow in a
    # <td class='result-snippet'>…</td>. Be quote-agnostic + order-agnostic.
    snippets = re.findall(r"""class=['"]result-snippet['"][^>]*>(.*?)</td>""", html, re.S)
    matches = re.finditer(
        r"""<a[^>]*href=["']([^"']+)["'][^>]*class=['"]result-link['"][^>]*>(.*?)</a>""",
        html, re.S,
    )
    for i, m in enumerate(matches):
        href = m.group(1)
        title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if "uddg=" in href:  # unwrap DDG redirect if present
            href = urllib.parse.unquote(href.split("uddg=", 1)[1].split("&", 1)[0])
        snip = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
        out.append({"title": title, "url": href, "snippet": snip})
        if len(out) >= k:
            break
    return out


def _search_tavily(query: str, k: int, key: str | None) -> list[dict[str, str]]:
    d = _http_json("https://api.tavily.com/search",
                   data={"api_key": key, "query": query, "max_results": k})
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": r.get("content", "")} for r in d.get("results", [])]


def _search_brave(query: str, k: int, key: str | None) -> list[dict[str, str]]:
    url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
        {"q": query, "count": k})
    d = _http_json(url, headers={"X-Subscription-Token": key or "", "Accept": "application/json"})
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": r.get("description", "")} for r in d.get("web", {}).get("results", [])]


def _search_exa(query: str, k: int, key: str | None) -> list[dict[str, str]]:
    d = _http_json("https://api.exa.ai/search",
                   data={"query": query, "numResults": k, "contents": {"text": True}},
                   headers={"x-api-key": key or ""})
    return [{"title": r.get("title", ""), "url": r.get("url", ""),
             "snippet": (r.get("text") or "")[:500]} for r in d.get("results", [])]


def _search_serpapi(query: str, k: int, key: str | None) -> list[dict[str, str]]:
    url = "https://serpapi.com/search.json?" + urllib.parse.urlencode(
        {"q": query, "num": k, "api_key": key or ""})
    d = _http_json(url)
    return [{"title": r.get("title", ""), "url": r.get("link", ""),
             "snippet": r.get("snippet", "")} for r in d.get("organic_results", [])]


_SEARCHERS = {
    "duckduckgo": _search_duckduckgo, "tavily": _search_tavily, "brave": _search_brave,
    "exa": _search_exa, "serpapi": _search_serpapi,
}


def search(query: str, *, k: int = 5, provider: str | None = None) -> dict[str, Any]:
    """Run a web search via the configured provider. EC note: the query leaves
    the enclave to the provider — callers must classify it first."""
    name = resolve_provider(provider)
    needs_key, key_env = _PROVIDERS[name]
    key = _api_key(name, key_env) if needs_key else None
    try:
        results = _SEARCHERS[name](query, k, key)
        return {"ok": True, "provider": name, "query": query, "results": results}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "provider": name, "query": query, "results": [], "error": str(exc)}


def fetch(url: str, *, max_chars: int = 8000) -> dict[str, Any]:
    """Fetch a URL's text (best-effort HTML→text), truncated. EC note: the URL
    leaves the enclave — callers must classify it first."""
    try:
        import re

        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Axiom web fetch)"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            raw = r.read().decode("utf-8", "ignore")
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return {"ok": True, "url": url, "text": text[:max_chars]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "url": url, "text": "", "error": str(exc)}
