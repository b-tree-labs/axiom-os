"""Chat's streaming path tries the next provider instead of giving up.

`complete()` already failed over; `stream_with_tools`, which chat uses, picked
one provider and stopped. Measured on a local node: the first provider pointed
at a port nothing listened on, a working model sat one place down the list,
and the person got "LLM stream failed". A request too large for one model's
context is the same kind of failure: that model cannot take it, a bigger one
can.
"""

from __future__ import annotations


from axiom.llm import gateway as gw


def _gw(providers):
    g = gw.Gateway.__new__(gw.Gateway)
    g.providers = providers
    g._trace_call = lambda *a, **k: None
    g._trace_generation = lambda *a, **k: None
    g._select_provider = lambda task, routing_tier, prefer=None: providers[0]
    g._check_vpn = lambda p: True
    return g


def _p(name):
    return gw.LLMProvider(name=name, endpoint=f"http://{name}", model=name, api_key_env="")


def _text(chunks):
    return "".join(c.text for c in chunks if c.type == "text")


def test_a_refused_connection_moves_to_the_next_provider(monkeypatch):
    a, b = _p("first"), _p("second")
    g = _gw([a, b])

    def stream(provider, *args):
        if provider is a:
            raise ConnectionError("Connection refused")
        yield gw.StreamChunk(type="text", text="hello from second")
        yield gw.StreamChunk(type="done")

    g._stream_provider = stream
    assert _text(g.stream_with_tools([{"role": "user", "content": "hi"}])) == "hello from second"


def test_a_request_too_large_for_one_model_moves_to_the_next(monkeypatch):
    a, b = _p("small"), _p("large")
    g = _gw([a, b])


    def stream(provider, *args):
        if provider is a:
            raise gw.PersistentLLMError(
                "rejected", provider="small", status=400,
                body='{"error":{"type":"exceed_context_size_error","n_ctx":4096}}')
        yield gw.StreamChunk(type="text", text="fits here")
        yield gw.StreamChunk(type="done")

    g._stream_provider = stream
    assert _text(g.stream_with_tools([{"role": "user", "content": "hi"}])) == "fits here"


def test_a_bad_request_for_any_other_reason_still_surfaces(monkeypatch):
    a, b = _p("first"), _p("second")
    g = _gw([a, b])
    tried = []

    def stream(provider, *args):
        tried.append(provider.name)
        raise gw.PersistentLLMError("bad auth", provider=provider.name, status=401, body="unauthorized")
        yield  # pragma: no cover

    g._stream_provider = stream
    out = _text(g.stream_with_tools([{"role": "user", "content": "hi"}]))
    assert tried == ["first"] and "LLM stream failed" in out


def test_a_failure_after_text_has_streamed_is_not_spliced_onto_another_model(monkeypatch):
    a, b = _p("first"), _p("second")
    g = _gw([a, b])
    tried = []

    def stream(provider, *args):
        tried.append(provider.name)
        yield gw.StreamChunk(type="text", text="half an ans")
        raise ConnectionError("reset")

    g._stream_provider = stream
    out = _text(g.stream_with_tools([{"role": "user", "content": "hi"}]))
    assert tried == ["first"]
    assert out.startswith("half an ans") and "LLM stream failed" in out


def test_when_every_provider_fails_each_one_is_named(monkeypatch):
    g = _gw([_p("first"), _p("second")])

    def stream(provider, *args):
        raise ConnectionError(f"{provider.name} refused")
        yield  # pragma: no cover

    g._stream_provider = stream
    out = _text(g.stream_with_tools([{"role": "user", "content": "hi"}]))
    assert "first" in out and "second" in out
