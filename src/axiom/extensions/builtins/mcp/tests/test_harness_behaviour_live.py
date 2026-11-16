# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Drive the real harnesses against the real Axiom server (opt-in).

``harness_profiles.toml`` records what was measured on one day. This is how it
is re-measured: for each harness installed on the machine, point it at the
Axiom MCP server and check the two behaviours the collision rules protect.

1. A slash line the harness reserves (``/resume`` and friends) must not cause
   an Axiom tool call. Where the profile says the harness handles it itself,
   the harness must also have answered without a model turn.
2. A natural-language request that plainly wants an Axiom read tool must reach
   it. Run N times (``AXIOM_HARNESS_SAMPLES``, default 3); the rate is
   reported, and the test fails only at a rate of zero, because a model is not
   deterministic and one miss is not a defect.

Deselected by default (the ``integration`` marker, see ``addopts``). Run with
``pytest -m integration <this file>``. A harness that is absent, broken, or has
no credentials is skipped with the reason.

Isolation: the harness is given a temp working directory and a temp MCP config
passed by flag; the server it spawns gets a temp HOME/AXIOM_HOME, the JSONL
action ledger, and no database URL, so nothing here can reach the default
Postgres or the real ledger. Claude Code is driven with ``--no-session-persistence``
and ``--strict-mcp-config`` (its login lives in the user's keychain, so HOME
cannot be swapped without losing it); OpenCode gets temp XDG dirs. No credential
is read, logged or placed in the server's environment. The only read-only tool
the model is allowed to use is ``axiom_mcp__client_capabilities``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.timeout(600)]

SRC = Path(__file__).resolve().parents[5]  # .../src
SAMPLES = max(1, int(os.environ.get("AXIOM_HARNESS_SAMPLES", "3")))
READ_TOOL = "axiom_mcp__client_capabilities"
NATURAL_REQUEST = (
    "Use the Axiom MCP tools (not the shell) to find out which agent harnesses "
    "Axiom supports and which can be routed to an export-controlled model. "
    "Call the tool, then answer in one sentence."
)
#: snapshot before any fixture redirects HOME: the harness needs its own login
_REAL_ENV = dict(os.environ)


def _scrubbed(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in _REAL_ENV.items() if k not in ("AXIOM_DB_URL", "DATABASE_URL")}
    env["AXIOM_ACTION_LEDGER_BACKEND"] = "jsonl"
    env.update(extra or {})
    return env


def _server_entry(state: Path) -> dict:
    """The Axiom server as a harness would launch it, in a throwaway state dir."""
    home = state / "axiom-home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "command": sys.executable,
        "args": ["-m", "axiom.extensions.builtins.mcp.server"],
        "env": {
            "PYTHONPATH": str(SRC),
            "HOME": str(state),
            "AXIOM_HOME": str(home),
            "AXI_STATE_DIR": str(state / "state"),
            "AXIOM_ACTION_LEDGER_BACKEND": "jsonl",
            "PATH": os.environ.get("PATH", ""),
        },
    }


def _run(cmd: list[str], env: dict[str, str], cwd: Path, timeout: int = 240):
    return subprocess.run(
        cmd, env=env, cwd=cwd, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
    )


# ---------------------------------------------------------------------------
# One driver per harness: returns (axiom_tools_called, handled_without_model)
# ---------------------------------------------------------------------------


def _claude(line: str, state: Path):
    cfg = state / "claude-mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"axiom": _server_entry(state)}}))
    cmd = [
        "claude", "-p", line, "--output-format", "stream-json", "--verbose",
        "--mcp-config", str(cfg), "--strict-mcp-config", "--no-session-persistence",
        "--model", "haiku",
        "--allowedTools", f"mcp__axiom__{READ_TOOL}",
        "--permission-mode", "default",
    ]
    out = _run(cmd, _scrubbed(), state)
    called: list[str] = []
    synthetic_only = True
    saw_assistant = False
    for raw in out.stdout.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if ev.get("type") != "assistant":
            continue
        saw_assistant = True
        msg = ev.get("message") or {}
        if msg.get("model") != "<synthetic>":
            synthetic_only = False
        for block in msg.get("content") or []:
            if block.get("type") == "tool_use" and str(block.get("name", "")).startswith("mcp__axiom"):
                called.append(block["name"])
    return called, (saw_assistant and synthetic_only), out


