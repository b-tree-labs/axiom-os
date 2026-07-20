# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Per-slot agent Background Service — the single launchd/systemd entry point.

**This is OS-level plumbing, not an agent.** No persona, no LLM, no
judgment. The Background Service is a deterministic dispatcher: wakes
on a timer, checks state, spawns due heartbeats as subprocesses,
records timestamps, exits. AXI (the conversational coordinator who
talks to humans) is a different role at a different layer.

Replaces the pre-0.11.1 pattern of one OS-service-registration per
always-on agent. The OS-level Login Items / systemd surface now shows
exactly one entry per installed Axiom slot, regardless of how many
agents the install hosts (per the 2026-04-29 UX fix: "a single
coordinating surface per installed OS").

Each tick (default cadence 30s):
  1. Discover all agent extensions on this slot.
  2. For each always-on agent with a heartbeat_command, check the
     last-run timestamp in ~/.axi/agents/.background-service/state.json.
  3. If (now - last_run) >= heartbeat_interval, spawn the agent's
     heartbeat command as a subprocess and record the new last_run.
  4. Persist a tick log entry to
     ~/.axi/agents/.background-service/ticks.jsonl for observability.

Each agent runs as a subprocess (preserves isolation: one bad agent
doesn't kill the dispatcher). Agents that exit non-zero are still
considered "ran successfully from the dispatcher's POV" — non-zero
just means the agent surfaced findings (RIVET exits 2 on red CI;
TRIAGE exits 2 on critical findings).

This is also the implementation seam where ADR-036 §D3 slot identity
becomes operationally visible (Phase 2 work).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from axiom.extensions.builtins.agents.consent import load_consent
from axiom.extensions.builtins.settings.store import autonomy_enabled
from axiom.infra.branding import get_branding
from axiom.infra.paths import get_user_state_dir

log = logging.getLogger(__name__)


@dataclass
class StateStore:
    """JSON-backed last-run-timestamp store for the background service.

    Atomic via write-then-rename. Corrupted state (truncated write,
    partial JSON) is treated as empty rather than fatal — the worst
    case is one extra agent invocation on the next tick.
    """

    path: Path

    def load(self) -> dict[str, float]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, state: dict[str, float]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def is_due(last_run: float, interval_secs: int, now: float) -> bool:
    return (now - last_run) >= interval_secs


def _discover_daemon_extensions():
    """Return all extensions whose [agent] block declares always-on + a heartbeat_command."""
    from axiom.extensions.discovery import discover_extensions

    return [
        ext
        for ext in discover_extensions()
        if ext.agent is not None and ext.agent.is_registrable
    ]


def dispatch_due_agents(
    extensions,
    store: StateStore,
    cli_binary: str,
    now: float | None = None,
    enabled: set[str] | None = None,
) -> list[str]:
    """Dispatch every due agent as a subprocess; return names dispatched.

    One bad agent does not block the others — exceptions and non-zero
    exits are logged but don't propagate. State is updated only for
    agents that actually ran (subprocess.run did not raise).

    ``enabled`` is the à-la-carte consent filter: ``None`` means no recorded
    choice (dispatch all — a pre-consent install must not be silently
    neutered by an upgrade); a set restricts dispatch to those agent names
    (an empty set therefore dispatches nothing, i.e. opted out).
    """
    if now is None:
        now = time.time()
    state = store.load()
    dispatched: list[str] = []
    for ext in extensions:
        if not (ext.agent and ext.agent.is_registrable):
            continue
        if enabled is not None and ext.name not in enabled:
            continue
        # First-tick semantics: an agent with no recorded last_run is
        # always due — fires promptly after install/reboot rather than
        # making the operator wait a full interval before the first
        # tick. Subsequent ticks honor the declared interval.
        if ext.name not in state:
            pass  # fall through to dispatch
        elif not is_due(state[ext.name], ext.agent.heartbeat_interval, now):
            continue

        cmd = [cli_binary, *ext.agent.heartbeat_command.split()]
        try:
            result = subprocess.run(cmd, check=False, timeout=120)
            log.info("background-service dispatched %s rc=%s", ext.name, result.returncode)
        except Exception as exc:
            log.warning("background-service failed to dispatch %s: %s", ext.name, exc)
            continue
        state[ext.name] = now
        dispatched.append(ext.name)

    store.save(state)
    return dispatched


def _bg_dir() -> Path:
    return get_user_state_dir() / "agents" / ".background-service"


def _state_path() -> Path:
    return _bg_dir() / "state.json"


def _ticks_log_path() -> Path:
    return _bg_dir() / "ticks.jsonl"


def background_service_main(argv: list[str] | None = None) -> int:
    """One background-service tick — what the launchd/systemd timer fires.

    Returns 0 on success (whether agents ran or not — having no work
    to do is normal). Returns non-zero only on background-service-level errors
    (discovery crashes, state-store corruption that can't be recovered).
    """
    parser = argparse.ArgumentParser(
        prog=f"{get_branding().product_name}-Background-Service",
        description="Background Service — dispatches due heartbeats",
    )
    parser.parse_args(argv)

    coord_dir = _bg_dir()
    coord_dir.mkdir(parents=True, exist_ok=True)

    # Master autonomy gate. When OFF (the default), dispatch nothing — a timer
    # surviving from a prior install becomes a no-op within one tick. Belt to
    # the install-time suspenders in register_all_daemon_agents.
    if not autonomy_enabled():
        _append_tick_log(
            {"ts": time.time(), "agent_count": 0, "dispatched": [], "skipped": "autonomy_disabled"}
        )
        return 0

    store = StateStore(_state_path())

    cli_binary = get_branding().cli_name

    try:
        extensions = _discover_daemon_extensions()
    except Exception as exc:
        log.exception("background-service discovery failed")
        _append_tick_log(
            {
                "ts": time.time(),
                "agent_count": 0,
                "dispatched": [],
                "error": f"discovery failed: {exc}",
            }
        )
        return 2

    # Respect the operator's à-la-carte consent: decided -> only approved
    # agents; opted out -> empty set (nothing); undecided/no file -> None
    # (dispatch all, so a pre-consent install keeps ticking after upgrade).
    consent = load_consent()
    if consent.opted_out:
        enabled: set[str] | None = set()
    elif consent.decided:
        enabled = set(consent.enabled)
    else:
        enabled = None

    try:
        dispatched = dispatch_due_agents(extensions, store, cli_binary, enabled=enabled)
    except Exception as exc:
        log.exception("background-service dispatch failed")
        _append_tick_log(
            {
                "ts": time.time(),
                "agent_count": len(extensions),
                "dispatched": [],
                "error": f"dispatch failed: {exc}",
            }
        )
        return 3

    _append_tick_log(
        {
            "ts": time.time(),
            "agent_count": len(extensions),
            "dispatched": dispatched,
        }
    )
    return 0


def _append_tick_log(entry: dict) -> None:
    """Best-effort append to the ticks JSONL log; never raises."""
    try:
        path = _ticks_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def console_main() -> None:
    """Console_scripts entry point.

    pyproject.toml adds (per-package, brand-aware):
        Axiom-Background-Service = "axiom.agents.background_service:console_main"

    A domain consumer adds its own (e.g. Consumer-Background-Service) in its
    own pyproject; both reuse this same main so the dispatcher logic
    lives in one place.
    """
    sys.exit(background_service_main(sys.argv[1:]))
