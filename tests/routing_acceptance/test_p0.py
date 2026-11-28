# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Routing build plan, Phase 0 acceptance (docs/working/routing-build-plan-2026-11-12.md, section 3).

P0.2, embeddings pass the guard (RS-E2, review finding F2):

- P0.2-A1: with a controlled label, ``embed_texts`` does not call a hosted provider even when its
  key is set. Chat's ``_rag_context`` in a session holding controlled content is the same leak on
  the query side.
- P0.2-A2: with the node's agent policy set to ``none`` or ``assist``, no embedding leaves the
  machine. ``local`` is the permissive default every general install has, so it is not this case.
- P0.2-A3: retrieval falls back to lexical, with a visible note, when no embedder is admitted.

The network is never reached: ``requests.post`` records the URL it was asked for, and the local
model runtime is a recorder.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import axiom.rag.embeddings as emb

_HOSTED = "https://api.openai.com/v1/embeddings"
_REMOTE = "https://embed.example.internal:42000"


class _Resp:
    status_code = 200
    text = ""

    def __init__(self, n: int):
        self._n = n

    def json(self):
        return {"data": [{"index": i, "embedding": [0.5] * 768} for i in range(self._n)]}


@pytest.fixture
def egress(monkeypatch, tmp_path: Path):
    """Record every embedding request; nothing leaves the test.

    Returns ``(posted_urls, local_calls)``. A fresh config directory holds no providers file
    unless a test writes one, and the node file declares nothing unless a test writes one.
    """
    import requests

    posted: list[str] = []
    local: list[list[str]] = []

    def _post(url, *a, json=None, **k):  # noqa: A002 - mirrors requests.post
        posted.append(url)
        return _Resp(len((json or {}).get("input") or [1]))

    monkeypatch.setattr(requests, "post", _post)

    def _ollama(texts):
        local.append(list(texts))
        return None  # the local runtime is not running

    monkeypatch.setattr(emb, "_embed_ollama", _ollama)
    monkeypatch.setattr(emb, "_ollama_reachable", lambda: False)
    monkeypatch.setattr(emb, "_openai_quota_blocked", False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("AXIOM_RAG_EMBED_URL", raising=False)
    monkeypatch.delenv("NEUT_RAG_EMBED_URL", raising=False)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "llm-providers.toml").write_text("[gateway]\n", encoding="utf-8")
    monkeypatch.setenv("AXIOM_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("AXIOM_NODE_CONFIG", str(tmp_path / "node.toml"))
    return posted, local


def _providers(tmp_path: Path, body: str) -> None:
    (tmp_path / "config" / "llm-providers.toml").write_text("[gateway]\n" + body, encoding="utf-8")


def _hosted_calls(posted: list[str]) -> list[str]:
    return [u for u in posted if u.startswith(_HOSTED)]


# ---------------------------------------------------------------------------
# P0.2-A1
# ---------------------------------------------------------------------------


def test_p0_2_a1_a_controlled_label_never_reaches_a_hosted_embedder(egress):
    """P0.2-A1: ``embed_texts`` with a controlled label, OPENAI_API_KEY set: nothing is posted to OpenAI."""
    posted, _local = egress

    out = emb.embed_texts(["HEU inventory at the facility"], routing_tier="export_controlled")

    assert _hosted_calls(posted) == [], f"controlled text was sent to a hosted embedder: {posted}"
    assert out is None  # no admitted embedder here: the caller searches by keyword


def test_p0_2_a1_a_controlled_chat_session_does_not_embed_its_question_on_a_hosted_provider(egress, monkeypatch):
    """P0.2-A1, query side: chat's ``_rag_context`` in a session that holds controlled content."""
    import axiom.rag.retriever as ret
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.scope import ChatScope

    posted, _local = egress
    monkeypatch.setattr(ret, "retrieve", lambda *a, **k: [])

    class _Agent:
        _render = None

        def _get_rag_store(self):
            return object()

    scope = ChatScope()
    scope.session.context["content_tier"] = "export_controlled"

    ChatAgent._rag_context(_Agent(), "what does the logbook say about the core loading", scope=scope)

    assert _hosted_calls(posted) == [], f"a controlled session's question went to a hosted embedder: {posted}"


def test_p0_2_a1_an_endpoint_the_providers_file_clears_is_used_for_controlled_text(egress, monkeypatch, tmp_path):
    """P0.2-A1: clearance is read from the gateway's ``routing_tier`` in llm-providers.toml, not a new variable."""
    posted, _local = egress
    monkeypatch.setenv("AXIOM_RAG_EMBED_URL", _REMOTE)
    _providers(
        tmp_path,
        '[[gateway.providers]]\nname = "site-embed"\nendpoint = "https://embed.example.internal:42000/v1"\n'
        'model = "nomic-embed-text"\nrouting_tier = "export_controlled"\n',
    )

    out = emb.embed_texts(["HEU inventory"], routing_tier="export_controlled")

    assert posted == [f"{_REMOTE}/v1/embeddings"]
    assert out is not None and len(out) == 1


def test_p0_2_a1_an_endpoint_declared_public_is_refused_for_controlled_text(egress, monkeypatch, tmp_path):
    """P0.2-A1: a remote endpoint the providers file declares public never sees controlled text."""
    posted, _local = egress
    monkeypatch.setenv("AXIOM_RAG_EMBED_URL", _REMOTE)
    _providers(
        tmp_path,
        '[[gateway.providers]]\nname = "site-embed"\nendpoint = "https://embed.example.internal:42000/v1"\n'
        'model = "nomic-embed-text"\nrouting_tier = "public"\n',
    )

    assert emb.embed_texts(["HEU inventory"], routing_tier="export_controlled") is None
    assert posted == []


def test_p0_2_a1_public_text_still_embeds_as_before(egress):
    """P0.2-A1, no regression: public text with a key set still embeds on the hosted provider."""
    posted, _local = egress

    out = emb.embed_texts(["the reactor's public fact sheet"])

    assert _hosted_calls(posted) == [_HOSTED]
    assert out is not None


def test_p0_2_a1_a_query_naming_a_controlled_term_is_held_back_with_no_session(egress):
    """P0.2-A1: a surface with no session (MCP, the CLI) is held to the query's own label."""
    posted, _local = egress

    q = emb.embed_query("HEU inventory at the facility")

    assert _hosted_calls(posted) == []
    assert q.vector is None


# ---------------------------------------------------------------------------
# P0.2-A2
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("policy", ["none", "assist"])
def test_p0_2_a2_a_node_that_forbids_model_calls_sends_no_embedding_off_the_machine(egress, monkeypatch, tmp_path, policy):
    """P0.2-A2: the node's agent policy forbids model calls here, so no embedding leaves."""
    posted, local = egress
    monkeypatch.setenv("AXIOM_RAG_EMBED_URL", _REMOTE)
    (tmp_path / "node.toml").write_text(f'[node]\nagents = "{policy}"\n', encoding="utf-8")

    emb.embed_texts(["the reactor's public fact sheet"])
    assert posted == [], f"an embedding left a node whose agent policy is {policy!r}: {posted}"
    emb.embed_query("the reactor's public fact sheet")
    assert posted == [], f"a query embedding left a node whose agent policy is {policy!r}: {posted}"
    assert local, "the machine's own embedder is still asked"


# ---------------------------------------------------------------------------
# P0.2-A3
# ---------------------------------------------------------------------------


def test_p0_2_a3_no_admitted_embedder_means_keyword_retrieval_with_a_note(egress):
    """P0.2-A3: ``embed_query`` says why retrieval is lexical."""
    q = emb.embed_query("core loading", routing_tier="export_controlled")

    assert q.vector is None
    assert q.note, "the fallback to keyword search is silent"
    assert "keyword" in q.note.lower()


def test_p0_2_a3_with_nothing_configured_there_is_no_note(egress, monkeypatch):
    """P0.2-A3, no noise: an install with no embedder at all searches by keyword as it always has."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    q = emb.embed_query("core loading")

    assert q.vector is None
    assert q.note == ""


def test_p0_2_a3_chat_retrieves_by_keyword_and_shows_the_note(egress, monkeypatch):
    """P0.2-A3: chat still retrieves, by keyword, and the person sees why."""
    import axiom.rag.retriever as ret
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.scope import ChatScope

    seen: dict = {}

    def _retrieve(store, query_text, query_embedding=None, **_k):
        seen["embedding"] = query_embedding
        seen["query_text"] = query_text
        return []

    monkeypatch.setattr(ret, "retrieve", _retrieve)
    shown: list[tuple[str, str]] = []

    class _Render:
        def render_message(self, role, content):
            shown.append((role, content))

    class _Agent:
        _render = _Render()

        def _get_rag_store(self):
            return object()

    scope = ChatScope()
    scope.session.context["content_tier"] = "export_controlled"

    ChatAgent._rag_context(_Agent(), "core loading", scope=scope)

    assert seen == {"embedding": None, "query_text": "core loading"}
    assert any("keyword" in text.lower() for _role, text in shown), shown


def test_p0_2_a3_mcp_retrieve_returns_keyword_results_with_the_note(egress, monkeypatch):
    """P0.2-A3: ``axiom_rag__retrieve`` keeps answering and carries the note."""
    import asyncio

    import axiom.rag.retriever as ret
    from axiom.extensions.builtins.mcp import platform_primitives as pp

    class _Store:
        def access_tier_for(self, _path):
            return "public"

    monkeypatch.setattr(pp, "_resolve_rag_store", lambda: _Store())
    seen: dict = {}

    def _retrieve(store, query_text, query_embedding=None, **_k):
        seen["embedding"] = query_embedding
        return []

    monkeypatch.setattr(ret, "retrieve", _retrieve)

    out = asyncio.run(pp._rag_retrieve({"query": "HEU inventory"}))

    assert seen["embedding"] is None
    assert "keyword" in (out.get("note") or "").lower(), out


# ---------------------------------------------------------------------------
# One function: every retrieval path embeds its query through ``embed_query``
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[2] / "src" / "axiom"

#: Every place that embeds a retrieval query. A new one goes here and calls ``embed_query``.
QUERY_PATHS = (
    "extensions/builtins/chat/agent.py",  # chat's _rag_context
    "extensions/builtins/mcp/platform_primitives.py",  # axiom_rag__retrieve
    "extensions/builtins/chat/tools_ext/search_docs.py",  # search_docs
    "rag/cli.py",  # axi rag search, axi rag eval
    "memory/recall.py",  # memory recall
    "extensions/builtins/library/service.py",  # library search
    "extensions/builtins/http/federation_endpoint.py",  # a federated search
    "extensions/builtins/rag/serving.py",  # the /rag mount
)


@pytest.mark.parametrize("rel", QUERY_PATHS)
def test_p0_2_every_retrieval_path_embeds_its_query_through_one_function(rel):
    """P0.2: no query path calls ``embed_texts`` itself; each goes through ``embed_query``."""
    text = (_SRC / rel).read_text(encoding="utf-8")
    assert "embed_query" in text, f"{rel} does not embed its query through embed_query"
    assert not re.search(r"\bembed_texts\b", text), f"{rel} calls embed_texts directly, past the query guard"


# ---------------------------------------------------------------------------
# P0.4, nothing fails silently (RS-B7, #544)
#
# - P0.4-A1: loading providers never writes a configuration file.
# - P0.4-A2: a provider file that cannot be parsed stops with one sentence and one fix; the stub is
#   not selected.
# - P0.4-A3: the reply stamp never names a provider other than the one that answered.
# ---------------------------------------------------------------------------

_NO_UID_PROVIDERS = (
    "# the operator's own comment\n"
    "[gateway]\n\n"
    "[[gateway.providers]]\n"
    'name = "site-model"\n'
    'endpoint = "https://llm.example.org/v1"\n'
    'model = "m-1"\n'
    'api_key_env = ""\n'
)


def _gateway_from(config_dir: Path):
    from axiom.llm.gateway import Gateway

    return Gateway(config_dir=config_dir)


@pytest.fixture
def no_local_server(monkeypatch):
    """Discovery of a local model server is recorded, and finds nothing unless a test says so."""
    from axiom.llm.gateway import Gateway

    probed: list[bool] = []
    monkeypatch.setattr(Gateway, "_discover_local_llm", lambda self: probed.append(True))
    return probed


def test_p0_4_a1_loading_providers_never_writes_the_providers_file(tmp_path, no_local_server):
    """P0.4-A1: a provider with no ``uid`` gets one derived at load; the file is not touched."""
    path = tmp_path / "llm-providers.toml"
    path.write_text(_NO_UID_PROVIDERS, encoding="utf-8")
    before = (path.read_bytes(), path.stat().st_mtime_ns)

    first = _gateway_from(tmp_path)
    second = _gateway_from(tmp_path)

    assert (path.read_bytes(), path.stat().st_mtime_ns) == before, "loading providers rewrote the file"
    [p1], [p2] = first.providers, second.providers
    assert p1.uid and p1.uid == p2.uid, "a derived uid must be the same on every load"


def test_p0_4_a1_a_declared_uid_is_kept(tmp_path, no_local_server):
    """P0.4-A1: a ``uid`` the file declares is the one used."""
    (tmp_path / "llm-providers.toml").write_text(
        _NO_UID_PROVIDERS + 'uid = "declared-uid-1"\n', encoding="utf-8"
    )

    [p] = _gateway_from(tmp_path).providers

    assert p.uid == "declared-uid-1"


def test_p0_4_a2_an_unparseable_providers_file_stops_with_one_sentence_and_one_fix(tmp_path, no_local_server):
    """P0.4-A2: a damaged file is reported in one sentence carrying one fix, and nothing answers."""
    path = tmp_path / "llm-providers.toml"
    path.write_text('[gateway]\n[[gateway.providers]]\nname = "site-model\n', encoding="utf-8")

    gw = _gateway_from(tmp_path)

    problem = gw.config_problem
    assert problem, "a providers file that cannot be parsed loaded silently"
    assert str(path) in problem and problem.count("Fix:") == 1, problem
    assert "\n" not in problem.strip(), problem
    assert not gw.available
    assert no_local_server == [], "a local model server was probed in place of the damaged configuration"


def test_p0_4_a2_a_turn_on_a_damaged_providers_file_returns_the_problem_not_a_stub(tmp_path, no_local_server):
    """P0.4-A2: every call path says the problem; none answers from a stub or a discovered model."""
    from axiom.extensions.builtins.chat.commands.cli import build_setup_hint

    (tmp_path / "llm-providers.toml").write_text("[gateway\n", encoding="utf-8")
    gw = _gateway_from(tmp_path)
    problem = gw.config_problem

    with_tools = gw.complete_with_tools([{"role": "user", "content": "hello"}])
    streamed = "".join(c.text for c in gw.stream_with_tools([{"role": "user", "content": "hello"}]))
    plain = gw.complete("hello")

    assert not with_tools.success and with_tools.text == problem, with_tools
    assert streamed == problem, streamed
    assert not plain.success and plain.text == problem, plain
    assert problem in (build_setup_hint(gw) or ""), "chat start does not say why no model is available"
    assert gw.last_selected is None


def _two_providers(monkeypatch):
    from tests.test_gateway import _gateway_with_providers, _make_provider

    return _gateway_with_providers(
        [
            _make_provider(name="first", endpoint="https://first.example.org/v1", model="m-first", priority=1),
            _make_provider(name="second", endpoint="https://second.example.org/v1", model="m-second", priority=2),
        ],
        monkeypatch,
    )


def _stamp_of(gw):
    from axiom.extensions.builtins.chat import where
    from axiom.infra.orchestrator.session import Session

    class _Agent:
        gateway = gw
        session = Session()

    return where.where_report(_Agent())


def test_p0_4_a3_a_streamed_answer_from_the_fallback_is_stamped_with_the_fallback(monkeypatch):
    """P0.4-A3: the first provider fails before any text; the second answers; the stamp names the second."""
    from axiom.extensions.builtins.chat import where
    from axiom.llm.gateway import StreamChunk, TransientLLMError

    monkeypatch.setattr(where, "_retrieval_where", lambda: where.RetrievalWhere("none", "", ""))
    gw = _two_providers(monkeypatch)

    def _stream(provider, *_a, **_k):
        if provider.name == "first":
            raise TransientLLMError("connection refused", provider="first")
        yield StreamChunk(type="text", text="an answer")

    monkeypatch.setattr(gw, "_stream_provider", _stream)

    text = "".join(c.text for c in gw.stream_with_tools([{"role": "user", "content": "hi"}]))

    assert text == "an answer"
    report = _stamp_of(gw)
    assert (report.provider, report.model, report.basis) == ("second", "m-second", "answered"), report


def test_p0_4_a3_a_plain_completion_from_the_fallback_is_stamped_with_the_fallback(monkeypatch):
    """P0.4-A3: ``complete`` falls back the same way and records the provider that answered."""
    from axiom.llm.gateway import GatewayResponse, TransientLLMError

    gw = _two_providers(monkeypatch)

    def _call(provider, *_a, **_k):
        if provider.name == "first":
            raise TransientLLMError("timed out", provider="first")
        return GatewayResponse(text="ok", provider=provider.name, model=provider.model)

    monkeypatch.setattr(gw, "_call_provider", _call)

    assert gw.complete("hi").provider == "second"
    assert gw.last_selected is not None and gw.last_selected.name == "second"


def test_p0_4_a3_a_turn_nobody_answered_names_no_provider(monkeypatch):
    """P0.4-A3: when the call fails, nothing answered, so the stamp names no provider and no model."""
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.extensions.builtins.chat.providers.null_render import NullRenderProvider
    from axiom.infra.orchestrator.session import Session

    gw = _two_providers(monkeypatch)

    def _fail(*_a, **_k):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(gw, "_call_provider_with_tools", _fail)

    resp = gw.complete_with_tools([{"role": "user", "content": "hi"}])

    assert not resp.success
    assert gw.last_selected is None, "a provider that did not answer is still recorded as the one that did"

    rendered: list[dict] = []

    class _Render(NullRenderProvider):
        def render_status(self, model, tokens_in, tokens_out, cost, tier=None, where=None):
            rendered.append({"model": model, "where": where})

    from types import SimpleNamespace

    agent = ChatAgent.__new__(ChatAgent)
    agent._render = _Render()
    agent.gateway = gw
    agent.session = Session()
    agent.usage = SimpleNamespace(turns=[SimpleNamespace(input_tokens=0, output_tokens=0, cost=None)])
    agent._emit_status(routing_tier="public")

    assert rendered and rendered[0]["model"] == "", f"the stamp named a model that did not answer: {rendered}"


def test_p0_4_a3_a_vpn_refusal_does_not_record_the_fallback_it_looked_at(monkeypatch):
    """P0.4-A3: the VPN path looks for a public fallback; looking is not answering."""
    from axiom.extensions.builtins.settings import store as settings_store
    from tests.test_gateway import _gateway_with_providers, _make_provider

    gw = _gateway_with_providers(
        [
            _make_provider(name="site", requires_vpn=True, routing_tier="export_controlled", priority=1),
            _make_provider(name="cloud", routing_tier="public", priority=2),
        ],
        monkeypatch,
    )
    monkeypatch.setattr(gw, "_check_vpn", lambda _p: False)
    monkeypatch.setattr(settings_store.SettingsStore, "get", lambda self, k, d=None: "warn" if k == "routing.on_vpn_unavailable" else d)

    resp = gw.complete_with_tools([{"role": "user", "content": "hi"}], routing_tier="export_controlled", principal="@tester")

    assert not resp.success
    assert gw.last_selected is None, f"the stamp would name {gw.last_selected.name!r}, which never answered"


# ---------------------------------------------------------------------------
# P0.5, settings do what they say (RS-B8)
#
# - P0.5-A1: every key in the settings defaults has a consumer, or is removed. A static test, both
#   directions: a default nobody reads fails, and a key something reads or shows with no default fails.
# - P0.5-A2: ``routing.on_vpn_unavailable = block`` blocks.
# ---------------------------------------------------------------------------

#: Families read by building the key at run time (``f"state.snapshots.{name}"``). The prefix literal
#: in the consuming module is its consumer; a key under it needs no literal of its own.
PREFIX_CONSUMERS = {
    "state.snapshots.": "infra/state_snapshots.py",
    "vault.backup.": "infra/vault_backup/policy.py",
    "notifications.roles.": "extensions/builtins/notifications/preferences.py",
}


def _defaults() -> set[str]:
    from axiom.extensions.builtins.settings.store import _DEFAULTS

    return set(_DEFAULTS)


#: Where a key is declared or listed rather than read. Each block is cut out of its file before the
#: scan, so a default does not count as its own consumer and a web allowlist entry is not a reader.
_DECLARATIONS = {
    "extensions/builtins/settings/store.py": ("_DEFAULTS:", "_SAFE_ON_DAMAGE:"),
    "extensions/builtins/settings/api.py": ("WEB_READABLE:", "WEB_EDITABLE:", "DESCRIPTIONS:"),
}


def _cut(text: str, start: str) -> str:
    """Remove the assignment that begins at ``start``, through its closing bracket."""
    i = text.index(start)
    depth, j, opened = 0, text.index("=", i), False
    while j < len(text):
        c = text[j]
        if c in "([{":
            depth, opened = depth + 1, True
        elif c in ")]}":
            depth -= 1
            if opened and depth == 0:
                break
        j += 1
    return text[:i] + text[j + 1 :]


def _consumer_sources() -> dict[str, str]:
    out: dict[str, str] = {}
    for f in _SRC.rglob("*.py"):
        if "tests" in f.parts:
            continue
        rel = str(f.relative_to(_SRC))
        text = f.read_text(encoding="utf-8", errors="ignore")
        for start in _DECLARATIONS.get(rel, ()):
            text = _cut(text, start)
        out[rel] = text
    return out


def test_p0_5_a1_every_default_setting_has_a_consumer():
    """P0.5-A1, defaults to consumers: each key is read somewhere other than where it is declared."""
    sources = _consumer_sources()
    for prefix, rel in PREFIX_CONSUMERS.items():
        assert f'"{prefix}' in sources[rel] or f"'{prefix}" in sources[rel], f"{rel} no longer reads {prefix}*"
    unread = sorted(
        k
        for k in _defaults()
        if not any(k.startswith(p) for p in PREFIX_CONSUMERS)
        and not any(f'"{k}"' in text for text in sources.values())
    )
    assert unread == [], f"settings with no consumer (wire them or remove them): {unread}"


def test_p0_5_a1_every_setting_that_is_read_or_shown_has_a_default():
    """P0.5-A1, consumers to defaults: a key read with a literal name, or shown on the web, is declared."""
    from axiom.extensions.builtins.settings import api

    defaults = _defaults()
    namespaces = {k.split(".", 1)[0] for k in defaults}
    read = re.compile(r'\.get\(\s*"((?:%s)\.[a-z0-9_.\-]+)"' % "|".join(sorted(namespaces)))
    undeclared: dict[str, str] = {}
    for rel, text in _consumer_sources().items():
        if "SettingsStore" not in text and "settings" not in text:
            continue
        for m in read.finditer(text):
            key = m.group(1)
            if key not in defaults and not any(key.startswith(p) for p in PREFIX_CONSUMERS):
                undeclared.setdefault(key, rel)
    for key in (*api.WEB_READABLE, *api.WEB_EDITABLE, *api.DESCRIPTIONS):
        if key not in defaults:
            undeclared.setdefault(key, "extensions/builtins/settings/api.py")
    assert undeclared == {}, f"settings read or shown with no default: {undeclared}"


@pytest.mark.parametrize("key", ["routing.default_mode", "routing.cloud_provider"])
def test_p0_5_a1_the_two_routing_keys_with_no_consumer_are_gone(key):
    """P0.5-A1: the plan names these two; nothing declares, shows or reads them (status only displayed one)."""
    from axiom.extensions.builtins.settings import api

    assert key not in _defaults()
    assert key not in (*api.WEB_READABLE, *api.DESCRIPTIONS)
    assert not any(f'"{key}"' in text for text in _consumer_sources().values())


def _vpn_gateway(monkeypatch, policy: str):
    from axiom.extensions.builtins.settings import store as settings_store
    from tests.test_gateway import _gateway_with_providers, _make_provider

    gw = _gateway_with_providers(
        [
            _make_provider(name="site", requires_vpn=True, routing_tier="public", priority=1),
            _make_provider(name="cloud", routing_tier="public", priority=2),
        ],
        monkeypatch,
    )
    monkeypatch.setattr(gw, "_check_vpn", lambda _p: False)
    monkeypatch.setattr(
        settings_store.SettingsStore,
        "get",
        lambda self, k, d=None: policy if k == "routing.on_vpn_unavailable" else d,
    )
    consulted: list[str] = []
    real = gw._select_provider_impl

    def _spy(task, routing_tier="any", required_tags=None, prefer=None):
        chosen = real(task, routing_tier, required_tags, prefer)
        consulted.append(getattr(chosen, "name", ""))
        return chosen

    monkeypatch.setattr(gw, "_select_provider_impl", _spy)
    sent: list[str] = []
    monkeypatch.setattr(gw, "_call_provider_with_tools", lambda p, *a, **k: sent.append(p.name))
    return gw, consulted, sent


def test_p0_5_a2_on_vpn_unavailable_block_blocks(monkeypatch, capsys):
    """P0.5-A2: ``block`` refuses the turn, consults no other provider, and says which setting did it."""
    gw, consulted, sent = _vpn_gateway(monkeypatch, "block")

    resp = gw.complete_with_tools([{"role": "user", "content": "hi"}])

    assert not resp.success and resp.error == "VPN_BLOCKED", resp
    assert sent == []
    assert consulted == ["site"], f"block looked for a fallback provider: {consulted}"
    assert "routing.on_vpn_unavailable" in resp.text


def test_p0_5_a2_the_legacy_value_fail_still_blocks(monkeypatch):
    """P0.5-A2: ``fail``, the value the code knew before, reads as ``block``."""
    gw, consulted, sent = _vpn_gateway(monkeypatch, "fail")

    resp = gw.complete_with_tools([{"role": "user", "content": "hi"}])

    assert resp.error == "VPN_BLOCKED" and sent == [] and consulted == ["site"]


def test_p0_5_a2_the_streamed_path_blocks_the_same_way(monkeypatch):
    """P0.5-A2: a streamed turn on a VPN provider that cannot be reached is refused under ``block``."""
    gw, consulted, _sent = _vpn_gateway(monkeypatch, "block")
    gw.providers = [p for p in gw.providers if p.name == "site"]

    text = "".join(c.text for c in gw.stream_with_tools([{"role": "user", "content": "hi"}]))

    assert "routing.on_vpn_unavailable" in text
    assert consulted == ["site"]


# ---------------------------------------------------------------------------
# P0.2-D4 (owner's decision 4): text-only controlled chunks catch up later
# ---------------------------------------------------------------------------


@pytest.fixture
def pg_store(real_postgres):
    import uuid

    from axiom.rag.store import RAGStore

    store = RAGStore(real_postgres.replace("postgresql+psycopg2://", "postgresql://", 1))
    store.connect()
    corpus = f"test-await-{uuid.uuid4().hex[:8]}"
    yield store, corpus
    store.delete_corpus(corpus)
    store.close()


def _controlled_text_only(store, corpus: str, path: str = "controlled.md") -> None:
    from axiom.rag.chunker import Chunk

    chunks = [
        Chunk(text=f"controlled passage {i}", source_path=path, source_title="C", chunk_index=i, start_line=1)
        for i in range(3)
    ]
    store.upsert_chunks(chunks, None, corpus=corpus, access_tier="export_controlled")


def test_p0_2_d4_chunks_without_a_vector_are_counted_per_corpus(pg_store):
    """D4: the store counts the chunks still waiting for a vector, per corpus, through its own API."""
    store, corpus = pg_store
    _controlled_text_only(store, corpus)

    assert store.vectorless_chunk_counts([corpus]) == {corpus: 3}


def test_p0_2_d4_doctor_lists_the_chunks_awaiting_an_admitted_embedder(pg_store):
    """D4: doctor shows "N chunks awaiting an admitted embedder" for the corpus."""
    from axiom.rag.ingest import embedding_doctor_rows

    store, corpus = pg_store
    _controlled_text_only(store, corpus)

    rows = [r for r in embedding_doctor_rows(store) if corpus in r["name"]]

    assert rows and "3 chunks awaiting an admitted embedder" in rows[0]["status"], rows


def test_p0_2_d4_catch_up_is_a_no_op_while_no_embedder_is_admitted(pg_store, egress):
    """D4: with only a hosted embedder configured, controlled chunks stay text-only and nothing is sent."""
    from axiom.rag.ingest import embed_awaiting

    store, corpus = pg_store
    posted, _local = egress
    _controlled_text_only(store, corpus)

    out = embed_awaiting(store, corpus)

    assert out == {"embedded": 0, "waiting": 3}
    assert posted == []
    assert store.vectorless_chunk_counts([corpus]) == {corpus: 3}


def test_p0_2_d4_catch_up_embeds_once_a_cleared_embedder_exists(pg_store, egress, monkeypatch, tmp_path):
    """D4: once an embedder cleared for the label exists, the stored chunks are embedded, not re-extracted."""
    from axiom.rag.ingest import embed_awaiting

    store, corpus = pg_store
    posted, _local = egress
    _controlled_text_only(store, corpus)
    monkeypatch.setenv("AXIOM_RAG_EMBED_URL", _REMOTE)
    _providers(
        tmp_path,
        '[[gateway.providers]]\nname = "site-embed"\nendpoint = "https://embed.example.internal:42000/v1"\n'
        'model = "nomic-embed-text"\nrouting_tier = "export_controlled"\n',
    )

    out = embed_awaiting(store, corpus)

    assert out == {"embedded": 3, "waiting": 0}
    assert posted and all(u.startswith(_REMOTE) for u in posted)
    assert store.vectorless_chunk_counts([corpus]) == {}


def test_p0_2_d4_data_reindex_runs_the_catch_up_without_a_source(pg_store, egress, real_postgres):
    """D4: ``axi data reindex --awaiting --corpus C`` is the catch-up, and needs no bronze source."""
    from axiom.extensions.builtins.data_platform.skills import reindex

    store, corpus = pg_store
    _controlled_text_only(store, corpus)

    dsn = real_postgres.replace("postgresql+psycopg2://", "postgresql://", 1)
    res = reindex.run({"awaiting": True, "corpus": corpus, "dsn": dsn}, None)

    assert res.ok, res.errors
    assert res.value == {"embedded": 0, "waiting": 3}


# ---------------------------------------------------------------------------
# P0.6, usage is right (review finding F10)
#
# - P0.6-A1: cached input tokens are read from the field the Anthropic format sends,
#   ``usage.cache_read_input_tokens`` (Messages API; the same name in ``message_start`` and the
#   cumulative ``message_delta`` usage of a stream).
# - P0.6-A2: a streamed OpenAI-format turn requests usage (``stream_options.include_usage``) and
#   records it from the last chunk, whose ``choices`` is empty. Cached prompt tokens are
#   ``usage.prompt_tokens_details.cached_tokens``.
# - P0.6-A3: an unpriced model is recorded as unpriced, never zero.
#
# Field names checked on 2026-11-18 against the Anthropic Messages API reference and the OpenAI
# Python SDK's ``ChatCompletionStreamOptionsParam`` and ``CompletionUsage`` types.
# ---------------------------------------------------------------------------

import json as _json  # noqa: E402 - kept beside the rows that use it


class _Sse:
    """A streamed HTTP response: one ``data:`` line per event."""

    encoding = "utf-8"
    status_code = 200

    def __init__(self, events: list, done: bool = True, body: dict | None = None):
        self._events, self._done, self._body = events, done, body or {}

    def json(self):
        return self._body

    def iter_lines(self, decode_unicode=True):
        for e in self._events:
            yield "data: " + _json.dumps(e)
        if self._done:
            yield "data: [DONE]"


def _usage_gateway(monkeypatch, *, endpoint: str, model: str, api_key_env: str = "FAKE_KEY", **kw):
    from tests.test_gateway import _gateway_with_providers, _make_provider

    return _gateway_with_providers(
        [_make_provider(name="p", endpoint=endpoint, model=model, api_key_env=api_key_env, **kw)], monkeypatch
    )


def _record_posts(monkeypatch, reply):
    from axiom.llm import gateway as gwm

    sent: list[dict] = []

    def _post(_requests, url, payload, headers, timeout=60, **kw):
        sent.append(payload)
        return reply(payload)

    monkeypatch.setattr(gwm, "_post_with_rate_limit_retry", _post)
    return sent


def _usage_of(chunks) -> tuple[int, int, int]:
    """Summed the way chat sums a stream's usage chunks: (input, output, cached)."""
    us = [c for c in chunks if c.type == "usage"]
    return (sum(c.input_tokens for c in us), sum(c.output_tokens for c in us), sum(c.cache_read_tokens for c in us))


_ANTHROPIC_USAGE = {"input_tokens": 12, "output_tokens": 7, "cache_read_input_tokens": 900, "cache_creation_input_tokens": 40}


def test_p0_6_a1_anthropic_cached_input_tokens_are_read(monkeypatch):
    """P0.6-A1: a Messages API reply's ``cache_read_input_tokens`` reaches ``cache_read_tokens``."""
    gw = _usage_gateway(monkeypatch, endpoint="https://api.anthropic.com/v1", model="claude-test")
    _record_posts(
        monkeypatch,
        lambda _p: _Sse([], body={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn", "usage": _ANTHROPIC_USAGE}),
    )

    resp = gw._call_anthropic_with_tools(gw.providers[0], [{"role": "user", "content": "hi"}], "", None, 100)

    assert (resp.input_tokens, resp.output_tokens, resp.cache_read_tokens) == (12, 7, 900)


def test_p0_6_a1_a_streamed_anthropic_turn_counts_cached_tokens_once(monkeypatch):
    """P0.6-A1, streamed: ``message_start`` carries the input side and ``message_delta`` is cumulative."""
    gw = _usage_gateway(monkeypatch, endpoint="https://api.anthropic.com/v1", model="claude-test")
    start = {k: v for k, v in _ANTHROPIC_USAGE.items() if k != "output_tokens"} | {"output_tokens": 1}
    events = [
        {"type": "message_start", "message": {"usage": start}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": _ANTHROPIC_USAGE},
        {"type": "message_stop"},
    ]
    _record_posts(monkeypatch, lambda _p: _Sse(events, done=False))

    chunks = list(gw._stream_anthropic(gw.providers[0], [{"role": "user", "content": "hi"}], "", None, 100))

    assert _usage_of(chunks) == (12, 7, 900)


def _openai_stream_events(with_usage: bool) -> list:
    events = [
        {"choices": [{"index": 0, "delta": {"content": "an answer"}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
    ]
    if with_usage:
        events.append(
            {"choices": [], "usage": {"prompt_tokens": 300, "completion_tokens": 20, "total_tokens": 320,
                                      "prompt_tokens_details": {"cached_tokens": 256}}}
        )
    return events


def test_p0_6_a2_a_streamed_openai_turn_requests_usage(monkeypatch):
    """P0.6-A2: the streamed request body asks for usage."""
    gw = _usage_gateway(monkeypatch, endpoint="https://api.openai.com/v1", model="gpt-test")
    sent = _record_posts(monkeypatch, lambda p: _Sse(_openai_stream_events(True)))

    list(gw._stream_openai(gw.providers[0], [{"role": "user", "content": "hi"}], "", None, 100))

    assert sent[0].get("stream_options") == {"include_usage": True}, sent[0]


def test_p0_6_a2_a_streamed_openai_turn_records_the_usage_of_its_last_chunk(monkeypatch):
    """P0.6-A2: the usage chunk after ``finish_reason`` (``choices`` empty) is read, cached tokens too."""
    gw = _usage_gateway(monkeypatch, endpoint="https://api.openai.com/v1", model="gpt-test")
    _record_posts(monkeypatch, lambda p: _Sse(_openai_stream_events(True)))

    chunks = list(gw._stream_openai(gw.providers[0], [{"role": "user", "content": "hi"}], "", None, 100))

    assert "".join(c.text for c in chunks if c.type == "text") == "an answer"
    assert _usage_of(chunks) == (300, 20, 256)


def test_p0_6_a2_a_server_that_refuses_stream_options_is_asked_again_without_them(monkeypatch):
    """P0.6-A2, no regression: an OpenAI-compatible server that rejects the field still answers."""
    import requests

    gw = _usage_gateway(monkeypatch, endpoint="http://llm.example.org:8000/v1", model="local-test")

    def _reply(payload):
        if "stream_options" in payload:
            r = requests.Response()
            r.status_code = 400
            r._content = b'{"error": "Unrecognized request argument supplied: stream_options"}'
            raise requests.HTTPError("400 Client Error", response=r)
        return _Sse(_openai_stream_events(False))

    sent = _record_posts(monkeypatch, _reply)

    chunks = list(gw._stream_openai(gw.providers[0], [{"role": "user", "content": "hi"}], "", None, 100))

    assert len(sent) == 2 and "stream_options" not in sent[1]
    assert "".join(c.text for c in chunks if c.type == "text") == "an answer"


def test_p0_6_a2_a_non_streamed_openai_reply_records_cached_tokens(monkeypatch):
    """P0.6-A2: the non-streamed OpenAI format reads ``prompt_tokens_details.cached_tokens`` too."""
    gw = _usage_gateway(monkeypatch, endpoint="https://api.openai.com/v1", model="gpt-test")
    body = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 300, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 256}},
    }
    _record_posts(monkeypatch, lambda p: _Sse([], body=body))

    resp = gw._call_openai_with_tools(gw.providers[0], [{"role": "user", "content": "hi"}], "", None, 100)

    assert (resp.input_tokens, resp.output_tokens, resp.cache_read_tokens) == (300, 20, 256)


def test_p0_6_a3_a_streamed_turn_on_an_unpriced_model_is_recorded_unpriced(monkeypatch, tmp_path):
    """P0.6-A3: the turn is priced by the provider that answered, not the first in the list.

    The first usable provider is local, so free. The turn is answered by a remote provider nobody
    priced. Pricing it as the local one recorded it at $0.00.
    """
    from unittest.mock import MagicMock

    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.infra.bus import EventBus
    from axiom.infra.orchestrator.session import Session
    from tests.test_gateway import _gateway_with_providers, _make_provider

    gw = _gateway_with_providers(
        [
            _make_provider(name="home", endpoint="http://localhost:8080/v1", model="home-model", api_key_env="", priority=1),
            _make_provider(name="site", endpoint="https://llm.example.org/v1", model="site-model-unpriced", priority=2),
        ],
        monkeypatch,
    )
    gw.set_provider_override("site")
    _record_posts(monkeypatch, lambda p: _Sse(_openai_stream_events(True)))

    agent = ChatAgent(gateway=gw, bus=EventBus(log_path=tmp_path / "e.jsonl"), session=Session())
    agent._rag_init_attempted = True
    agent._rag_store = None
    render = MagicMock()
    render.stream_text.side_effect = lambda chunks: "".join(c.text for c in chunks if c.type == "text")
    agent.set_render_provider(render)

    agent.turn("what is the value?", stream=True)

    last = agent.usage.turns[-1]
    assert (last.provider, last.model) == ("site", "site-model-unpriced"), last
    assert last.cost is None, f"an unpriced model was recorded at {last.cost!r}"
    assert (last.input_tokens, last.output_tokens) == (300, 20)


# ---------------------------------------------------------------------------
# P0.7-A1 (#1710 item 2): the controlled-audit warning and the behaviour agree
#
# With export-controlled providers configured and AXIOM_AUDIT_HMAC_KEY unset, the gateway logged
# "EC requests will be blocked", and nothing blocked them: ``_ec_audit_enabled`` had no reader. The
# behaviour is that controlled requests are served and the audit log stays in standard mode, which
# writes no routing record. The warning now says that, and names the fix.
# ---------------------------------------------------------------------------


@pytest.fixture
def no_audit_key(monkeypatch):
    import axiom.infra.audit_log as audit_log

    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    monkeypatch.setattr(audit_log, "_instance", None)
    yield audit_log
    audit_log._instance = None


def test_p0_7_a1_the_audit_key_warning_says_what_happens(tmp_path, monkeypatch, caplog, no_audit_key):
    """P0.7-A1: the warning no longer claims a block; it says requests are served unaudited, and the fix."""
    from axiom.llm.gateway import Gateway

    monkeypatch.setattr(Gateway, "_discover_local_llm", lambda self: None)
    (tmp_path / "llm-providers.toml").write_text(
        '[gateway]\n[[gateway.providers]]\nname = "site-ec"\nuid = "u-ec"\nendpoint = "https://ec.example.org/v1"\n'
        'model = "m"\napi_key_env = ""\nrouting_tier = "export_controlled"\n',
        encoding="utf-8",
    )

    with caplog.at_level("WARNING"):
        Gateway(config_dir=tmp_path)

    [warning] = [r.getMessage() for r in caplog.records if "AXIOM_AUDIT_HMAC_KEY" in r.getMessage()]
    assert "blocked" not in warning.lower(), warning
    assert "served" in warning and "no audit record" in warning, warning
    assert "setup audit-key" in warning


def test_p0_7_a1_a_controlled_request_is_served_as_the_warning_says(tmp_path, monkeypatch, no_audit_key):
    """P0.7-A1: the behaviour the warning describes: the request is served and no routing record is written."""
    from axiom.llm.gateway import CompletionResponse, Gateway

    monkeypatch.setattr(Gateway, "_discover_local_llm", lambda self: None)
    (tmp_path / "llm-providers.toml").write_text(
        '[gateway]\n[[gateway.providers]]\nname = "site-ec"\nuid = "u-ec"\nendpoint = "https://ec.example.org/v1"\n'
        'model = "m"\napi_key_env = ""\nrouting_tier = "export_controlled"\n',
        encoding="utf-8",
    )
    gw = Gateway(config_dir=tmp_path)
    monkeypatch.setattr(
        gw, "_call_provider_with_tools", lambda p, *a, **k: CompletionResponse(text="ok", provider=p.name, model=p.model)
    )

    resp = gw.complete_with_tools(
        [{"role": "user", "content": "core loading"}], routing_tier="export_controlled", principal="@tester"
    )

    assert resp.success and resp.text == "ok"
    assert no_audit_key.AuditLog.get().mode == "standard"