def _opencode(line: str, state: Path):
    xdg = {f"XDG_{k}_HOME": str(state / k.lower()) for k in ("CONFIG", "DATA", "STATE", "CACHE")}
    cfg = state / "opencode.json"
    entry = _server_entry(state)
    cfg.write_text(json.dumps({
        "$schema": "https://opencode.ai/config.json",
        "mcp": {"axiom": {
            "type": "local",
            "command": [entry["command"], *entry["args"]],
            "environment": entry["env"],
            "enabled": True,
        }},
        "permission": {"*": "deny", f"axiom_{READ_TOOL}": "allow"},
    }))
    env = _scrubbed({**xdg, "OPENCODE_CONFIG": str(cfg)})
    cmd = ["opencode", "run", "--format", "json", "-m", "anthropic/claude-haiku-4-5", line]
    out = _run(cmd, env, state)
    called = []
    for raw in out.stdout.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        part = ev.get("part") or {}
        if part.get("type") == "tool" and str(part.get("tool", "")).startswith("axiom_"):
            called.append(part["tool"])
    return called, False, out


DRIVERS = {"claude-code": ("claude", _claude), "opencode": ("opencode", _opencode)}


def _available(harness: str) -> str | None:
    """None when usable, else the reason to skip."""
    binary, _ = DRIVERS[harness]
    if shutil.which(binary) is None:
        return f"{binary} is not on PATH"
    try:
        probe = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"{binary} --version did not run: {type(exc).__name__}"
    if probe.returncode != 0:
        return f"{binary} is installed but broken (exit {probe.returncode})"
    if harness == "opencode" and not _REAL_ENV.get("ANTHROPIC_API_KEY"):
        return "opencode needs a model credential in the environment; none is set"
    return None


@pytest.fixture(params=sorted(DRIVERS))
def driver(request, tmp_path):
    reason = _available(request.param)
    if reason:
        pytest.skip(f"{request.param}: {reason}")
    return request.param, DRIVERS[request.param][1], tmp_path


def _reserved_lines(harness: str) -> list[str]:
    from axiom.extensions.builtins.mcp.harness_collisions import load_profiles

    prof = load_profiles()[harness]
    verified = sorted(n for n, m in prof.reserved.items() if m in ("verified-by-run", "from-binary"))
    # `resume`/`help`/`cost` are the interactive-only builtins; `undo` is an
    # OpenCode TUI command. Use those the profile marks verified, in that order.
    preferred = [n for n in ("resume", "help", "undo") if n in verified]
    return [f"/{n}" for n in preferred[:2]] or [f"/{verified[0]}"]


def test_a_reserved_slash_line_makes_no_axiom_tool_call(driver):
    harness, run, state = driver
    from axiom.extensions.builtins.mcp.harness_collisions import load_profiles

    profile = load_profiles()[harness]
    expects = profile.raw["noninteractive"]["builtin_slash_line"]
    for line in _reserved_lines(harness):
        called, handled, out = run(line, state)
        assert out.returncode == 0, f"{harness} {line}: exit {out.returncode}"
        assert called == [], (
            f"{harness}: sending {line!r} caused Axiom tool calls {called}; a harness-"
            "reserved slash line must never reach an Axiom tool"
        )
        if expects == "handled-by-harness":
            assert handled, (
                f"{harness}: profile says {line!r} is handled by the harness, but a model "
                "turn answered it. Re-measure and update harness_profiles.toml."
            )


def test_a_natural_request_reaches_an_axiom_tool(driver, record_property):
    """Rate of requests that made the model reach for Axiom at all, plus the
    rate that picked the one tool this test is allowed to run.

    The surface has over a hundred tools, so a model may pick a sibling read
    tool (it did: ``axiom_agents__roster``). Only the one named tool is
    permitted, so an attempt on another is denied by the harness and harmless.
    Reaching for Axiom is the property; picking the exact tool is reported.
    """
    harness, run, state = driver
    attempted = exact = 0
    picked: list[str] = []
    for _ in range(SAMPLES):
        called, _handled, out = run(NATURAL_REQUEST, state)
        if out.returncode != 0:
            continue
        if called:
            attempted += 1
            picked.extend(called)
        if any(c.endswith(READ_TOOL) for c in called):
            exact += 1
    record_property(f"{harness}_axiom_attempt_rate", f"{attempted}/{SAMPLES}")
    record_property(f"{harness}_exact_tool_rate", f"{exact}/{SAMPLES}")
    print(
        f"\n[{harness}] reached for Axiom: {attempted}/{SAMPLES} "
        f"({attempted / SAMPLES:.0%}); exact {READ_TOOL}: {exact}/{SAMPLES}; picked {sorted(set(picked))}"
    )
    assert attempted > 0, (
        f"{harness}: 0/{SAMPLES} natural-language requests reached any Axiom tool. The "
        "server may not be connecting, or the harness renamed the tools (check "
        "harness_profiles.toml)."
    )
