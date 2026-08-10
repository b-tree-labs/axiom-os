# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""axiom_secrets_* / axiom_vault_audit MCP tools (issue #667).

Same thin-delegate pattern as the #665/#666 action tools: the handlers
call into the secrets/vault extensions; domain logic stays there. The
invariant these tests pin: **secret VALUES never transit MCP** — list
and audit are metadata-only, and rotate-trigger returns a journaled
action handle, never a credential.

State is isolated per-test (AXI_STATE_DIR → tmp_path, file value
backend, jsonl ledger). No keychain, no live issuer.
"""

from __future__ import annotations

import json
import logging

import pytest

from axiom.extensions.builtins.memory import mcp_server


@pytest.fixture
def state(tmp_path, monkeypatch):
    from axiom.policy import action_ledger

    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AXIOM_FOREIGN_SECRETS_BACKEND", "file")
    monkeypatch.setenv("AXIOM_MODE", "dev")
    monkeypatch.setenv("AXIOM_ACTION_LEDGER_BACKEND", "jsonl")
    monkeypatch.delenv("AXIOM_AUDIT_HMAC_KEY", raising=False)
    action_ledger._reset_backend_cache()
    yield tmp_path
    action_ledger._reset_backend_cache()


def _seed(state, name="hpc-gitlab-pat", **meta):
    from axiom.extensions.builtins.secrets.foreign.store import (
        ForeignCredentialStore,
    )

    store = ForeignCredentialStore(state)
    store.set(name, b"glpat-MCPSENTINEL", **meta)
    return store


class TestSecretsList:
    def test_lists_names_and_metadata_never_values(self, state):
        _seed(state, expires_at="2026-08-01")
        out = mcp_server.secrets_list()
        assert out["count"] == 1
        assert out["items"][0]["name"] == "hpc-gitlab-pat"
        assert "glpat-MCPSENTINEL" not in json.dumps(out)

    def test_empty_state(self, state):
        out = mcp_server.secrets_list()
        assert out == {"count": 0, "items": []}


class TestVaultAudit:
    def test_expiry_findings_no_values(self, state):
        _seed(state, expires_at="2026-01-01")  # long past
        out = mcp_server.vault_audit()
        levels = {
            f["name"]: f["level"]
            for f in out["foreign_secrets"]["findings"]
        }
        assert levels["hpc-gitlab-pat"] == "expired"
        assert "glpat-MCPSENTINEL" not in json.dumps(out)
        assert "available" in out["capabilities"]


class TestRotateTrigger:
    def test_headless_guided_fails_closed_and_journals(self, state):
        from axiom.policy.action_ledger import search_actions

        _seed(state)  # no provider metadata → guided → interactive-only
        out = mcp_server.secrets_rotate_trigger(name="hpc-gitlab-pat")
        assert out["ok"] is False
        blob = json.dumps(out)
        assert "glpat-MCPSENTINEL" not in blob
        # The refusal/failure is journaled — the handle is queryable.
        found = search_actions(op_class="secrets.rotate", state_dir=state)
        assert found["count"] >= 1
        assert out["value"]["action_id"]

    def test_unknown_name_errors_cleanly(self, state):
        out = mcp_server.secrets_rotate_trigger(name="nope")
        assert out["ok"] is False
        assert out["errors"]

    def test_never_returns_a_value_key_with_secret_material(self, state):
        _seed(state)
        out = mcp_server.secrets_rotate_trigger(name="hpc-gitlab-pat")
        # The rotate skill's payload is metadata/handles; assert the
        # sentinel is absent everywhere, including error strings.
        assert "glpat-MCPSENTINEL" not in json.dumps(out)


class TestToolWiring:
    def test_tools_advertised_and_handlers_wired(self):
        names = {t.name for t in mcp_server._TOOLS}
        expected = {
            "axiom_secrets_list",
            "axiom_vault_audit",
            "axiom_secrets_rotate_trigger",
        }
        assert expected <= names
        assert expected <= set(mcp_server._HANDLERS)

    def test_rotate_trigger_schema_requires_name(self):
        tool = next(
            t for t in mcp_server._TOOLS
            if t.name == "axiom_secrets_rotate_trigger"
        )
        assert "name" in tool.inputSchema.get("required", [])
