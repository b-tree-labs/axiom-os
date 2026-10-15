# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One registrar matrix — the memory server registers through every row of
``mcp/install.py::TOOL_SPECS`` and nowhere else.

Two matrices used to exist: ``TOOL_SPECS`` for the aggregation server and
``TOOL_REGISTRARS`` for the memory server, each with its own detection,
config paths and file writers. They drifted (windsurf on one side, openclaw
on the other, gemini and opencode stubbed on one side, OpenCode's format
wrong on the other). These tests pin the collapse:

- every ``TOOL_SPECS`` row has a memory registrar, and every memory
  registrar (bar the documented ``REGISTRAR_ONLY`` set) has a spec row;
- registering the memory server writes the spec's shape, ``is_registered``
  reads it back from the same file, re-registering is idempotent, and a
  user's other servers and settings survive;
- a config that cannot be parsed is never rewritten;
- ``register_all_detected`` reports every outcome instead of aborting; and
- the interpreter the memory server is registered with is resolved the
  same way as the aggregation server's (install-mode parity: ``-m``, no
  path into a checkout).

Every test runs under a throwaway HOME so the developer's real IDE configs
are never read or written.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from axiom.extensions.builtins.mcp import install as inst
from axiom.extensions.builtins.memory import register_mcp as reg

yaml = pytest.importorskip("yaml")

MEMORY_ARGS = ["-m", "axiom.extensions.builtins.memory.mcp_server"]
SPEC_TOOLS = [s.name for s in inst.TOOL_SPECS]
GARBAGE = "{ [ } ]\n"  # rejected by json, tomlkit and yaml alike


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway HOME: no harness on PATH, no service venv, every spec's
    config path redirected under it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setenv("PATH", str(home / "empty-bin"))
    for spec in inst.TOOL_SPECS:
        monkeypatch.setenv(spec.env_override, str(home / "cfg" / f"{spec.name}.cfg"))
    return home


def _config(spec: inst.ToolSpec) -> Path:
    return inst.config_path_for(spec)


def _load(spec: inst.ToolSpec, path: Path) -> dict[str, Any]:
    """The raw document, parsed with a *different* parser than the writer
    where one exists (tomllib vs tomlkit) so the on-disk bytes are what is
    asserted, not the writer's own view of them."""
    text = path.read_text()
    if spec.fmt == "codex_toml":
        return tomllib.loads(text)
    if spec.fmt == "hermes_yaml":
        return yaml.safe_load(text)
    return json.loads(text)


def _servers(spec: inst.ToolSpec, path: Path) -> dict[str, Any]:
    return _load(spec, path).get(inst.SERVERS_KEY_BY_FMT[spec.fmt]) or {}


def _seed(spec: inst.ToolSpec, path: Path) -> None:
    """A pre-existing config with an unrelated server and an unrelated
    top-level setting, in the spec's own format."""
    key = inst.SERVERS_KEY_BY_FMT[spec.fmt]
    path.parent.mkdir(parents=True, exist_ok=True)
    if spec.fmt == "codex_toml":
        path.write_text(
            'unrelated = "kept"\n\n[mcp_servers.other]\ncommand = "/bin/other"\nargs = ["x"]\n'
        )
        return
    other: dict[str, Any] = {"command": "/bin/other", "args": ["x"]}
    if spec.fmt == "opencode_json":
        other = {"type": "local", "command": ["/bin/other", "x"]}
    elif spec.fmt == "vscode_json":
        other = {"type": "stdio", **other}
    doc = {"unrelated": "kept", key: {"other": other}}
    if spec.fmt == "hermes_yaml":
        path.write_text(yaml.safe_dump(doc))
    else:
        path.write_text(json.dumps(doc, indent=2))


# ---------------------------------------------------------------------------
# Parity: the two registries can never drift again
# ---------------------------------------------------------------------------


def test_registrar_matrix_parity_with_tool_specs():
    spec_names = {s.name for s in inst.TOOL_SPECS}
    registrar_names = set(reg.TOOL_REGISTRARS)

    assert registrar_names - reg.REGISTRAR_ONLY == spec_names
    assert registrar_names >= reg.REGISTRAR_ONLY
    assert not (reg.REGISTRAR_ONLY & spec_names)

    for name in spec_names:
        r = reg.TOOL_REGISTRARS[name]
        assert r.spec is inst.spec_for(name), name
        assert r.detect is r.spec.detect, name  # detection is the spec's rule, not a copy
        assert r.mechanism == "file", name
    for name in reg.REGISTRAR_ONLY:
        assert reg.TOOL_REGISTRARS[name].spec is None
        assert reg.TOOL_REGISTRARS[name].mechanism == "cli"


