# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""TDD tests for ``observability.install`` skill.

Heavy subprocess mocking — the skill shells to ``helm`` + ``kubectl``;
we intercept at ``shutil.which`` and ``subprocess.run`` so no real
binaries / cluster are required.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch


from axiom.extensions.builtins.observability.skills import install


def _ok_proc(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_missing_helm_returns_error(skill_ctx):
    with patch("shutil.which", side_effect=lambda b: None):
        r = install.run({}, skill_ctx)
    assert not r.ok
    assert any("helm" in e for e in r.errors)


def test_missing_kubectl_returns_error(skill_ctx):
    def fake_which(b: str) -> str | None:
        return "/usr/bin/helm" if b == "helm" else None
    with patch("shutil.which", side_effect=fake_which):
        r = install.run({}, skill_ctx)
    assert not r.ok
    assert any("kubectl" in e for e in r.errors)


def test_no_active_context_returns_error(skill_ctx):
    with patch("shutil.which", return_value="/bin/x"), \
         patch("subprocess.run", return_value=_ok_proc(stdout="")):
        r = install.run({}, skill_ctx)
    assert not r.ok
    assert any("context" in e.lower() for e in r.errors)


# All success-path tests opt into postgres_mode=internal so they don't
# need a real Postgres DSN. The shared-mode path is exercised by its
# own dedicated tests below (which mock the psycopg2 bootstrap).

_INTERNAL_PG = {"postgres_mode": "internal"}


def test_helm_failure_propagates(skill_ctx):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[:3] == ["kubectl", "config", "current-context"]:
            return _ok_proc(stdout="kind-axiom\n")
        if cmd[0] == "helm":
            return _ok_proc(stdout="", returncode=1)
        return _ok_proc(stdout="")

    with patch("shutil.which", return_value="/bin/x"), \
         patch("subprocess.run", side_effect=fake_run):
        r = install.run({**_INTERNAL_PG,
                         "skip_diagnose": True, "salt": "s", "nextauth_secret": "n",
                         "encryption_key": "e", "postgres_password": "p",
                         "clickhouse_password": "c"}, skill_ctx)
    assert not r.ok
    assert any("helm" in e.lower() for e in r.errors)


def test_install_dry_run_invokes_helm_with_dry_run(skill_ctx):
    cmds: list[list[str]] = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        if cmd[:3] == ["kubectl", "config", "current-context"]:
            return _ok_proc(stdout="kind-axiom\n")
        return _ok_proc(stdout="")

    with patch("shutil.which", return_value="/bin/x"), \
         patch("subprocess.run", side_effect=fake_run):
        r = install.run({**_INTERNAL_PG,
                         "dry_run": True, "salt": "s", "nextauth_secret": "n",
                         "encryption_key": "e", "postgres_password": "p",
                         "clickhouse_password": "c"}, skill_ctx)

    helm_cmds = [c for c in cmds if c and c[0] == "helm"]
    assert helm_cmds, "helm not invoked"
    assert "--dry-run" in helm_cmds[0]
    assert r.ok


def test_install_success_returns_release_info_and_env(skill_ctx):
    def fake_run(cmd, **kw):
        if cmd[:3] == ["kubectl", "config", "current-context"]:
            return _ok_proc(stdout="kind-axiom\n")
        return _ok_proc(stdout="")

    with patch("shutil.which", return_value="/bin/x"), \
         patch("subprocess.run", side_effect=fake_run):
        r = install.run({
            **_INTERNAL_PG,
            "namespace": "obs", "release": "obs-rel", "skip_diagnose": True,
            "salt": "s", "nextauth_secret": "n", "encryption_key": "e",
            "postgres_password": "p", "clickhouse_password": "c",
        }, skill_ctx)

    assert r.ok, r.errors
    assert r.value["release"] == "obs-rel"
    assert r.value["namespace"] == "obs"
    # The install skill must surface LANGFUSE_* env so the operator can
    # bind them into the Axiom process and the env-driven trace
    # provider picks Langfuse automatically.
    env = r.value.get("env", {})
    assert "LANGFUSE_HOST" in env
    assert env["LANGFUSE_HOST"].startswith("http")


def test_install_mints_secrets_when_not_supplied(skill_ctx):
    def fake_run(cmd, **kw):
        if cmd[:3] == ["kubectl", "config", "current-context"]:
            return _ok_proc(stdout="kind-axiom\n")
        return _ok_proc(stdout="")

    with patch("shutil.which", return_value="/bin/x"), \
         patch("subprocess.run", side_effect=fake_run):
        r = install.run({**_INTERNAL_PG, "skip_diagnose": True}, skill_ctx)

    assert r.ok, r.errors
    secrets = r.value.get("secrets", {})
    for k in ("salt", "nextauth_secret", "encryption_key",
              "postgres_password", "clickhouse_password"):
        assert secrets.get(k), f"{k} not minted"
        assert len(secrets[k]) >= 16


# ---------------------------------------------------------------------------
# Shared-Postgres (default) mode — schema=langfuse path
# ---------------------------------------------------------------------------


class TestSharedPostgresMode:
    """Default postgres_mode=external should reuse the axiom OLTP DB
    with schema=langfuse and refuse to proceed if no DSN is reachable.
    Per extension ADR-001 / ADR-052.
    """

    def _patch_subprocess(self):
        def fake_run(cmd, **kw):
            if cmd[:3] == ["kubectl", "config", "current-context"]:
                return _ok_proc(stdout="kind-axiom\n")
            return _ok_proc(stdout="")
        return patch("subprocess.run", side_effect=fake_run)

    def test_default_mode_fails_without_dsn(self, skill_ctx, monkeypatch):
        monkeypatch.delenv("DP1_RAG_DSN", raising=False)
        with patch("shutil.which", return_value="/bin/x"), self._patch_subprocess():
            r = install.run({"skip_diagnose": True}, skill_ctx)
        assert not r.ok
        assert any("shared PG DSN" in e or "DP1_RAG_DSN" in e for e in r.errors), r.errors

    def test_pg_dsn_param_takes_precedence(self, skill_ctx, monkeypatch):
        monkeypatch.delenv("DP1_RAG_DSN", raising=False)
        with patch("shutil.which", return_value="/bin/x"), \
             self._patch_subprocess(), \
             patch(
                 "axiom.extensions.builtins.observability.skills.install._ensure_schema_and_extension",
                 return_value=[],
             ):
            r = install.run({
                "skip_diagnose": True,
                "pg_dsn": "postgres://u:p@host:5432/axiom",
            }, skill_ctx)
        assert r.ok, r.errors
        # Schema gets appended to the DSN so Prisma stays out of `public`.
        actions = " ".join(r.actions_taken)
        assert "schema=langfuse" in actions

    def test_falls_back_to_dp1_rag_dsn_env(self, skill_ctx, monkeypatch):
        monkeypatch.setenv("DP1_RAG_DSN", "postgres://u:p@host:5432/axiom")
        with patch("shutil.which", return_value="/bin/x"), \
             self._patch_subprocess(), \
             patch(
                 "axiom.extensions.builtins.observability.skills.install._ensure_schema_and_extension",
                 return_value=[],
             ):
            r = install.run({"skip_diagnose": True}, skill_ctx)
        assert r.ok, r.errors

    def test_schema_extension_failure_propagates(self, skill_ctx):
        with patch("shutil.which", return_value="/bin/x"), \
             self._patch_subprocess(), \
             patch(
                 "axiom.extensions.builtins.observability.skills.install._ensure_schema_and_extension",
                 return_value=["pg connect refused"],
             ):
            r = install.run({
                "skip_diagnose": True,
                "pg_dsn": "postgres://u:p@bad:5432/axiom",
            }, skill_ctx)
        assert not r.ok
        assert any("pg connect refused" in e for e in r.errors)
