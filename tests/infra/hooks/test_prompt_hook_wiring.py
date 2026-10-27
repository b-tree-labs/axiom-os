# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Integration: hooks fire at the prompt composer flush."""

from __future__ import annotations

import pytest

from axiom.infra.bus import EventBus
from axiom.infra.hooks import (
    HookBus,
    HookDenied,
    HookSpec,
    allow,
    allow_modified,
    deny,
)


class TestPromptPreSubmit:
    def test_fires_with_messages_and_layers(self):
        from axiom.infra import prompt_gateway

        hookbus = HookBus()
        eventbus = EventBus()
        captured: list[dict] = []

        def hook(ctx):
            captured.append(dict(ctx.payload))
            return allow()

        hookbus.register(
            HookSpec(
                event="prompt.pre_submit",
                entry=hook,
                priority=100,
                fail_mode="abort",
                source="test",
            ),
        )

        result = prompt_gateway.submit_prompt(
            messages=[{"role": "user", "content": "hi"}],
            system_layers=[{"layer": "identity", "content": "you are axi"}],
            principal="@p:c",
            model_id="claude-3-5-sonnet",
            transport=lambda **kw: {"text": "ok"},
            hookbus=hookbus,
            eventbus=eventbus,
        )
        assert captured
        assert captured[0]["model_id"] == "claude-3-5-sonnet"
        assert captured[0]["messages"] == [{"role": "user", "content": "hi"}]
        assert result == {"text": "ok"}

    def test_deny_raises(self):
        from axiom.infra import prompt_gateway

        hookbus = HookBus()
        eventbus = EventBus()

        def gate(ctx):
            return deny(reason="prompt blocked")

        hookbus.register(
            HookSpec(
                event="prompt.pre_submit",
                entry=gate,
                priority=10,
                fail_mode="abort",
                source="policy",
            ),
        )

        with pytest.raises(HookDenied):
            prompt_gateway.submit_prompt(
                messages=[{"role": "user", "content": "hi"}],
                system_layers=[],
                principal="@p:c",
                model_id="m",
                transport=lambda **kw: {"text": "should not run"},
                hookbus=hookbus,
                eventbus=eventbus,
            )

    def test_allow_modified_splices_messages(self):
        from axiom.infra import prompt_gateway

        hookbus = HookBus()
        eventbus = EventBus()
        seen_messages: list[list[dict]] = []

        def rewriter(ctx):
            return allow_modified(
                messages=[{"role": "user", "content": "rewritten"}],
            )

        def fake_transport(**kw):
            seen_messages.append(list(kw["messages"]))
            return {"text": "ok"}

        hookbus.register(
            HookSpec(
                event="prompt.pre_submit",
                entry=rewriter,
                priority=10,
                fail_mode="abort",
                source="rewriter",
            ),
        )

        prompt_gateway.submit_prompt(
            messages=[{"role": "user", "content": "raw"}],
            system_layers=[],
            principal="@p:c",
            model_id="m",
            transport=fake_transport,
            hookbus=hookbus,
            eventbus=eventbus,
        )
        assert seen_messages == [[{"role": "user", "content": "rewritten"}]]


class TestPromptPostSubmit:
    def test_post_event_published(self):
        from axiom.infra import prompt_gateway

        hookbus = HookBus()
        eventbus = EventBus()
        captured: list[tuple[str, dict]] = []

        def observer(subject, payload):
            captured.append((subject, dict(payload)))

        eventbus.subscribe("prompt.post_submit", observer)

        prompt_gateway.submit_prompt(
            messages=[{"role": "user", "content": "hi"}],
            system_layers=[],
            principal="@p:c",
            model_id="m",
            transport=lambda **kw: {"text": "response"},
            hookbus=hookbus,
            eventbus=eventbus,
        )
        assert captured
        subject, payload = captured[0]
        assert subject == "prompt.post_submit"
        assert payload["model_id"] == "m"
        assert "latency_ms" in payload