def test_registrar_order_follows_the_matrix():
    assert list(reg.TOOL_REGISTRARS)[: len(inst.TOOL_SPECS)] == SPEC_TOOLS


def test_every_spec_row_has_named_register_and_is_registered_functions():
    for spec in inst.TOOL_SPECS:
        ident = spec.name.replace("-", "_")
        register = getattr(reg, f"register_{ident}_mcp")
        check = getattr(reg, f"is_{ident}_mcp_registered")
        assert reg.TOOL_REGISTRARS[spec.name].register is register
        assert reg.TOOL_REGISTRARS[spec.name].is_registered is check
    # Claude Code keeps its historical names too.
    assert reg.register_claude_code_mcp is reg.register_axiom_memory_mcp
    assert reg.is_claude_code_mcp_registered is reg.is_axiom_memory_mcp_registered


def test_a_registrar_cannot_exist_for_a_tool_the_matrix_does_not_know():
    with pytest.raises(KeyError):
        reg._spec_registrar("not-a-harness")


def test_hermes_is_fully_supported_not_detection_only():
    """Its format is known (``~/.hermes/config.yaml`` -> ``mcp_servers``),
    so hermes is a file-backed row, not a detection-only placeholder."""
    r = reg.TOOL_REGISTRARS["hermes"]
    assert r.mechanism == "file"
    assert r.spec is not None and r.spec.fmt == "hermes_yaml"
    assert inst.SERVERS_KEY_BY_FMT["hermes_yaml"] == "mcp_servers"


# ---------------------------------------------------------------------------
# Every row: write, read back, idempotent, preserve, refuse malformed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool", SPEC_TOOLS)
def test_register_writes_the_spec_shape(isolated_home: Path, tool: str):
    r = reg.TOOL_REGISTRARS[tool]
    spec = r.spec
    assert spec is not None

    result = r.register(python_path="/p/python")

    assert result["action"] == "added"
    assert result["tool"] == tool
    assert result["command"] == "/p/python"
    path = Path(result["config_path"])
    assert path == _config(spec)
    assert str(path).startswith(str(isolated_home))

    doc = _load(spec, path)
    assert set(doc) == {inst.SERVERS_KEY_BY_FMT[spec.fmt]}
    entry = doc[inst.SERVERS_KEY_BY_FMT[spec.fmt]]["axiom-memory"]
    if spec.fmt == "opencode_json":
        assert entry["type"] == "local"
        assert entry["command"] == ["/p/python", *MEMORY_ARGS]
        assert entry["enabled"] is True
        assert "environment" not in entry  # memory server carries no env
    else:
        assert entry["command"] == "/p/python"
        assert list(entry["args"]) == MEMORY_ARGS
    if spec.fmt == "vscode_json" or spec.include_type:
        assert entry["type"] == "stdio"
    if spec.fmt == "mcp_json" and not spec.include_type:
        assert "type" not in entry
    if spec.fmt in ("mcp_json", "vscode_json"):
        assert entry["env"] == {}
    if spec.fmt == "hermes_yaml":
        assert entry["enabled"] is True
    if spec.fmt == "codex_toml":
        assert "env" not in entry


@pytest.mark.parametrize("tool", SPEC_TOOLS)
def test_is_registered_reads_back_the_file_the_spec_wrote(isolated_home: Path, tool: str):
    r = reg.TOOL_REGISTRARS[tool]
    assert r.spec is not None

    before = r.is_registered()
    assert before["registered"] is False
    assert before["reason"] == "missing"
    assert before["tool"] == tool

    r.register(python_path="/p/python")

    after = r.is_registered(expected_command="/p/python")
    assert after == {
        "registered": True,
        "command": "/p/python",
        "stale": False,
        "config_path": str(_config(r.spec)),
        "tool": tool,
    }
    stale = r.is_registered(expected_command="/other/python")
    assert stale["registered"] is True
    assert stale["stale"] is True


