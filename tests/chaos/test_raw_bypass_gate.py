# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Security: the raw bypass belongs to the deployment, not to the request.

`raw` skips retrieval, the system prompt and tool routing — every governance
layer the wrapper exists to apply. It was settable by any caller from the
query string or the request body, and its own docstring said "production
traffic should NOT set the flag".

An intention with no mechanism reads as a control and is not one. Being
authenticated is not the same as being authorised to switch the safety
wrapper off for a turn, so it is enabled per-deployment and refused otherwise.
"""

from __future__ import annotations

import pytest

from axiom.extensions.builtins.http.chat_server import (
    RAW_BYPASS_ENV,
    raw_bypass_allowed,
    raw_bypass_refusal,
)


class TestOffUnlessTheDeploymentEnablesIt:
    def test_it_is_off_when_unset(self, monkeypatch):
        monkeypatch.delenv(RAW_BYPASS_ENV, raising=False)
        assert raw_bypass_allowed() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " on "])
    def test_it_is_on_when_the_deployment_says_so(self, monkeypatch, value):
        monkeypatch.setenv(RAW_BYPASS_ENV, value)
        assert raw_bypass_allowed() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "maybe", "2"])
    def test_anything_else_leaves_it_off(self, monkeypatch, value):
        """Fail closed. An unrecognised value is not permission."""
        monkeypatch.setenv(RAW_BYPASS_ENV, value)
        assert raw_bypass_allowed() is False


class TestTheRefusalIsUseful:
    def test_it_says_what_to_do_instead(self):
        message = raw_bypass_refusal()["error"]["message"]
        assert RAW_BYPASS_ENV in message, "must say which switch turns it on"
        assert "raw=1" in message, (
            "must name what the caller actually set; nobody types 'bypass'"
        )
        assert "whole deployment" in message

    def test_it_names_the_parameter_at_fault(self):
        assert raw_bypass_refusal()["error"]["param"] == "raw"

    def test_it_is_a_permission_error_not_a_bad_request(self):
        """The request was well formed; the deployment declined it."""
        assert raw_bypass_refusal()["error"]["type"] == "permission_error"


class TestBothImplementationsAreGated:
    """Two implementations of one contract is exactly how a control comes to
    exist on only one of them — as authentication already did here."""

    def test_the_capability_itself_is_gated(self):
        """The gate that actually matters. It sits where the switch is READ,
        so a new way to reach `raw` is not a new way around it."""
        import inspect

        from axiom.extensions.builtins.chat.agent import ChatAgent

        source = inspect.getsource(ChatAgent._raw_turn)
        assert "require_raw_allowed()" in source

    def test_both_transports_still_refuse_early(self):
        """Belt and braces: the capability raises, but each HTTP path checks
        first so the caller gets a 403 with an explanation rather than a 500
        from an escaped exception."""
        import inspect

        from axiom.extensions.builtins.http import chat_server

        source = inspect.getsource(chat_server)
        assert source.count("raw_bypass_allowed()") == 2

    def test_the_policy_has_one_definition(self):
        """HTTP must not carry a second copy of the rule."""
        from axiom.extensions.builtins.http import chat_server
        from axiom.infra import raw_mode

        assert chat_server.raw_bypass_allowed is raw_mode.raw_allowed
        assert chat_server.RAW_BYPASS_ENV == raw_mode.RAW_ENV

    def test_a_refused_request_is_not_answered_wrapped_instead(self):
        """It must 403, not quietly answer with the full pipeline: a
        benchmark harness would otherwise compare governed output against
        governed output and publish it as wrapped-versus-naked."""
        import inspect

        from axiom.extensions.builtins.http import chat_server

        source = inspect.getsource(chat_server)
        assert source.count("status_code=403, content=raw_bypass_refusal()") == 1
        assert source.count("_send_json(403, raw_bypass_refusal())") == 1


class TestTheGateHoldsEndToEnd:
    """Through the real router, not by reading the source."""

    @staticmethod
    def _client():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from axiom.extensions.builtins.http.chat_server import build_chat_router

        app = FastAPI()
        app.include_router(build_chat_router(prefix=""))
        return TestClient(app)

    def test_a_raw_request_is_refused_when_not_enabled(self, monkeypatch):
        from unittest.mock import MagicMock, patch

        from axiom.extensions.builtins.http import chat_server

        monkeypatch.delenv(RAW_BYPASS_ENV, raising=False)
        agent = MagicMock()
        agent.turn.return_value = "should never be produced"
        with patch.object(chat_server, "_get_agent", return_value=agent):
            response = self._client().post(
                "/v1/chat/completions?raw=1",
                json={"messages": [{"role": "user", "content": "hi"}]},
            )
        assert response.status_code == 403
        assert response.json()["error"]["param"] == "raw"
        agent.turn.assert_not_called(), "the turn ran despite being refused"

    def test_a_raw_turn_is_audited_when_enabled(self, monkeypatch, caplog):
        """The call site, not just the function. A mutation sweep showed that
        deleting the audit call broke nothing, because the only test called
        the logger helper directly."""
        from unittest.mock import MagicMock, patch

        from axiom.extensions.builtins.http import chat_server

        monkeypatch.setenv(RAW_BYPASS_ENV, "1")
        agent = MagicMock()
        agent.turn.return_value = "raw out"
        agent.session.principal_id = "@alice:example"
        with caplog.at_level("WARNING"):
            with patch.object(chat_server, "_get_agent", return_value=agent):
                response = self._client().post(
                    "/v1/chat/completions?raw=1",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )
        assert response.status_code == 200
        assert "raw=1" in caplog.text
        assert "@alice:example" in caplog.text

    def test_a_normal_turn_is_not_audited_as_raw(self, monkeypatch, caplog):
        """Pinned so the audit cannot fire on every turn and mean nothing."""
        from unittest.mock import MagicMock, patch

        from axiom.extensions.builtins.http import chat_server

        monkeypatch.delenv(RAW_BYPASS_ENV, raising=False)
        agent = MagicMock()
        agent.turn.return_value = "wrapped out"
        agent.session.principal_id = "@alice:example"
        with caplog.at_level("WARNING"):
            with patch.object(chat_server, "_get_agent", return_value=agent):
                self._client().post(
                    "/v1/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]},
                )
        assert "raw=1 answered straight" not in caplog.text


class TestBothRawPathsAudit:
    """The functional test above drives the ROUTER. A mutation sweep removing
    the RAW HANDLER's audit call survived it — two implementations of one
    contract, and a test that covers one of them.

    So both call sites are pinned. Source-level, deliberately: driving the
    BaseHTTPRequestHandler needs a fake-socket harness whose failure modes
    would tell you less than this does.
    """

    def test_every_raw_path_audits(self):
        import inspect

        from axiom.extensions.builtins.http import chat_server

        source = inspect.getsource(chat_server)
        assert source.count("_audit_raw_turn(agent.session.principal_id)") == 2, (
            "each raw-capable implementation must record that a turn ran "
            "unwrapped, and for whom"
        )

    def test_the_audit_is_guarded_by_raw_mode(self):
        """Pinned so it cannot fire on every turn and stop meaning anything."""
        import inspect

        from axiom.extensions.builtins.http import chat_server

        source = inspect.getsource(chat_server)
        assert source.count(
            "if raw_mode:\n                _audit_raw_turn("
        ) == 2


class TestRawTurnsAreAuditable:
    def test_a_raw_turn_names_its_principal(self, caplog):
        from axiom.extensions.builtins.http.chat_server import _audit_raw_turn

        with caplog.at_level("WARNING"):
            _audit_raw_turn("@alice:example")
        assert "@alice:example" in caplog.text
        assert "raw=1" in caplog.text

    def test_an_unidentified_raw_turn_is_still_recorded(self, caplog):
        from axiom.extensions.builtins.http.chat_server import _audit_raw_turn

        with caplog.at_level("WARNING"):
            _audit_raw_turn("")
        assert "unidentified" in caplog.text
