# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Tests for in-enclave web search + server-side resolve loop."""
from __future__ import annotations

from types import SimpleNamespace

from axiom.web import resolve, search, tools

# --- provider selection ------------------------------------------------------


def test_resolve_provider_default_is_duckduckgo(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    assert search.resolve_provider() in ("duckduckgo",)  # no settings -> default


def test_resolve_provider_falls_back_when_keyed_has_no_key(monkeypatch):
    # Hermetic: mock key resolution to "no key" (independent of env AND vault).
    monkeypatch.setattr(search, "_api_key", lambda provider, key_env: None)
    assert search.resolve_provider("tavily") == "duckduckgo"


def test_resolve_provider_honors_keyed_when_key_present(monkeypatch):
    monkeypatch.setattr(search, "_api_key", lambda provider, key_env: "tvly-x")
    assert search.resolve_provider("tavily") == "tavily"


def test_available_providers_covers_all():
    assert set(search.available_providers()) == {"duckduckgo", "tavily", "brave", "exa", "serpapi"}


# --- EC query guard ----------------------------------------------------------


class _ECRouter:
    def classify(self, text, **kw):
        from axiom.llm.router import RoutingDecision, RoutingTier
        tier = RoutingTier.EXPORT_CONTROLLED if "ITAR" in text else RoutingTier.PUBLIC
        return RoutingDecision(tier=tier, reason="t", classifier="keyword",
                               matched_terms=["ITAR"] if tier == RoutingTier.EXPORT_CONTROLLED else [])


def test_web_search_withholds_ec_query(monkeypatch):
    monkeypatch.setattr(search, "search", lambda *a, **k: {"ok": True, "results": []})
    r = tools.execute_web_tool("web_search", {"query": "ITAR controlled missile specs"}, router=_ECRouter())
    assert r["ok"] is False and "withheld" in r["error"]


def test_web_search_allows_public_query(monkeypatch):
    monkeypatch.setattr(search, "search", lambda q, **k: {"ok": True, "provider": "duckduckgo", "results": [{"title": "x", "url": "u", "snippet": "s"}]})
    r = tools.execute_web_tool("web_search", {"query": "python asyncio docs"}, router=_ECRouter())
    assert r["ok"] is True and r["results"]


def test_classifier_outage_blocks_egress(monkeypatch):
    class _Broken:
        def classify(self, text, **kw):
            raise RuntimeError("down")
    r = tools.execute_web_tool("web_search", {"query": "anything"}, router=_Broken())
    assert r["ok"] is False and "withheld" in r["error"]  # fail-closed


# --- non-streaming resolve loop ---------------------------------------------


def _TU(tid, name, inp):
    return SimpleNamespace(tool_id=tid, name=name, input=inp)


class _FakeGateway:
    """Returns a web_search tool_use on turn 1, then final text on turn 2."""
    def __init__(self):
        self.turn = 0

    def complete_with_tools(self, messages, **kw):
        self.turn += 1
        if self.turn == 1:
            return SimpleNamespace(text="", tool_use=[_TU("c1", "web_search", {"query": "x"})],
                                   stop_reason="tool_use", input_tokens=1, output_tokens=1)
        return SimpleNamespace(text="Here is the answer", tool_use=[],
                               stop_reason="stop", input_tokens=1, output_tokens=2)


def test_complete_with_web_tools_resolves_then_returns_text(monkeypatch):
    monkeypatch.setattr(tools, "execute_web_tool", lambda n, a, router=None: {"ok": True, "results": [{"title": "t"}]})
    gw = _FakeGateway()
    kw = {"messages": [{"role": "user", "content": "search the web"}], "system": "", "tools": None, "max_tokens": 100}
    resp = resolve.complete_with_web_tools(gw, kw, routing_tier="any", prefer=None, router=None, routing_decision=None)
    assert resp.text == "Here is the answer"
    assert gw.turn == 2  # looped once to resolve the web call


def test_complete_with_web_tools_passes_client_tool_through(monkeypatch):
    class _ClientToolGateway:
        def complete_with_tools(self, messages, **kw):
            return SimpleNamespace(text="", tool_use=[_TU("b1", "Bash", {"command": "ls"})],
                                   stop_reason="tool_use", input_tokens=1, output_tokens=1)
    resp = resolve.complete_with_web_tools(_ClientToolGateway(), {"messages": [], "system": "", "tools": None, "max_tokens": 10},
                                           routing_tier="any", prefer=None, router=None, routing_decision=None)
    assert resp.tool_use[0].name == "Bash"  # client tool handed back, not executed


# --- streaming resolve loop --------------------------------------------------


def _chunk(**kw):
    base = {"type": "", "text": "", "tool_name": "", "tool_id": "", "tool_input_json": ""}
    base.update(kw)
    return SimpleNamespace(**base)


def test_resolve_stream_suppresses_web_tool_and_stitches(monkeypatch):
    monkeypatch.setattr(tools, "execute_web_tool", lambda n, a, router=None: {"ok": True, "results": []})

    class _StreamGW:
        def __init__(self):
            self.turn = 0
        def stream_with_tools(self, messages, **kw):
            self.turn += 1
            if self.turn == 1:
                yield _chunk(type="tool_use_start", tool_name="web_search", tool_id="c1")
                yield _chunk(type="tool_input_delta", tool_id="c1", tool_input_json='{"query":"x"}')
                yield _chunk(type="tool_use_end", tool_id="c1", tool_name="web_search")
                yield _chunk(type="done")
            else:
                yield _chunk(type="text", text="final answer")
                yield _chunk(type="done")

    out = list(resolve.resolve_stream(_StreamGW(), {"messages": [], "system": "", "tools": None, "max_tokens": 10},
                                      routing_tier="any", prefer=None, router=None))
    types = [c.type for c in out]
    # web tool chunks suppressed; final text + exactly one done
    assert "tool_use_start" not in types
    assert "text" in types
    assert types.count("done") == 1


# --- DuckDuckGo parser (fixture; no network) ---------------------------------


def test_ddg_parser_extracts_results(monkeypatch):
    import urllib.request

    from axiom.web import search as s

    html = (
        "<html><body>"
        "<a rel=\"nofollow\" href=\"https://example.com/a\" class='result-link'>Title A</a>"
        "<td class='result-snippet'>Snippet A here</td>"
        "<a rel=\"nofollow\" href=\"https://example.com/b\" class='result-link'>Title B</a>"
        "<td class='result-snippet'>Snippet B here</td>"
        "</body></html>"
    )

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return html.encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _Resp())
    out = s._search_duckduckgo("q", 5, None)
    assert [r["url"] for r in out] == ["https://example.com/a", "https://example.com/b"]
    assert out[0]["title"] == "Title A"
    assert out[0]["snippet"] == "Snippet A here"