@pytest.mark.parametrize("tool", SPEC_TOOLS)
def test_reregister_is_idempotent_and_never_duplicates(isolated_home: Path, tool: str):
    r = reg.TOOL_REGISTRARS[tool]
    assert r.spec is not None
    path = _config(r.spec)

    assert r.register(python_path="/p/python")["action"] == "added"
    first = path.read_text()
    assert r.register(python_path="/p/python")["action"] == "unchanged"
    assert path.read_text() == first
    assert list(_servers(r.spec, path)) == ["axiom-memory"]

    assert r.register(python_path="/new/python")["action"] == "updated"
    assert list(_servers(r.spec, path)) == ["axiom-memory"]
    assert r.is_registered()["command"] == "/new/python"


@pytest.mark.parametrize("tool", SPEC_TOOLS)
def test_unrelated_servers_and_settings_survive(isolated_home: Path, tool: str):
    r = reg.TOOL_REGISTRARS[tool]
    assert r.spec is not None
    path = _config(r.spec)
    _seed(r.spec, path)

    assert r.register(python_path="/p/python")["action"] == "added"

    doc = _load(r.spec, path)
    assert doc["unrelated"] == "kept"
    servers = doc[inst.SERVERS_KEY_BY_FMT[r.spec.fmt]]
    assert set(servers) == {"other", "axiom-memory"}
    assert "/bin/other" in json.dumps(servers["other"])


