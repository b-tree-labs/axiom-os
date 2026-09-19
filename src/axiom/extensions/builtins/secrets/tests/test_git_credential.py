# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""git credential helper + ``wire-git`` convenience.

``axi secrets git-credential <get|store|erase>`` speaks the git
credential protocol (key=value lines on stdin/stdout) against the
foreign-credential store, so git never needs a plaintext token in
config or shell history. The subprocess layer runs the real CLI against
a fixture store (``file`` backend in an isolated AXI_STATE_DIR) — no
keychain involved.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

from axiom.extensions.builtins.secrets import skills as secrets_skills
from axiom.extensions.builtins.secrets.foreign.store import (
    ForeignCredentialStore,
)
from axiom.infra.skills import SkillContext

from .test_foreign_store import InMemoryValueStore


def _ctx(tmp_path, prompt=None):
    return SkillContext(
        registry=secrets_skills.bind_default(),
        state_dir=tmp_path,
        logger=logging.getLogger("test.gitcred"),
        user_prompt=prompt,
    )


@pytest.fixture
def store(tmp_path):
    s = ForeignCredentialStore(tmp_path, value_store=InMemoryValueStore())
    s.set(
        "hpc-gitlab-pat", b"glpat-VALUE",
        git_host="gitlab.example.org", git_username="oauth2",
    )
    return s


class TestGitCredentialSkill:
    def test_get_matches_host_and_emits_protocol_output(self, tmp_path, store):
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.git_credential", {
            "op": "get",
            "input": "protocol=https\nhost=gitlab.example.org\n\n",
            "_store": store,
        }, ctx)
        assert result.ok, result.errors
        out = result.value["output"]
        assert "username=oauth2\n" in out
        assert "password=glpat-VALUE\n" in out

    def test_get_unknown_host_emits_nothing(self, tmp_path, store):
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.git_credential", {
            "op": "get", "input": "protocol=https\nhost=other.example\n\n",
            "_store": store,
        }, ctx)
        assert result.ok
        assert result.value["output"] == ""

    def test_store_and_erase_are_acknowledged_noops(self, tmp_path, store):
        ctx = _ctx(tmp_path)
        for op in ("store", "erase"):
            result = ctx.registry.invoke("secrets.git_credential", {
                "op": op, "input": "host=gitlab.example.org\n\n",
                "_store": store,
            }, ctx)
            assert result.ok
            assert result.value["output"] == ""

    def test_bad_op_fails(self, tmp_path, store):
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.git_credential", {
            "op": "explode", "input": "", "_store": store,
        }, ctx)
        assert not result.ok


class TestWireGit:
    def test_wire_git_sets_mapping_and_writes_git_config(
        self, tmp_path, store
    ):
        ctx = _ctx(tmp_path)
        config_file = tmp_path / "gitconfig"
        result = ctx.registry.invoke("secrets.wire_git", {
            "name": "hpc-gitlab-pat", "host": "gitlab.example.org",
            "username": "oauth2", "config_file": str(config_file),
            "_store": store,
        }, ctx)
        assert result.ok, result.errors
        meta = store.metadata("hpc-gitlab-pat")
        assert meta["git_host"] == "gitlab.example.org"
        content = config_file.read_text(encoding="utf-8")
        assert "gitlab.example.org" in content
        assert "git-credential" in content

    def test_wire_git_unknown_name_fails(self, tmp_path, store):
        ctx = _ctx(tmp_path)
        result = ctx.registry.invoke("secrets.wire_git", {
            "name": "nope", "host": "h", "config_file": str(tmp_path / "gc"),
            "_store": store,
        }, ctx)
        assert not result.ok


# ---------------------------------------------------------------------------
# Subprocess layer — the real CLI against a fixture (file-backend) store
# ---------------------------------------------------------------------------

_MOD = "axiom.extensions.builtins.secrets.cli"


def _repo_src() -> str:
    return str(Path(__file__).resolve().parents[5])


def _run(args, *, env, stdin_text=""):
    child = os.environ.copy()
    child.update(env)
    src = _repo_src()
    existing = child.get("PYTHONPATH", "")
    child["PYTHONPATH"] = f"{src}{os.pathsep}{existing}" if existing else src
    return subprocess.run(
        [sys.executable, "-m", _MOD, *args],
        input=stdin_text, capture_output=True, text=True, timeout=30,
        env=child,
    )


class TestGitCredentialSubprocess:
    def test_get_over_real_cli_with_file_fixture_store(self, tmp_path):
        state = tmp_path / "state"
        env = {
            "AXI_STATE_DIR": str(state),
            "AXIOM_FOREIGN_SECRETS_BACKEND": "file",
            "AXIOM_MODE": "dev",
            "AXIOM_ACTION_LEDGER_BACKEND": "jsonl",
        }
        # Seed via the same store the CLI will open (file backend).
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            store = ForeignCredentialStore(state)
            store.set(
                "fixture-pat", b"fixture-password",
                git_host="git.fixture.example", git_username="pat",
            )
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        cp = _run(
            ["git-credential", "get"], env=env,
            stdin_text="protocol=https\nhost=git.fixture.example\n\n",
        )
        assert cp.returncode == 0, cp.stderr
        assert "username=pat" in cp.stdout
        assert "password=fixture-password" in cp.stdout
        # Raw protocol output only — no JSON wrapper, no bullets.
        assert not cp.stdout.strip().startswith("{")
        assert "•" not in cp.stdout

    def test_get_unknown_host_exits_zero_silent(self, tmp_path):
        env = {
            "AXI_STATE_DIR": str(tmp_path / "state"),
            "AXIOM_FOREIGN_SECRETS_BACKEND": "file",
            "AXIOM_MODE": "dev",
            "AXIOM_ACTION_LEDGER_BACKEND": "jsonl",
        }
        cp = _run(
            ["git-credential", "get"], env=env,
            stdin_text="protocol=https\nhost=nowhere.example\n\n",
        )
        assert cp.returncode == 0, cp.stderr
        assert cp.stdout.strip() == ""
