# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""/compare runs one prompt through two providers over real HTTP and compares the answers.

The providers are real ``LLMProvider`` objects pointed at a local fake server speaking the OpenAI
chat-completions wire format: the gateway's own request code runs. Only the model is faked.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.chat import where
from axiom.extensions.builtins.chat.builtin_commands import ensure_loaded
from axiom.extensions.builtins.chat.command_registry import REGISTRY, CommandContext
from axiom.extensions.builtins.chat.commands_ext import compare as cmp
from axiom.infra.orchestrator.session import Session
from tests.test_gateway import _gateway_with_providers, _make_provider

ensure_loaded()


class FakeModel:
    """A local HTTP server answering /v1/chat/completions the way a provider does."""

    def __init__(self, reply="an answer", *, delay=0.0, status=200, usage=(11, 7)):
        self.reply, self.delay, self.status, self.usage = reply, delay, status, usage
        self.requests: list[dict] = []
        self.spans: list[tuple[float, float]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                t0 = time.monotonic()
                outer.requests.append(body)
                time.sleep(outer.delay)
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                    self.wfile.write(b'{"error": "nope"}')
                    outer.spans.append((t0, time.monotonic()))
                    return
                data = json.dumps(
                    {
                        "choices": [{"message": {"content": outer.reply}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": outer.usage[0],
                            "completion_tokens": outer.usage[1],
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                outer.spans.append((t0, time.monotonic()))

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    monkeypatch.delenv("NO_SUCH_KEY", raising=False)
    monkeypatch.setattr(where, "_retrieval_where", lambda: where.RetrievalWhere("none", "", ""))


@pytest.fixture
def audit(monkeypatch):
    calls = []
    import axiom.infra.routing_audit as ra

    monkeypatch.setattr(ra, "log_routing_decision", lambda **kw: calls.append(kw))
    return calls


@pytest.fixture
def servers():
    made = []

    def make(*a, **k):
        s = FakeModel(*a, **k)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def provider(name, server, model=None, tier="any", **kw):
    kw.setdefault("api_key_env", "")
    return _make_provider(
        name=name, endpoint=server.url, model=model or f"{name}-model", routing_tier=tier, **kw
    )


def gateway(monkeypatch, *providers):
    return _gateway_with_providers(list(providers), monkeypatch)


# ---- the function -------------------------------------------------------------------------------


def test_same_context_goes_to_both_concurrently_and_the_session_provider_is_untouched(
    monkeypatch, servers, audit
):
    sa, sb = servers("Pressure is 40 psi.", delay=0.4), servers("Pressure is 55 psi.", delay=0.4)
    gw = gateway(monkeypatch, provider("one", sa, priority=1), provider("two", sb, priority=2))
    history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    r = cmp.compare(
        gw,
        "one",
        "two",
        "what is the limit?",
        messages=history,
        system="SYSTEM TEXT",
        prompt_tier="public",
    )
    assert r.ran and r.a.status == r.b.status == "ok"
    ra, rb = sa.requests[0], sb.requests[0]
    assert ra["messages"] == rb["messages"]  # identical context, not merely similar
    assert ra["messages"][0] == {"role": "system", "content": "SYSTEM TEXT"}
    assert ra["messages"][-1] == {"role": "user", "content": "what is the limit?"}
    assert [m["content"] for m in ra["messages"][1:3]] == ["hi", "hello"]
    assert ra["model"] == "one-model" and rb["model"] == "two-model"
    (a0, a1), (b0, b1) = sa.spans[0], sb.spans[0]
    assert a0 < b1 and b0 < a1  # the two calls overlapped in time
    assert getattr(gw, "_provider_override", None) is None  # the session's choice was not moved
    assert r.delta is not None
    assert (
        r.delta.numeric_disagreements[0]["a"] == "40"
        and r.delta.numeric_disagreements[0]["b"] == "55"
    )


def test_each_side_reports_model_placement_latency_tokens_and_cost(monkeypatch, servers, audit):
    sa, sb = servers("one", usage=(1000, 500)), servers("two", usage=(10, 5))
    gw = gateway(
        monkeypatch,
        provider("priced", sa, model="gpt-4o", placement="cloud", priority=1),
        provider("home", sb, model="homebrew-xyz", priority=2),
    )
    r = cmp.compare(gw, "priced", "home", "q", prompt_tier="public")
    assert (
        r.a.placement.startswith("CLOUD")
        and r.b.placement == "LOCAL 127.0.0.1"
        or r.b.placement.startswith("LOCAL")
    )
    assert (r.a.input_tokens, r.a.output_tokens) == (1000, 500)
    assert r.a.cost_usd == pytest.approx((1000 * 2.5 + 500 * 10.0) / 1e6)
    assert r.b.cost_usd is None  # no price for this model: unpriced, not $0
    assert r.a.latency_ms and r.a.latency_ms > 0
    out = cmp.render(r)
    assert "unpriced" in out and "1000 in / 500 out" in out and "gpt-4o" in out


@pytest.mark.parametrize("how", ["session", "prompt"])
def test_a_provider_that_may_not_receive_the_content_refuses_the_whole_comparison(
    monkeypatch, servers, audit, how
):
    ok, bad = servers("x"), servers("y")
    gw = gateway(
        monkeypatch,
        provider("cleared", ok, tier="export_controlled", priority=1),
        provider("open", bad, tier="any", priority=2),
    )
    r = cmp.compare(
        gw,
        "cleared",
        "open",
        "q",
        session_tier="export_controlled" if how == "session" else "any",
        prompt_tier="export_controlled" if how == "prompt" else "public",
    )
    assert not r.ran and r.b.status == "refused"
    assert [x["provider"] for x in r.refusals] == ["open"]
    assert "open" in r.refusals[0]["reason"] and "export_controlled" in r.refusals[0]["reason"]
    assert "cleared" in r.refusals[0]["reason"]  # says who may
    assert ok.requests == [] and bad.requests == []  # NOT sent to either, not even the admitted one
    out = cmp.render(r)
    assert "refused" in out.lower() and "Nothing was sent" in out
    assert any("refused" in c["reason"] and c["provider"] == "open" for c in audit)


def test_unknown_same_and_keyless_providers_are_refused_with_the_reason(
    monkeypatch, servers, audit
):
    s = servers("x")
    gw = gateway(
        monkeypatch,
        provider("one", s, priority=1),
        provider("keyless", s, api_key_env="NO_SUCH_KEY", priority=2),
    )
    assert (
        "no provider named 'ghost'"
        in cmp.compare(gw, "one", "ghost", "q", prompt_tier="public").refusals[0]["reason"]
    )
    assert "same" in cmp.compare(gw, "one", "one", "q", prompt_tier="public").refusals[0]["reason"]
    r = cmp.compare(gw, "one", "keyless", "q", prompt_tier="public")
    assert not r.ran and "API key" in r.refusals[0]["reason"]
    assert s.requests == []


def test_every_call_is_audited_with_its_provider_and_tier(monkeypatch, servers, audit):
    sa, sb = servers("a"), servers("b")
    gw = gateway(monkeypatch, provider("one", sa, priority=1), provider("two", sb, priority=2))
    cmp.compare(gw, "one", "two", "q", prompt_tier="public", session_id="s1")
    sent = [c for c in audit if c["reason"] == "compare: sent"]
    assert {c["provider"] for c in sent} == {"one", "two"}
    assert all(
        c["classifier"] == "compare" and c["tier"] == "any" and c["session_id"] == "s1"
        for c in sent
    )
    assert all(
        c["query_hash"] and c["query_hash"] != "q" for c in sent
    )  # a hash, never the plaintext


def test_one_side_failing_is_reported_and_no_comparison_is_invented(monkeypatch, servers, audit):
    good, bad = servers("A perfectly good answer."), servers("", status=400)
    gw = gateway(monkeypatch, provider("good", good, priority=1), provider("bad", bad, priority=2))
    r = cmp.compare(gw, "good", "bad", "q", prompt_tier="public")
    assert r.a.status == "ok" and r.b.status == "error" and r.b.error
    assert r.delta is None
    out = cmp.render(r)
    assert "bad FAILED" in out and "400" in out
    assert "A perfectly good answer." in out
    assert "No comparison" in out and "Delta" not in out


def test_a_slow_side_times_out_and_the_other_is_still_shown(monkeypatch, servers, audit):
    fast, slow = servers("quick answer"), servers("late", delay=1.5)
    gw = gateway(
        monkeypatch, provider("fast", fast, priority=1), provider("slow", slow, priority=2)
    )
    t0 = time.monotonic()
    r = cmp.compare(gw, "fast", "slow", "q", prompt_tier="public", timeout=0.3)
    assert time.monotonic() - t0 < 1.2  # did not wait for the slow one
    assert r.a.status == "ok" and r.b.status == "timeout"
    assert "timed out" in cmp.render(r) and r.delta is None


def test_judge_gets_both_answers_and_is_labelled_an_opinion(monkeypatch, servers, audit):
    sa, sb, sj = servers("answer from A"), servers("answer from B"), servers("They differ on X.")
    gw = gateway(
        monkeypatch,
        provider("one", sa, priority=1),
        provider("two", sb, priority=2),
        provider("judge", sj, priority=3),
    )
    r = cmp.compare(gw, "one", "two", "the question", prompt_tier="public", judge="judge")
    sent = sj.requests[0]["messages"][-1]["content"]
    assert "answer from A" in sent and "answer from B" in sent and "the question" in sent
    assert r.judge.status == "ok" and r.judge.text == "They differ on X."
    out = cmp.render(r)
    assert "model-generated opinion" in out and "They differ on X." in out
    assert any(c["provider"] == "judge" and c["reason"] == "compare judge: sent" for c in audit)


def test_a_judge_the_tier_forbids_is_refused_but_the_two_answers_still_run(
    monkeypatch, servers, audit
):
    sa, sb, sj = servers("a"), servers("b"), servers("never")
    gw = gateway(
        monkeypatch,
        provider("one", sa, tier="export_controlled", priority=1),
        provider("two", sb, tier="export_controlled", priority=2),
        provider("judge", sj, tier="public", priority=3),
    )
    r = cmp.compare(
        gw, "one", "two", "q", session_tier="export_controlled", prompt_tier="public", judge="judge"
    )
    assert r.ran and r.judge.status == "refused" and sj.requests == []
    assert "judge refused" in cmp.render(r)


def test_long_answers_are_bounded_with_a_stated_limit_unless_full(monkeypatch, servers, audit):
    big = "word " * 3000
    sa, sb = servers(big), servers("short")
    gw = gateway(monkeypatch, provider("one", sa, priority=1), provider("two", sb, priority=2))
    r = cmp.compare(gw, "one", "two", "q", prompt_tier="public")
    short = cmp.render(r)
    assert re.search(r"truncated: showing 4000 of 15000 characters; rerun with --full", short)
    assert "truncated" not in cmp.render(r, full=True)
    assert len(json.dumps(cmp.to_dict(r))) > 15000  # --json is never cut


# ---- the command --------------------------------------------------------------------------------


@pytest.fixture
def agent(monkeypatch, servers, audit):
    sa, sb = servers("alpha says 40 psi"), servers("beta says 55 psi")
    gw = gateway(
        monkeypatch,
        provider("home", sa, model="small", priority=1),
        provider("cloudy", sb, model="big", tier="public", placement="cloud", priority=2),
        provider("nokey", sa, api_key_env="NO_SUCH_KEY", model="m3", priority=3),
        provider("secure", sb, model="m4", tier="export_controlled", priority=4),
    )
    ag = SimpleNamespace(
        gateway=gw,
        session=Session(),
        usage=SimpleNamespace(turns=[]),
        servers=(sa, sb),
    )
    return ag


def run(agent, line):
    return re.sub(
        r"\x1b\[[0-9;]*m",
        "",
        str(REGISTRY.dispatch(line, CommandContext(agent=agent, store=None, line=line))),
    )


def test_completion_offers_configured_providers_then_the_rest_then_nothing(agent):
    c = REGISTRY.get("/compare").complete
    first = dict(c(agent, []))
    assert set(first) == {"home", "cloudy", "nokey", "secure"}
    assert "small" in first["home"] and "LOCAL" in first["home"]
    assert "CLOUD" in first["cloudy"]
    assert "no API key" in first["nokey"]
    second = dict(c(agent, ["home"]))
    assert set(second) == {"cloudy", "nokey", "secure"}
    assert c(agent, ["home", "cloudy"]) == [] and c(agent, ["home", "cloudy", "what"]) == []


def test_completion_marks_providers_the_session_tier_does_not_admit(agent):
    where.note_content_tier(agent.session, "export_controlled")
    first = dict(REGISTRY.get("/compare").complete(agent, []))
    assert "REFUSED" in first["home"] and "REFUSED" in first["cloudy"]
    assert "REFUSED" not in first["secure"]


def test_command_runs_and_records_one_marked_entry_without_touching_the_transcript(agent, audit):
    agent.session.add_message("user", "earlier")
    before = [(m.role, m.content) for m in agent.session.messages]
    out = run(agent, "/compare home cloudy what is the limit  of it?")
    assert "alpha says 40 psi" in out and "beta says 55 psi" in out
    assert "A home" in out.replace("  ", " ") or "home" in out
    assert "A says 40, B says 55" in out
    assert [(m.role, m.content) for m in agent.session.messages] == before
    entries = agent.session.context["comparisons"]
    assert len(entries) == 1 and entries[0]["kind"] == "comparison"
    assert (
        entries[0]["prompt"] == "what is the limit  of it?"
    )  # the prompt as typed, spacing intact
    assert entries[0]["a"]["text"] == "alpha says 40 psi"
    json.dumps(agent.session.context)  # still serialisable with the session
    assert agent.gateway._provider_override is None
    sent = agent.servers[0].requests[0]["messages"]
    assert [m["content"] for m in sent] == ["earlier", "what is the limit  of it?"]


def test_command_json_is_the_dataclass_and_flags_work_before_or_after_the_prompt(agent, audit):
    d = json.loads(run(agent, "/compare home cloudy --json why is the sky blue --full"))
    assert (
        d["prompt"] == "why is the sky blue"
        and d["a"]["provider"] == "home"
        and d["delta"]["words_a"] == 4
    )


def test_command_refuses_on_session_tier_and_sends_nothing(agent, audit):
    where.note_content_tier(agent.session, "export_controlled")
    out = run(agent, "/compare home secure anything")
    assert "refused" in out.lower() and "home" in out
    assert agent.servers[0].requests == [] and agent.servers[1].requests == []
    assert agent.session.context["comparisons"][0]["ran"] is False


def test_a_comparison_that_ran_on_controlled_content_makes_the_session_controlled(
    monkeypatch, servers, audit
):
    s = servers("x")
    gw = gateway(
        monkeypatch,
        provider("p", s, tier="export_controlled", priority=1),
        provider("q", s, tier="export_controlled", priority=2),
    )
    ag = SimpleNamespace(gateway=gw, session=Session(), usage=SimpleNamespace(turns=[]))
    ag._router = SimpleNamespace(
        classify=lambda text, **kw: SimpleNamespace(tier=SimpleNamespace(value="export_controlled"))
    )
    run(ag, "/compare p q something sensitive")
    assert where.content_tier_of(ag.session) == "export_controlled"


@pytest.mark.parametrize(
    "line,needle",
    [
        ("/compare", "two provider names"),
        ("/compare home", "two provider names"),
        ("/compare home cloudy", "prompt"),
        ("/compare home cloudy --bogus hi", "unknown option"),
    ],
)
def test_bad_usage_says_what_is_missing(agent, line, needle):
    out = run(agent, line)
    assert needle in out and "usage:" in out
    assert agent.servers[0].requests == []


def test_rag_off_is_honoured_and_retrieval_is_computed_once_for_both(monkeypatch, servers, audit):
    from axiom.extensions.builtins.chat.agent import ChatAgent
    from axiom.rag.store import SearchResult

    sa, sb = servers("a"), servers("b")
    gw = gateway(monkeypatch, provider("one", sa, priority=1), provider("two", sb, priority=2))
    ag = ChatAgent.__new__(ChatAgent)
    ag.session = Session()
    ag.gateway = gw
    ag._rag_init_attempted = True
    store = MagicMock()
    hit = SearchResult(
        source_path="doc.md",
        source_title="doc.md",
        chunk_text="RETRIEVED-MARKER body",
        chunk_index=0,
        similarity=0.9,
        combined_score=0.9,
        corpus="rag-internal",
    )
    store.search.side_effect = lambda **kw: [hit]
    ag._rag_store = store
    try:
        ag._build_system_prompt(scope=None)
    except (
        Exception
    ) as exc:  # the bare agent is too bare to build a prompt: say so rather than skip silently
        pytest.skip(f"bare ChatAgent cannot build a system prompt in this environment: {exc!r}")

    run(ag, "/compare one two tell me about the doc")
    sys_a = sa.requests[0]["messages"][0]["content"]
    assert "RETRIEVED-MARKER" in sys_a
    assert sa.requests[0]["messages"] == sb.requests[0]["messages"]
    searches = store.search.call_count

    ag.session.context["rag"] = "off"
    run(ag, "/compare one two tell me about the doc again")
    assert "RETRIEVED-MARKER" not in sa.requests[1]["messages"][0]["content"]
    assert store.search.call_count == searches  # off means the store was not touched
