# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A forwarded call the node refuses says why, and what to do (#1709).

A new contributor ran a kit promotion from a bound laptop and read "this install
does not hold the medallion ... The node answered HTTP 403." The node had said
why (its key was a reader key, without the scope a submission needs) in its
error envelope, and the client dropped it: it looked for ``detail`` and the
composed app answers ``{"error": {"code", "message"}}``. The routing fact became
the headline and the reason was lost.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from axiom.extensions.builtins.data_platform import read_target as rt

FACE = "https://node.example-site.test"
KEY_ID = "0123456789ab"
TOKEN = f"axk_{KEY_ID}_secretpartneverprinted"


def _refuse(message, code="forbidden"):
    body = json.dumps({"error": {"code": code, "message": message, "request_id": "req_1",
                                 "extension": "data_platform"}}).encode()

    def urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(body))

    return urlopen


@pytest.fixture
def target(monkeypatch):
    from axiom.infra import node_compat

    monkeypatch.setattr(node_compat, "check_once", lambda url: None)
    monkeypatch.setattr(node_compat, "known_older", lambda url: "")
    monkeypatch.setattr(rt, "_key", lambda url: TOKEN)
    return rt.ReadTarget("binding", site="example-site", face_url=FACE)


def _ask(target, monkeypatch, urlopen, verb):
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    result = rt.ask(target, verb=verb, params={})
    assert result.ok is False
    return "\n".join(result.errors)


def test_a_scope_refusal_names_the_missing_scope_and_the_operators_fix(target, monkeypatch):
    text = _ask(target, monkeypatch, _refuse("credential scopes do not cover this resource"),
                "data.kit_submit")
    headline = text.splitlines()[0]
    assert "does not hold the medallion" not in headline
    assert "data_platform:invoke" in headline
    assert f"gate rescope api-key {KEY_ID} --add-scope data_platform:invoke" in text
    assert KEY_ID in headline
    assert "secretpartneverprinted" not in text
    assert "does not hold the medallion" in text, "the routing fact stays, as a second line"


def test_a_read_refusal_names_the_read_scope(target, monkeypatch):
    text = _ask(target, monkeypatch, _refuse("credential scopes do not cover this resource"),
                "data.series")
    assert "data_platform:read" in text.splitlines()[0]


def test_no_credential_sent_says_so_and_points_at_the_vault(target, monkeypatch):
    monkeypatch.setattr(rt, "_key", lambda url: "")
    text = _ask(target, monkeypatch, _refuse("no valid credential", code="invalid_credential"),
                "data.kit_submit")
    headline = text.splitlines()[0]
    assert "no key" in headline.lower()
    assert "vault where" in headline


def test_a_key_the_node_does_not_accept_is_named(target, monkeypatch):
    text = _ask(target, monkeypatch, _refuse("no valid credential", code="invalid_credential"),
                "data.series")
    headline = text.splitlines()[0]
    assert KEY_ID in headline and "did not accept" in headline


def test_any_other_refusal_keeps_the_nodes_reason_verbatim(target, monkeypatch):
    reason = "this key carries no site, so it reads no site's data"
    text = _ask(target, monkeypatch, _refuse(reason), "data.series")
    assert reason in text.splitlines()[0]