def test_web_tool_calls_never_leak_on_hop_exhaustion(monkeypatch):
    # A model that ALWAYS calls web_search (never converges) must not leak a
    # web tool_use to the client after the hop budget is exhausted.
    monkeypatch.setattr(tools, "execute_web_tool", lambda n, a, router=None: {"ok": True, "results": []})

    class _LoopGateway:
        def complete_with_tools(self, messages, **kw):
            return SimpleNamespace(text="", tool_use=[_TU("c", "web_search", {"query": "x"})],
                                   stop_reason="tool_use", input_tokens=1, output_tokens=1)

    resp = resolve.complete_with_web_tools(_LoopGateway(), {"messages": [], "system": "", "tools": None, "max_tokens": 10},
                                           routing_tier="any", prefer=None, router=None, routing_decision=None)
    assert all(not tools.is_web_tool(t.name) for t in (resp.tool_use or []))  # no web leak


def test_final_turn_strips_web_keeps_client_tool(monkeypatch):
    monkeypatch.setattr(tools, "execute_web_tool", lambda n, a, router=None: {"ok": True, "results": []})

    class _MixedGateway:
        # returns a web call AND a client (Bash) call together
        def complete_with_tools(self, messages, **kw):
            return SimpleNamespace(
                text="", tool_use=[_TU("w", "web_search", {"query": "x"}), _TU("b", "Bash", {"command": "ls"})],
                stop_reason="tool_use", input_tokens=1, output_tokens=1)

    resp = resolve.complete_with_web_tools(_MixedGateway(), {"messages": [], "system": "", "tools": None, "max_tokens": 10},
                                           routing_tier="any", prefer=None, router=None, routing_decision=None)
    names = [t.name for t in resp.tool_use]
    assert "Bash" in names and "web_search" not in names  # client tool kept, web stripped


# --- per-provider request shape + response parse (mocked HTTP, no key/network) ---


class _CapturingHTTP:
    """Captures the urllib request and returns a canned JSON body."""
    def __init__(self, payload):
        self.payload = payload
        self.req = None

    def __call__(self, req, timeout=None):
        self.req = req
        import io
        return _CtxResp(io.BytesIO(__import__("json").dumps(self.payload).encode()))


class _CtxResp:
    def __init__(self, buf): self.buf = buf
    def __enter__(self): return self.buf
    def __exit__(self, *a): return False


def _install_http(monkeypatch, payload):
    import urllib.request
    cap = _CapturingHTTP(payload)
    monkeypatch.setattr(urllib.request, "urlopen", cap)
    return cap


def test_tavily_request_and_parse(monkeypatch):
    from axiom.web import search as s
    cap = _install_http(monkeypatch, {"results": [{"title": "T", "url": "https://x", "content": "snip"}]})
    out = s._search_tavily("q", 3, "tvly-key")
    body = cap.req.data.decode()
    assert cap.req.full_url == "https://api.tavily.com/search"
    assert '"api_key": "tvly-key"' in body and '"query": "q"' in body
    assert out == [{"title": "T", "url": "https://x", "snippet": "snip"}]


def test_brave_request_and_parse(monkeypatch):
    from axiom.web import search as s
    cap = _install_http(monkeypatch, {"web": {"results": [{"title": "B", "url": "https://y", "description": "d"}]}})
    out = s._search_brave("q", 3, "brave-key")
    assert cap.req.full_url.startswith("https://api.search.brave.com/res/v1/web/search?")
    assert cap.req.headers.get("X-subscription-token") == "brave-key"  # urllib title-cases header keys
    assert out == [{"title": "B", "url": "https://y", "snippet": "d"}]


def test_exa_request_and_parse(monkeypatch):
    from axiom.web import search as s
    cap = _install_http(monkeypatch, {"results": [{"title": "E", "url": "https://z", "text": "body text"}]})
    out = s._search_exa("q", 3, "exa-key")
    assert cap.req.full_url == "https://api.exa.ai/search"
    assert cap.req.headers.get("X-api-key") == "exa-key"
    assert out[0]["title"] == "E" and out[0]["url"] == "https://z" and out[0]["snippet"] == "body text"


def test_serpapi_request_and_parse(monkeypatch):
    from axiom.web import search as s
    cap = _install_http(monkeypatch, {"organic_results": [{"title": "S", "link": "https://w", "snippet": "sn"}]})
    out = s._search_serpapi("q", 3, "serp-key")
    assert cap.req.full_url.startswith("https://serpapi.com/search.json?")
    assert "api_key=serp-key" in cap.req.full_url
    assert out == [{"title": "S", "url": "https://w", "snippet": "sn"}]