@pytest.mark.parametrize("tool", SPEC_TOOLS)
def test_malformed_config_is_never_rewritten(isolated_home: Path, tool: str):
    r = reg.TOOL_REGISTRARS[tool]
    assert r.spec is not None
    path = _config(r.spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(GARBAGE)

    with pytest.raises(ValueError):
        r.register(python_path="/p/python")
    assert path.read_text() == GARBAGE

    status = r.is_registered()
    assert status["registered"] is False
    assert status["reason"] == "malformed_config"
    assert status["tool"] == tool


# ---------------------------------------------------------------------------
# Detection follows the spec's rule
# ---------------------------------------------------------------------------


def test_detection_is_the_spec_rule(isolated_home: Path):
    """Config-dir detection for the rows whose rule is a home-relative dir
    (the GUI rows also accept an absolute app bundle, which a throwaway
    HOME cannot isolate, so they are not asserted False here)."""
    dir_based = {
        "claude-code": ".claude",
        "codex": ".codex",
        "gemini": ".gemini",
        "hermes": ".hermes",
        "opencode": ".config/opencode",
        "windsurf": ".codeium/windsurf",
    }
    before = reg.detect_installed_tools()
    assert set(before) == set(reg.TOOL_REGISTRARS)
    for tool in dir_based:
        assert before[tool] is False, tool

    for rel in dir_based.values():
        (isolated_home / rel).mkdir(parents=True)

    after = reg.detect_installed_tools()
    for tool in dir_based:
        assert after[tool] is True, tool


# ---------------------------------------------------------------------------
# One command registers every detected harness, reporting each outcome
# ---------------------------------------------------------------------------


def test_register_all_detected_reports_supported_and_unsupported(isolated_home: Path):
    detected = dict.fromkeys(reg.TOOL_REGISTRARS, True)

    results = reg.register_all_detected("/p/python", detected=detected)

    assert set(results) == set(reg.TOOL_REGISTRARS)
    for tool in SPEC_TOOLS:
        assert results[tool]["action"] == "added", results[tool]
        assert results[tool]["command"] == "/p/python"
        assert reg.TOOL_REGISTRARS[tool].is_registered()["registered"] is True
    # OpenClaw is CLI-driven and its binary is not on this PATH: a reported
    # outcome, not an exception, and not a "stub".
    assert results["openclaw"]["action"] == "failed"
    assert "not_installed" in results["openclaw"]["reason"]
    assert not any(r["action"] == "stub" for r in results.values())


def test_register_all_detected_skips_undetected(isolated_home: Path):
    results = reg.register_all_detected("/p/python", detected={})

    assert set(results) == set(reg.TOOL_REGISTRARS)
    for r in results.values():
        assert r["action"] == "skipped"
        assert r["reason"] == "not_detected"
    for spec in inst.TOOL_SPECS:
        assert not _config(spec).exists()


def test_register_all_detected_isolates_one_malformed_config(isolated_home: Path):
    cursor = _config(inst.spec_for("cursor"))
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text(GARBAGE)

    results = reg.register_all_detected("/p/python", detected=dict.fromkeys(SPEC_TOOLS, True))

    assert results["cursor"]["action"] == "failed"
    assert results["cursor"]["reason"].startswith("JSONDecodeError")  # a ValueError, named
    assert cursor.read_text() == GARBAGE
    for tool in SPEC_TOOLS:
        if tool != "cursor":
            assert results[tool]["action"] == "added", tool


# ---------------------------------------------------------------------------
# Install-mode parity: PyPI and editable resolve to the same registration
# ---------------------------------------------------------------------------


def test_memory_server_resolution_matches_the_aggregation_server(
    isolated_home: Path,
    monkeypatch,
):
    monkeypatch.delenv("AXIOM_MCP_SERVER_MODULE", raising=False)
    monkeypatch.delenv("AXIOM_MCP_SERVER_NAME", raising=False)

    name, command, args = reg.resolve_memory_server()
    _, agg_command, agg_args = inst.resolve_server()

    assert name == "axiom-memory"
    # No service venv under this home: both servers resolve to this interpreter.
    assert command == agg_command == sys.executable
    assert args == MEMORY_ARGS
    assert agg_args[0] == args[0] == "-m"
    # ``-m`` resolves through the interpreter: no path into a checkout, so a
    # PyPI wheel and an editable install register byte-identically.
    assert not any(Path(a).is_absolute() for a in args)
    assert importlib.util.find_spec(args[1]) is not None


def test_memory_server_resolution_prefers_the_pinned_service_venv(isolated_home: Path):
    pinned = isolated_home / ".axi" / "service-venv" / "bin" / "python"
    pinned.parent.mkdir(parents=True)
    pinned.write_text("#!/bin/sh\n")
    pinned.chmod(0o755)

    assert reg.resolve_memory_server()[1] == str(pinned)
    assert reg.resolve_memory_server("/explicit/python")[1] == "/explicit/python"

    # Registrars and the sweep write that same interpreter by default, and
    # a check against the resolver never calls it stale.
    r = reg.TOOL_REGISTRARS["cursor"]
    assert r.register()["command"] == str(pinned)
    assert r.is_registered(expected_command=reg.resolve_memory_server()[1])["stale"] is False
    results = reg.register_all_detected(detected={"gemini": True})
    assert results["gemini"]["command"] == str(pinned)


def test_every_registered_command_is_the_resolved_interpreter(isolated_home: Path):
    results = reg.register_all_detected(detected=dict.fromkeys(SPEC_TOOLS, True))
    for tool in SPEC_TOOLS:
        assert results[tool]["command"] == sys.executable, tool
        entry = inst.read_entry(inst.spec_for(tool), "axiom-memory")
        assert entry is not None
        assert entry["command"] == sys.executable
        assert entry["args"] == MEMORY_ARGS


# ---------------------------------------------------------------------------
# CLI: one command, every matrix row
# ---------------------------------------------------------------------------


def test_cli_register_mcp_all_reaches_every_matrix_row(isolated_home: Path, monkeypatch, capsys):
    from axiom.extensions.builtins.memory import cli

    monkeypatch.setattr(
        reg,
        "detect_installed_tools",
        lambda: dict.fromkeys(SPEC_TOOLS, True),
    )

    rc = cli.main(["register-mcp", "--all", "--json"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    for tool in SPEC_TOOLS:
        assert payload[tool]["action"] == "added", payload[tool]
    assert payload["openclaw"]["action"] == "skipped"

    # Everything registered against the resolver: --check --all is clean
    # (rows not detected under this HOME are skipped, not failed).
    rc = cli.main(["register-mcp", "--all", "--check", "--json"])
    assert rc == 0


def test_cli_register_mcp_tool_hermes(isolated_home: Path, capsys):
    from axiom.extensions.builtins.memory import cli

    rc = cli.main(["register-mcp", "--tool", "hermes", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "added"
    assert payload["config_path"] == str(_config(inst.spec_for("hermes")))
    assert yaml.safe_load(Path(payload["config_path"]).read_text())["mcp_servers"]["axiom-memory"]

    assert cli.main(["register-mcp", "--tool", "hermes", "--check"]) == 0
