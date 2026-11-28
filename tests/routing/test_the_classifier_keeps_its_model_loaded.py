# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The export-control classifier keeps its model loaded, so a cold start is not a verdict.

Found on a node (2026-11-26): the classifier's small local model was unloaded when idle,
loading it took longer than the classifier's two-second wait, and every question came
back "classifier unavailable, routed as export-controlled" without being read. Now each
call asks the model server to keep the model loaded, a router warms it in the background
when it starts, and a timeout starts a warm so the next question finds it loaded.
"""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch

from axiom.llm import router as rt
from axiom.llm.router import ClassifierFailure, OllamaClassifier


def _ready(clf: OllamaClassifier) -> OllamaClassifier:
    clf._available = True  # skip the reachability probe
    return clf


def _ok(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.read.return_value = json.dumps(body).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda s, *a: False
    return resp


def _payloads(mock) -> list[dict]:
    return [json.loads(call.args[0].data) for call in mock.call_args_list if call.args[0].data]


def test_each_classification_asks_the_server_to_keep_the_model_loaded():
    clf = _ready(OllamaClassifier(model="m", keep_alive="24h"))
    with patch("urllib.request.urlopen", return_value=_ok({"response": "PUBLIC"})) as op:
        clf.classify("what is the capital of France?")
    assert _payloads(op)[0]["keep_alive"] == "24h"


def test_warming_loads_the_model_without_asking_anything():
    clf = _ready(OllamaClassifier(model="m", keep_alive="24h"))
    op = MagicMock(return_value=_ok({"done": True}))
    clf._warm_open = op
    clf.warm(wait=True)
    body = _payloads(op)[0]
    assert body["model"] == "m" and body["keep_alive"] == "24h"
    assert "prompt" not in body or body["prompt"] == ""
    # Loading is slow: the warm call waits far longer than a classification may.
    assert op.call_args.kwargs["timeout"] >= 60


def test_warming_in_the_background_returns_at_once():
    clf = _ready(OllamaClassifier(model="m"))
    gate = threading.Event()

    def slow(*_a, **_k):
        gate.wait(5)
        return _ok({"done": True})

    clf._warm_open = slow
    thread = clf.warm()
    assert thread is not None and thread.is_alive()
    gate.set()
    thread.join(5)


def test_a_timeout_starts_a_warm_for_the_next_question():
    clf = _ready(OllamaClassifier(model="m"))
    warmed = []
    clf.warm = lambda wait=False: warmed.append(wait)  # type: ignore[method-assign]
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        result = clf.classify("anything")
    assert isinstance(result, ClassifierFailure) and result.reason == "timeout"
    assert warmed == [False]


def test_a_new_router_warms_its_classifier():
    warmed = []
    fake = MagicMock(spec=OllamaClassifier)
    fake.warm.side_effect = lambda wait=False: warmed.append(wait)
    rt.QueryRouter(ollama=fake)
    assert warmed == [False]


def test_the_keep_alive_comes_from_the_setting(monkeypatch):
    from axiom.extensions.builtins.settings import store

    monkeypatch.setattr(
        store.SettingsStore,
        "get",
        lambda self, key, default=None: {"routing.classifier_keep_alive": "-1"}.get(key, default),
    )
    assert rt.QueryRouter._default_ollama()._keep_alive == "-1"
