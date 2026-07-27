# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for ``axi connect <preset>`` framework — Item 8.

The connect-preset framework gives a one-line ``axi connect <preset>`` that
wires both an LLM endpoint and a RAG pack server in one go. Tests stub the
HTTP probe via an injectable runner so we can drive every flow offline.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.cli import connect as connect_cli

# ---------------------------------------------------------------------------
# Stubs / fixtures
# ---------------------------------------------------------------------------


class _StubProber:
    """Records probe calls + returns canned (status_ok, latency_ms, message) tuples.

    Maps ``endpoint+path`` keys to a probe outcome. Default: reachable.
    """

    def __init__(self, *, default_ok: bool = True, results: dict | None = None) -> None:
        self._default_ok = default_ok
        self._results = results or {}
        self.calls: list[str] = []

    def __call__(self, url: str, *, timeout: float = 5.0) -> connect_cli.ProbeResult:
        self.calls.append(url)
        if url in self._results:
            ok, msg = self._results[url]
            return connect_cli.ProbeResult(ok=ok, latency_ms=12.3, message=msg)
        return connect_cli.ProbeResult(
            ok=self._default_ok,
            latency_ms=12.3,
            message="ok" if self._default_ok else "unreachable",
        )


def _write_preset_file(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


_BUILTIN_PRESET_TOML = """
[[connect.preset]]
name = "axiom-internal-test"
description = "Built-in self-test preset (no external deps)"
discovery_hint = "Used by axiom tests; not intended for production"

[[connect.preset.providers]]
kind = "llm"
provider_name = "internal-test-llm"
endpoint = "${AXIOM_TEST_LLM_ENDPOINT}"
api_key_env = "AXIOM_TEST_LLM_KEY"
model = "test-model"
routing_tier = "any"
routing_tags = ["internal_test"]
probe_path = "/v1/models"

[[connect.preset.providers]]
kind = "rag"
endpoint = "${AXIOM_TEST_RAG_ENDPOINT}"
probe_path = "/v1/info"
"""


_EXTENSION_PRESET_TOML = """
[extension]
name = "fake-extension"
version = "0.1.0"
description = "Fake extension for connect-preset tests"

[[connect.preset]]
name = "fake-extension-preset"
description = "Provided by an extension manifest"

[[connect.preset.providers]]
kind = "llm"
provider_name = "fake-ext-llm"
endpoint = "${FAKE_EXT_LLM_ENDPOINT}"
model = "fake-model"
routing_tier = "any"
probe_path = "/v1/models"

[[connect.preset.providers]]
kind = "rag"
endpoint = "${FAKE_EXT_RAG_ENDPOINT}"
probe_path = "/v1/info"
"""


@pytest.fixture
def runtime_root(tmp_path, monkeypatch) -> Path:
    """Set up a runtime config layout under tmp_path; chdir there.

    Builds:
      tmp_path/runtime/config/                   (empty — output target)
      tmp_path/runtime/config.example/connect-presets.toml  (built-in source)
    """
    config_dir = tmp_path / "runtime" / "config"
    config_dir.mkdir(parents=True)
    example_dir = tmp_path / "runtime" / "config.example"
    example_dir.mkdir(parents=True)
    _write_preset_file(example_dir / "connect-presets.toml", _BUILTIN_PRESET_TOML)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AXIOM_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def extension_root(tmp_path) -> Path:
    """Lay out a fake extension manifest under tmp_path/exts/fake-extension."""
    ext_dir = tmp_path / "exts" / "fake-extension"
    ext_dir.mkdir(parents=True)
    _write_preset_file(ext_dir / "axiom-extension.toml", _EXTENSION_PRESET_TOML)
    return tmp_path / "exts"


@pytest.fixture
def deps(runtime_root, extension_root) -> connect_cli.ConnectCliDeps:
    """Wire deps: built-in preset file + extension search dir + stub prober."""
    return connect_cli.ConnectCliDeps(
        runtime_config_dir=runtime_root / "runtime" / "config",
        builtin_presets_path=runtime_root / "runtime" / "config.example" / "connect-presets.toml",
        extension_search_dirs=(extension_root,),
        prober=_StubProber(default_ok=True),
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Reset preset-related env vars between tests."""
    for var in (
        "AXIOM_TEST_LLM_ENDPOINT",
        "AXIOM_TEST_LLM_KEY",
        "AXIOM_TEST_RAG_ENDPOINT",
        "FAKE_EXT_LLM_ENDPOINT",
        "FAKE_EXT_RAG_ENDPOINT",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# axi connect list
# ---------------------------------------------------------------------------


class TestList:
    def test_list_shows_builtin_preset(self, deps, capsys):
        rc = connect_cli.cmd_list(argv=[], deps=deps)
        assert rc == 0
        out = capsys.readouterr().out
        assert "axiom-internal-test" in out

    def test_list_shows_extension_preset(self, deps, capsys):
        rc = connect_cli.cmd_list(argv=[], deps=deps)
        assert rc == 0
        out = capsys.readouterr().out
        assert "fake-extension-preset" in out

    def test_list_shows_descriptions(self, deps, capsys):
        rc = connect_cli.cmd_list(argv=[], deps=deps)
        assert rc == 0
        out = capsys.readouterr().out
        # Either source's description should land in the listing
        assert "self-test" in out.lower() or "extension" in out.lower()

    def test_list_with_no_presets_is_clean(self, tmp_path, capsys):
        empty_deps = connect_cli.ConnectCliDeps(
            runtime_config_dir=tmp_path / "rc",
            builtin_presets_path=tmp_path / "missing.toml",
            extension_search_dirs=(),
            prober=_StubProber(),
        )
        rc = connect_cli.cmd_list(argv=[], deps=empty_deps)
        assert rc == 0
        out = capsys.readouterr().out
        assert "no presets" in out.lower() or "(empty)" in out.lower()


# ---------------------------------------------------------------------------
# axi connect <preset> --dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_dry_run_prints_config_and_does_not_write(
        self, deps, runtime_root, capsys, monkeypatch
    ):
        monkeypatch.setenv("AXIOM_TEST_LLM_ENDPOINT", "https://llm.example/v1")
        monkeypatch.setenv("AXIOM_TEST_LLM_KEY", "secret")
        monkeypatch.setenv("AXIOM_TEST_RAG_ENDPOINT", "https://rag.example")

        rc = connect_cli.cmd_apply(
            argv=["axiom-internal-test", "--dry-run"],
            deps=deps,
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert "https://llm.example/v1" in out
        assert "https://rag.example" in out

        # No files written
        assert not (runtime_root / "runtime" / "config" / "llm-providers.toml").exists()
        assert not (runtime_root / "runtime" / "config" / "rag-packs.toml").exists()


# ---------------------------------------------------------------------------
# axi connect <preset>
# ---------------------------------------------------------------------------


class TestApply:
    def test_apply_writes_both_config_files(self, deps, runtime_root, monkeypatch):
        monkeypatch.setenv("AXIOM_TEST_LLM_ENDPOINT", "https://llm.example/v1")
        monkeypatch.setenv("AXIOM_TEST_LLM_KEY", "secret")
        monkeypatch.setenv("AXIOM_TEST_RAG_ENDPOINT", "https://rag.example")

        rc = connect_cli.cmd_apply(
            argv=["axiom-internal-test"],
            deps=deps,
        )
        assert rc == 0
        llm_path = runtime_root / "runtime" / "config" / "llm-providers.toml"
        rag_path = runtime_root / "runtime" / "config" / "rag-packs.toml"
        assert llm_path.exists()
        assert rag_path.exists()
        llm_text = llm_path.read_text()
        assert "internal-test-llm" in llm_text
        assert "https://llm.example/v1" in llm_text
        rag_text = rag_path.read_text()
        assert "https://rag.example" in rag_text

    def test_apply_probes_each_endpoint(self, deps, monkeypatch):
        monkeypatch.setenv("AXIOM_TEST_LLM_ENDPOINT", "https://llm.example/v1")
        monkeypatch.setenv("AXIOM_TEST_RAG_ENDPOINT", "https://rag.example")
        rc = connect_cli.cmd_apply(argv=["axiom-internal-test"], deps=deps)
        assert rc == 0
        # Two probes: llm + rag, each with their probe_path appended
        prober = deps.prober
        assert any("llm.example" in c and "/v1/models" in c for c in prober.calls)
        assert any("rag.example" in c and "/v1/info" in c for c in prober.calls)

    def test_apply_with_no_test_skips_probe(self, runtime_root, extension_root, monkeypatch):
        prober = _StubProber(default_ok=False)
        deps = connect_cli.ConnectCliDeps(
            runtime_config_dir=runtime_root / "runtime" / "config",
            builtin_presets_path=runtime_root / "runtime" / "config.example"
            / "connect-presets.toml",
            extension_search_dirs=(extension_root,),
            prober=prober,
        )
        monkeypatch.setenv("AXIOM_TEST_LLM_ENDPOINT", "https://llm.example/v1")
        monkeypatch.setenv("AXIOM_TEST_RAG_ENDPOINT", "https://rag.example")

        rc = connect_cli.cmd_apply(
            argv=["axiom-internal-test", "--no-test"],
            deps=deps,
        )
        assert rc == 0
        # Files written despite unreachable endpoints
        assert (runtime_root / "runtime" / "config" / "llm-providers.toml").exists()
        # Prober was not called
        assert prober.calls == []

    def test_apply_unreachable_fails_and_writes_nothing(
        self, runtime_root, extension_root, capsys, monkeypatch
    ):
        prober = _StubProber(default_ok=False)
        deps = connect_cli.ConnectCliDeps(
            runtime_config_dir=runtime_root / "runtime" / "config",
            builtin_presets_path=runtime_root / "runtime" / "config.example"
            / "connect-presets.toml",
            extension_search_dirs=(extension_root,),
            prober=prober,
        )
        monkeypatch.setenv("AXIOM_TEST_LLM_ENDPOINT", "https://llm.example/v1")
        monkeypatch.setenv("AXIOM_TEST_RAG_ENDPOINT", "https://rag.example")

        rc = connect_cli.cmd_apply(argv=["axiom-internal-test"], deps=deps)
        assert rc != 0
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert (
            "unreachable" in combined.lower()
            or "could not reach" in combined.lower()
            or "failed" in combined.lower()
        )
        # No partial writes
        assert not (runtime_root / "runtime" / "config" / "llm-providers.toml").exists()
        assert not (runtime_root / "runtime" / "config" / "rag-packs.toml").exists()

    def test_apply_unknown_preset_errors_cleanly(self, deps, capsys):
        rc = connect_cli.cmd_apply(argv=["does-not-exist"], deps=deps)
        assert rc != 0
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        # Helpful: tells user the preset wasn't found
        assert "does-not-exist" in combined or "not found" in combined.lower()
        assert rc == 2 or rc == 1

    def test_apply_missing_env_var_errors_with_clear_message(
        self, deps, runtime_root, capsys
    ):
        # Don't set env vars — expansion should fail with a clear message
        rc = connect_cli.cmd_apply(argv=["axiom-internal-test"], deps=deps)
        assert rc != 0
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "AXIOM_TEST_LLM_ENDPOINT" in combined
        # Nothing written
        assert not (runtime_root / "runtime" / "config" / "llm-providers.toml").exists()


# ---------------------------------------------------------------------------
# axi connect status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_status_with_no_config_is_graceful(self, deps, capsys):
        rc = connect_cli.cmd_status(argv=[], deps=deps)
        assert rc == 0
        out = capsys.readouterr().out
        # Reports there is no llm-providers.toml or no providers
        assert "no" in out.lower() or "not configured" in out.lower() or "(empty)" in out.lower()

    def test_status_after_apply_shows_endpoints(
        self, deps, runtime_root, capsys, monkeypatch
    ):
        monkeypatch.setenv("AXIOM_TEST_LLM_ENDPOINT", "https://llm.example/v1")
        monkeypatch.setenv("AXIOM_TEST_RAG_ENDPOINT", "https://rag.example")
        connect_cli.cmd_apply(argv=["axiom-internal-test"], deps=deps)
        capsys.readouterr()  # drain apply's output

        rc = connect_cli.cmd_status(argv=[], deps=deps)
        assert rc == 0
        out = capsys.readouterr().out
        assert "https://llm.example/v1" in out
        assert "https://rag.example" in out

    def test_status_runs_health_probe(self, deps, runtime_root, monkeypatch):
        monkeypatch.setenv("AXIOM_TEST_LLM_ENDPOINT", "https://llm.example/v1")
        monkeypatch.setenv("AXIOM_TEST_RAG_ENDPOINT", "https://rag.example")
        connect_cli.cmd_apply(argv=["axiom-internal-test"], deps=deps)

        # Reset call log
        deps.prober.calls.clear()

        rc = connect_cli.cmd_status(argv=[], deps=deps)
        assert rc == 0
        # Status should re-probe each currently-configured endpoint
        assert len(deps.prober.calls) >= 1


# ---------------------------------------------------------------------------
# Top-level dispatcher routes preset commands; falls through to legacy connect
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_main_list_routes_to_preset_handler(self, deps, capsys, monkeypatch):
        # Inject deps via factory monkeypatch
        monkeypatch.setattr(connect_cli, "_build_default_deps", lambda: deps)
        rc = connect_cli.main(["list"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "axiom-internal-test" in out

    def test_main_falls_through_to_legacy_for_unknown_first_arg(self, monkeypatch):
        """Args that aren't preset subcommands or preset names go to legacy connect."""
        called = {"value": False}

        def _legacy_main(argv: list[str] | None = None) -> int:
            called["value"] = True
            assert argv == ["--check", "--json"]
            return 0

        monkeypatch.setattr(connect_cli, "_legacy_connect_main", _legacy_main)
        rc = connect_cli.main(["--check", "--json"])
        assert rc == 0
        assert called["value"]


# ---------------------------------------------------------------------------
# Env-var resolution helper
# ---------------------------------------------------------------------------


class TestExpandEnv:
    def test_expand_env_replaces_var(self, monkeypatch):
        monkeypatch.setenv("FOO", "bar")
        assert connect_cli._expand_env("${FOO}/path") == "bar/path"

    def test_expand_env_missing_raises(self):
        with pytest.raises(connect_cli.ConnectCliError) as excinfo:
            connect_cli._expand_env("${THIS_DOES_NOT_EXIST_XYZ}")
        assert "THIS_DOES_NOT_EXIST_XYZ" in str(excinfo.value)

    def test_expand_env_no_vars_passthrough(self):
        assert connect_cli._expand_env("plain string") == "plain string"

    def test_expand_env_handles_none_and_empty(self):
        assert connect_cli._expand_env("") == ""
        assert connect_cli._expand_env(None) == ""
