# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""DP-AUTH-1: every ``axi data`` skill that mutates platform state
must consult GUARD via the easy-onramp before doing work, and must
surface the receipt fragment id in ``actions_taken`` so the operator
can chase the audit chain via ``axi audit show <id>``.

These tests monkeypatch the data-platform's ``_authz.action`` context
manager so the suite runs without a Postgres-backed authz schema —
the integration that the production stack actually exercises lives
behind the AUTHZ pg_only marker in the authz extension's own tests.
"""

from __future__ import annotations

import contextlib
from typing import Any
from unittest.mock import MagicMock

import pytest

from axiom.extensions.builtins.data_platform import _authz
from axiom.extensions.builtins.data_platform.skills import (
    diagnose as diagnose_skill,
)
from axiom.extensions.builtins.data_platform.skills import (
    ingest as ingest_skill,
)
from axiom.extensions.builtins.data_platform.skills import (
    install as install_skill,
)
from axiom.extensions.builtins.data_platform.skills import (
    register as register_skill,
)
from axiom.infra.paths import get_user_state_dir
from axiom.infra.skills import SkillContext, SkillRegistry


@pytest.fixture()
def captured_actions(monkeypatch):
    """Replace _authz.action with a context manager that records every
    invocation and yields a synthetic receipt id. Returns the list of
    (verb, resource, classification_name, actor) tuples captured."""
    captured: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def _fake_action(*, verb, resource, classification=None, actor=None):
        captured.append({
            "verb": verb,
            "resource": resource,
            "classification": getattr(classification, "name", str(classification)),
            "actor": actor,
        })
        yield MagicMock(receipt_id=f"test-receipt-{verb}-{len(captured)}")

    monkeypatch.setattr(_authz, "action", _fake_action)
    return captured


@pytest.fixture()
def ctx():
    import logging
    return SkillContext(
        registry=SkillRegistry(),
        state_dir=get_user_state_dir(),
        logger=logging.getLogger("test.dp1.authz"),
        user_prompt=None,
    )


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


class TestInstallSkillWrapsInActionEnvelope:
    def test_install_wraps_helm_call(self, captured_actions, monkeypatch, ctx, tmp_path):
        # Short-circuit kubectl current-context detection.
        monkeypatch.setattr(install_skill.shutil, "which", lambda b: f"/usr/bin/{b}")

        def _subproc(cmd, *args, **kw):
            class R:
                returncode = 0
                stdout = "k3d-example\n"
                stderr = ""
            return R()
        monkeypatch.setattr(install_skill.subprocess, "run", _subproc)

        # Force skip_diagnose so we don't chain into a second action.
        result = install_skill.run(
            {
                "namespace": "axiom-data",
                "release": "axiom-data-platform",
                "kube_context": "k3d-example",
                "skip_diagnose": True,
                "actor": "@operator:test",
            },
            ctx,
        )

        assert result.ok
        # Exactly one action() call, with the install verb and the right
        # data-platform://<namespace> resource shape.
        assert len(captured_actions) == 1
        c = captured_actions[0]
        assert c["verb"] == "install"
        assert c["resource"] == "data-platform://axiom-data"
        assert c["actor"] == "@operator:test"
        # Receipt id surfaced to actions_taken so the operator can grep it.
        assert any("audit-receipt:" in line for line in result.actions_taken)


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------


class TestIngestSkillWrapsRunIngest:
    def test_ingest_wraps_run_ingest(self, captured_actions, monkeypatch, ctx):
        # Stand-in for run_ingest so we don't need a real connector.
        class _Report:
            connector = "example-box-corpus"
            proceed = True
            items_seen = 5
            items_landed = 5
            items_failed = 0
            refused_reason = ""

        monkeypatch.setattr(ingest_skill, "run_ingest", lambda *a, **kw: _Report())

        result = ingest_skill.run(
            {"connector": "example-box-corpus", "actor": "@operator:test"},
            ctx,
        )

        assert result.ok
        assert len(captured_actions) == 1
        c = captured_actions[0]
        assert c["verb"] == "ingest"
        assert c["resource"] == "data-platform://connector/example-box-corpus"
        assert c["actor"] == "@operator:test"
        assert any("audit-receipt:" in line for line in result.actions_taken)

    def test_ingest_without_connector_short_circuits_no_audit(
        self, captured_actions, ctx,
    ):
        # Param validation fires before the action wrap.
        result = ingest_skill.run({}, ctx)
        assert not result.ok
        assert captured_actions == []


# ---------------------------------------------------------------------------
# register
# ---------------------------------------------------------------------------


class TestRegisterSkillWrapsConnectorSave:
    def test_register_wraps_save_connector(
        self, captured_actions, monkeypatch, ctx,
    ):
        # Skip the source-kind registry lookup — provide a fake provider.
        class _FakeProvider:
            def validate(self, _cfg):
                return []

        class _FakeRegistry:
            def get(self, kind):  # noqa: ARG002
                return _FakeProvider()

        monkeypatch.setattr(
            register_skill,
            "default_source_kind_registry",
            lambda: _FakeRegistry(),
        )
        monkeypatch.setattr(
            register_skill,
            "load_connector",
            lambda name, state_dir: (_ for _ in ()).throw(FileNotFoundError),
        )

        saved = {}
        def _save(config, state_dir):  # noqa: ANN001
            saved["config"] = config
            return f"/fake/{config.name}.toml"
        monkeypatch.setattr(register_skill, "save_connector", _save)

        result = register_skill.run(
            {
                "name": "example-box-corpus",
                "kind": "box",
                "bronze_root": "/var/lib/axiom/bronze",
                "actor": "@operator:test",
            },
            ctx,
        )

        assert result.ok
        assert "config" in saved
        assert len(captured_actions) == 1
        c = captured_actions[0]
        assert c["verb"] == "register"
        assert c["resource"] == "data-platform://connector/example-box-corpus"
        assert any("audit-receipt:" in line for line in result.actions_taken)


# ---------------------------------------------------------------------------
# diagnose
# ---------------------------------------------------------------------------


class TestDiagnoseSkillWrapsKubectlPasses:
    def test_diagnose_wraps_namespace_action(
        self, captured_actions, monkeypatch, ctx,
    ):
        # Short-circuit the kubectl + helm calls so the test runs without
        # a real cluster.
        monkeypatch.setattr(diagnose_skill.shutil, "which", lambda b: f"/usr/bin/{b}")
        import json as _json

        # Return shape-valid JSON for each kubectl/helm probe so the
        # diagnose pass reports no irregularities and skips the
        # troubleshoot delegation. Only the "everything green" path needs
        # to be exercised here; the irregular branch is covered by the
        # diagnose extension's own tests.
        def _subproc(cmd, *_a, **_kw):
            stdout = "{}"
            if "deploy" in cmd:
                stdout = _json.dumps({"status": {"readyReplicas": 1, "replicas": 1}})
            elif "pvc" in cmd:
                stdout = _json.dumps({"status": {"phase": "Bound"}})
            elif "status" in cmd:  # helm status
                stdout = _json.dumps({"info": {"status": "deployed"}})
            class _R:
                returncode = 0
                stderr = ""
            r = _R()
            r.stdout = stdout
            return r
        monkeypatch.setattr(diagnose_skill.subprocess, "run", _subproc)

        result = diagnose_skill.run(
            {"namespace": "axiom-data", "actor": "@operator:test"},
            ctx,
        )

        # Exactly one action() invocation regardless of pass/fail downstream.
        assert len(captured_actions) == 1
        c = captured_actions[0]
        assert c["verb"] == "diagnose"
        assert c["resource"] == "data-platform://axiom-data"
        assert any("audit-receipt:" in line for line in result.actions_taken)


# ---------------------------------------------------------------------------
# _authz fallback when setup_extension cannot wire (smoke; no DB)
# ---------------------------------------------------------------------------


class TestAuthzFallbackWhenWiringUnavailable:
    def test_synthetic_receipt_emitted_when_setup_fails(self, monkeypatch):
        """If setup_extension raises, _authz.action() yields a synthetic
        action and the wrapped skill still runs to completion (with a
        no-authz receipt id the operator can grep on)."""
        # Reset the module's lazy state.
        monkeypatch.setattr(_authz, "_ctx", None)
        monkeypatch.setattr(_authz, "_ctx_init_failed", False)
        # Force setup_extension to raise.
        import axiom.governance.simple as _simple
        monkeypatch.setattr(
            _simple, "setup_extension",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no db")),
        )

        with _authz.action(verb="ingest", resource="data-platform://x") as act:
            pass
        assert act.receipt_id.startswith("data-no-authz-ingest")
