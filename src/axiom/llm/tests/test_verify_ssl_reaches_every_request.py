"""A provider's `verify_ssl = false` reaches every request it makes.

Only the tool-calling path honoured it. The streaming path, which chat uses,
and the plain completion path checked the certificate anyway, so a private
model server with a self-signed certificate answered the tool path and failed
chat with CERTIFICATE_VERIFY_FAILED, the provider configured exactly as the
setting's own comment says to.
"""

from __future__ import annotations

from axiom.llm import gateway as gw


class _Response:
    def iter_lines(self, decode_unicode=True):
        yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
        yield "data: [DONE]"

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


def _capture(monkeypatch):
    seen = {}

    def post(requests_mod, url, payload, headers, timeout=60, **kwargs):
        seen.update(kwargs)
        return _Response()

    monkeypatch.setattr(gw, "_post_with_rate_limit_retry", post)
    return seen


def _provider(verify):
    return gw.LLMProvider(
        name="private",
        endpoint="https://llm.example/v1",
        model="m",
        api_key_env="",
        verify_ssl=verify,
    )


def _gw():
    return gw.Gateway.__new__(gw.Gateway)


def test_the_stream_path_skips_verification_when_told_to(monkeypatch):
    seen = _capture(monkeypatch)
    list(_gw()._stream_openai(_provider(False), [{"role": "user", "content": "hi"}], "", None, 64))
    assert seen.get("verify") is False


def test_the_stream_path_verifies_by_default(monkeypatch):
    seen = _capture(monkeypatch)
    list(_gw()._stream_openai(_provider(True), [{"role": "user", "content": "hi"}], "", None, 64))
    assert seen.get("verify", True) is True


def test_the_completion_path_skips_verification_when_told_to(monkeypatch):
    seen = _capture(monkeypatch)
    _gw()._call_provider(_provider(False), "hi", "", 64)
    assert seen.get("verify") is False
