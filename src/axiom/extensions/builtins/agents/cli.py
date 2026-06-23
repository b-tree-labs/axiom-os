# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi agents` — agent service lifecycle management.

Usage:
    axi agents status              Show running/stopped state for all agents
    axi agents start [name]        Start one or all always-on agents
    axi agents stop [name]         Stop one or all always-on agents
    axi agents logs [name]         Tail service log output
    axi agents register            Register all daemon agents as system services
    axi agents unregister [name]   Remove service registrations
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

from axiom.extensions.contracts import Extension


@dataclass
class RegistrationResult:
    """Outcome of registering a single daemon agent as an OS service."""

    agent_name: str
    registered: bool  # install() returned True
    started: bool  # start() returned True
    provider: str = ""
    error: str = ""
    unchanged: bool = False  # service was already in desired state — no user-visible work

    @property
    def ok(self) -> bool:
        return self.registered and self.started


def register_all_daemon_agents() -> list[RegistrationResult]:
    """Register a single per-slot Background Service (idempotent).

    Background Service pattern — replaces the pre-0.11.1 model of one OS-level
    service registration per always-on agent. After this call:
      - There is exactly one launchd plist / systemd timer per slot.
      - That background-service process discovers all daemon agents at runtime
        and dispatches due heartbeats every 30 seconds.
      - Any pre-0.11.1 per-agent service registrations are cleaned up
        automatically (per Ben 2026-04-29: "automatically fix whatever
        you see, today").

    The reason for the change is operator-visible: in macOS Login Items
    each per-agent registration appeared as its own "axi" entry; with N
    daemon agents the list bloated to N indistinguishable entries. The
    Background Service collapses that to one entry per slot, named
    "{ProductName}-Background-Service" (Axiom-Background-Service for
    axi-platform; a consumer layer's own brand when rebranded — host
    admins see who to call for help).

    Never raises; failures are reported in returned results.
    """
    cleanup_results = _cleanup_legacy_per_agent_services()
    results: list[RegistrationResult] = list(cleanup_results)

    try:
        agents = _discover_agent_extensions()
    except Exception as exc:  # pragma: no cover — discovery should not crash
        results.append(
            RegistrationResult(
                agent_name="<discovery>", registered=False, started=False, error=str(exc)
            )
        )
        return results

    # Daemon agents whose manifests *would* be registered (visible to operator
    # in `axi agents status`). The background service dispatches them at runtime.
    daemons = [e for e in agents if e.agent and e.agent.is_always_on and e.agent.is_registrable]
    if not daemons:
        # No daemon agents → no background service needed.
        return results

    try:
        mgr = _make_background_service_manager()
        # Snapshot the prior state so we can flag truly-unchanged runs as
        # such — keeps the CLI quiet on idempotent re-runs (no toast,
        # no "✓ registered" line) per #208.
        try:
            prior_status = mgr.status().status
        except Exception:
            prior_status = None
        from axiom.infra.services import ServiceStatus
        was_running = prior_status == ServiceStatus.RUNNING

        installed = mgr.install()
        started = mgr.start() if installed else False
        results.append(
            RegistrationResult(
                agent_name="background-service",
                registered=bool(installed),
                started=bool(started),
                provider=mgr.provider_name,
                unchanged=bool(was_running and installed and started),
                error="" if installed and started else "background-service install or start returned False",
            )
        )
    except Exception as exc:
        results.append(
            RegistrationResult(
                agent_name="background-service",
                registered=False,
                started=False,
                error=str(exc),
            )
        )
    return results


def _cleanup_legacy_per_agent_services() -> list[RegistrationResult]:
    """Remove any pre-0.11.1 per-agent OS service registrations on this slot.

    Pre-0.11.1 we installed one service per agent (com.axi-platform.<agent>-agent.plist
    on macOS, neut-<agent>-agent.{service,timer} on Linux). Post-0.11.1 there
    is exactly one background service. This function scans for the legacy pattern
    and cleans up; idempotent.

    Returns a RegistrationResult per cleanup so the operator sees what was
    removed. Never raises.
    """
    import platform

    results: list[RegistrationResult] = []
    if platform.system() == "Darwin":
        results.extend(_cleanup_legacy_launchd())
    elif platform.system() == "Linux":
        results.extend(_cleanup_legacy_systemd())
    return results


def _cleanup_legacy_launchd() -> list[RegistrationResult]:
    """Remove stale launchd plists from this slot.

    Two cleanup passes:
      1. **Pre-0.11.1 per-agent plists** — `com.<pkg>.<agent>-agent.plist`
         from any installed portfolio member.
      2. **Cross-brand Background Service plists** — when the operator
         installed (e.g.) a consumer layer over Axiom, the previously-registered
         `com.axi-platform.background-service.plist` is now stale and
         must be replaced by `com.consumer-layer.background-service.plist`.
         This pass finds and removes any portfolio-member Background
         Service plist that doesn't match the *current* brand.

    Both passes are driven by the entry-points-discovered portfolio
    member list, so future products (X-Foo, Y-Bar, ...) participate
    automatically without axi-platform changes.
    """
    import subprocess
    from pathlib import Path

    from axiom.infra.branding import discover_portfolio_members, get_branding

    current_pkg = get_branding().package_name
    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    if not launch_agents_dir.exists():
        return []

    current_bg_plist = f"com.{current_pkg}.background-service.plist"
    members = discover_portfolio_members()
    # Always include the current brand even if entry-points discovery
    # hasn't propagated (e.g., on first install before the new wheel
    # is fully imported).
    portfolio_pkgs = {m.package_name for m in members} | {current_pkg}

    results: list[RegistrationResult] = []

    # Pass 1: pre-0.11.1 per-agent plists from any portfolio member.
    for pkg in portfolio_pkgs:
        legacy_pattern = f"com.{pkg}.*-agent.plist"
        for plist_path in launch_agents_dir.glob(legacy_pattern):
            results.extend(_unload_and_remove_plist(plist_path))

    # Pass 2: cross-brand Background Service plists (other portfolio
    # member's BS plist is stale; the current brand's wins).
    for pkg in portfolio_pkgs:
        if pkg == current_pkg:
            continue
        cross_brand = launch_agents_dir / f"com.{pkg}.background-service.plist"
        if cross_brand.exists():
            results.extend(_unload_and_remove_plist(cross_brand))

    _ = current_bg_plist  # documentation: this is the survivor; never cleaned up
    _ = subprocess  # used inside the helper
    return results


def _unload_and_remove_plist(plist_path) -> list[RegistrationResult]:
    """Helper: unload a plist via launchctl, remove the file, return one result."""
    import subprocess

    label = plist_path.stem
    try:
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            capture_output=True,
            timeout=10,
        )
        plist_path.unlink(missing_ok=True)
        return [
            RegistrationResult(
                agent_name=f"<legacy-cleanup:{label}>",
                registered=False,
                started=False,
                error="",
            )
        ]
    except Exception as exc:
        return [
            RegistrationResult(
                agent_name=f"<legacy-cleanup:{label}>",
                registered=False,
                started=False,
                error=f"cleanup failed: {exc}",
            )
        ]


def _cleanup_legacy_systemd() -> list[RegistrationResult]:
    """Remove stale systemd user units from this slot.

    Two cleanup passes (mirror of the launchd path):
      1. **Pre-0.11.1 per-agent timers** — `neut-<agent>-agent.{service,timer}`
         under any prefix the portfolio uses (currently `neut-` for both
         axi-platform and a consumer layer; future portfolio members may use
         a different prefix and self-declare via entry-points).
      2. **Cross-brand Background Service units** — if a previous brand
         registered `<other-prefix>-background-service.{service,timer}`
         and the current brand is different, clean up the stale.
    """
    import subprocess
    from pathlib import Path

    from axiom.infra.branding import get_branding

    unit_dir = Path.home() / ".config" / "systemd" / "user"
    if not unit_dir.exists():
        return []

    # The unit-prefix is currently "neut-" for both portfolio members
    # we ship today (axi-platform + a consumer layer both use it for back-compat
    # with consumer-rebrand expectations). Future portfolio members
    # using a different unit prefix should declare it in their portfolio
    # entry-point metadata; tracked as a follow-up.
    bg_basename = "neut-background-service"
    legacy_pattern_service = "neut-*-agent.service"
    legacy_pattern_timer = "neut-*-agent.timer"

    results: list[RegistrationResult] = []
    seen_units: set[str] = set()
    for pattern in (legacy_pattern_service, legacy_pattern_timer):
        for unit_path in unit_dir.glob(pattern):
            stem = unit_path.stem
            if stem == bg_basename:
                continue
            if stem in seen_units:
                continue
            seen_units.add(stem)
            try:
                subprocess.run(
                    ["systemctl", "--user", "disable", "--now", f"{stem}.timer"],
                    capture_output=True,
                    timeout=10,
                )
                (unit_dir / f"{stem}.service").unlink(missing_ok=True)
                (unit_dir / f"{stem}.timer").unlink(missing_ok=True)
                results.append(
                    RegistrationResult(
                        agent_name=f"<legacy-cleanup:{stem}>",
                        registered=False,
                        started=False,
                        error="",
                    )
                )
            except Exception as exc:
                results.append(
                    RegistrationResult(
                        agent_name=f"<legacy-cleanup:{stem}>",
                        registered=False,
                        started=False,
                        error=f"cleanup failed: {exc}",
                    )
                )

    _ = get_branding  # cross-brand systemd cleanup is a follow-up; today both
    # portfolio members use the same `neut-` prefix and the BS unit name is
    # the same, so the cross-brand case reduces to "BS unit gets rewritten
    # in place" — no cross-prefix cleanup needed yet.

    if results:
        try:
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=10
            )
        except Exception:
            pass
    return results


def missing_daemon_agents() -> list[str]:
    """Return names of always-on agents whose OS service is not installed.

    Used by the CLI self-heal hook as a cheap drift detector. Never raises.
    """
    try:
        agents = _discover_agent_extensions()
    except Exception:
        return []
    missing: list[str] = []
    for ext in agents:
        if not (ext.agent and ext.agent.is_always_on):
            continue
        try:
            mgr = _make_service_manager(ext)
            info = mgr.status()
            if info.status == "not_installed":
                missing.append(ext.name)
        except Exception:
            # Treat errors as "unknown" — don't add to missing list to avoid noise.
            pass
    return missing


def _registrable_agent_names() -> list[str]:
    """Names of always-on agents that *can* be registered (have a heartbeat).

    The candidate set the operator chooses from in `agents register`.
    """
    try:
        agents = _discover_agent_extensions()
    except Exception:
        return []
    return [ext.name for ext in agents if ext.agent and ext.agent.is_registrable]


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------


def _discover_agent_extensions() -> list[Extension]:
    """Find all extensions with an [agent] section.

    Lifecycle visibility is gated on the presence of `[agent]` in the
    manifest, NOT on the top-level `[extension] kind` (which defaults
    to "tool" for every agent-bearing extension we ship — release,
    hygiene, publishing, signals, model_corral). Daemon vs. lazy is
    further filtered downstream by `agent.is_always_on`.
    """
    from axiom.extensions.discovery import discover_extensions

    return [ext for ext in discover_extensions() if ext.agent is not None]


def _surfaced_agent_extensions() -> list[Extension]:
    """Brand-scoped agent *listing* for `agents status` (ADR-048 / AEOS §2.10).

    A sibling product's agents stay discovered + invocable, but are not shown
    under another brand's status. Action verbs (register/start/stop, drift
    detection) use `_discover_agent_extensions` so they remain universal.
    """
    from axiom.extensions.discovery import surfaced_extensions

    return [ext for ext in surfaced_extensions() if ext.agent is not None]


_BACKGROUND_SERVICE_INTERVAL_SECS = 30


def _background_service_binary_name() -> str:
    """Brand-aware wrapper binary name installed via console_scripts.

    Axiom installs ship `Axiom-Background-Service`. Consumer-layer installs
    add their own (e.g., `ConsumerLayer-Background-Service`) in their
    pyproject and we use that instead — host admins reading macOS Login
    Items see who to call for help based on what they think they
    installed.
    """
    from axiom.infra.branding import get_branding

    product = get_branding().product_name.replace(" ", "-")
    return f"{product}-Background-Service"


def _make_background_service_manager():
    """Create a ServiceManager for the per-slot Background Service.

    The Background Service is a single OS service that wakes every 30s and
    dispatches all due agent heartbeats. One entry per slot in macOS
    Login Items (vs. one per agent before 0.11.1); same scaling on
    Linux systemd. ADR-036 §D3 slot identity becomes operationally
    visible here once F7 (slot-aware service naming) ships in Phase 2.

    Wrapper-binary resolution order (handles the brand-installed-but-
    its-own-wrapper-not-yet-present case):

      1. Try the current brand's wrapper (e.g., `ConsumerLayer-Background-Service`
         when a consumer layer is the active brand).
      2. Fall back to `Axiom-Background-Service` — always present
         because axi-platform is a transitive dep of every portfolio
         package, and axi-platform always installs this wrapper. Logs a
         one-line warning so the operator sees the fallback was used.
      3. Last resort: pass the literal name to the OS service manager;
         the registration will fail loudly with "binary not found"
         rather than silently mis-fire.
    """
    import logging
    import shutil

    from axiom.infra.branding import get_branding
    from axiom.infra.services import ServiceManager

    log = logging.getLogger(__name__)
    binary_name = _background_service_binary_name()
    binary_path = shutil.which(binary_name)
    if binary_path is None:
        fallback = "Axiom-Background-Service"
        fallback_path = shutil.which(fallback)
        if fallback_path is not None:
            log.warning(
                "Wrapper binary %r not found; falling back to %r. "
                "If you intended the current brand to provide its own wrapper, "
                "ensure its package declares the console_script.",
                binary_name,
                fallback,
            )
            binary_path = fallback_path
        else:
            binary_path = binary_name  # let ServiceManager surface the error

    pkg = get_branding().package_name

    return ServiceManager(
        name="background-service",
        binary=binary_path,
        args=[],
        interval_secs=_BACKGROUND_SERVICE_INTERVAL_SECS,
        service_id=f"com.{pkg}.background-service",
    )


def _make_service_manager(ext: Extension):
    """Create a ServiceManager for an agent extension (LEGACY).

    Pre-0.11.1 used per-agent service registration. Post-0.11.1 we use
    the Background Service pattern (`_make_background_service_manager`). This
    legacy helper is kept only for callers that still need per-agent
    paths (e.g., introspection of legacy registrations during cleanup).
    Do NOT use for new registrations — use the Background Service.
    """
    import os
    import re

    from axiom.infra.branding import get_branding
    from axiom.infra.services import ServiceManager

    cli = get_branding().cli_name

    # Resolve [agent.env] from the manifest with ${VAR} substitution from
    # the operator's current os.environ at install time. ADR-036 §D9 makes
    # this the auditable, manifest-declared opt-in path for any env beyond
    # the bounded PATH + LANG/LC_ALL the platform always provides.
    env: dict[str, str] = {}
    if ext.agent and ext.agent.env:
        var_re = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
        for key, value in ext.agent.env.items():
            env[key] = var_re.sub(lambda m: os.environ.get(m.group(1), ""), value)

    return ServiceManager(
        name=f"{ext.name}-agent",
        binary=cli,
        args=_agent_service_args(ext),
        env=env,
        interval_secs=ext.agent.heartbeat_interval if ext.agent else 0,
    )


def _agent_service_args(ext: Extension) -> list[str]:
    """Build the argv the systemd timer fires on each heartbeat tick.

    Reads ``[agent] heartbeat_command`` from the extension manifest — a
    space-separated string like ``"tidy health --json"`` that becomes a
    oneshot command. Callers MUST gate registration on
    ``ext.agent.is_registrable`` so agents without a declared
    heartbeat_command don't end up with a crash-looping invocation (the
    v0.9.0–v0.10.2 regression that motivated this design).
    """
    if ext.agent and ext.agent.heartbeat_command:
        return ext.agent.heartbeat_command.split()
    # Defensive: if caller failed to gate on is_registrable, emit a
    # command that will fail fast and obviously rather than loop.
    return ["--invalid-no-heartbeat-command-configured"]


def _get_service_status(ext: Extension) -> str:
    """Get the service status string for an agent."""

    mgr = _make_service_manager(ext)
    info = mgr.status()
    return info.status


def _find_agent(agents: list[Extension], name: str) -> Extension | None:
    """Find an agent by name (matches extension name or CLI noun)."""
    for ext in agents:
        if ext.name == name:
            return ext
        for cmd in ext.cli_commands:
            if cmd.noun == name:
                return ext
    return None


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


def _consent_status_line(consent) -> str:
    """One-line summary of the recorded consent decision + how to change it."""
    cli = _cli_name()
    if not consent.decided:
        return f"not yet decided — run `{cli} agents register` to choose"
    if consent.opted_out:
        return f"opted out (no agents run) — `{cli} agents register` to enable"
    ver = f" · since {consent.decided_version}" if consent.decided_version else ""
    enabled = ", ".join(consent.enabled) or "(none)"
    return f"enabled: {enabled}{ver} — `{cli} agents register` to change"


def _cmd_status(args) -> int:
    agents = _surfaced_agent_extensions()  # listing surface -> brand-scoped
    if not agents:
        print("No agent extensions with lifecycle configuration found.")
        return 0

    # Background Service status — the single OS-level surface (Login Items / systemd timer).
    coord_mgr = _make_background_service_manager()
    coord_info = coord_mgr.status()
    coord_color_map = {
        "running": "\033[32m",
        "starting": "\033[33m",
        "stopped": "\033[33m",
        "failed": "\033[31m",
        "not_installed": "\033[90m",
        "unknown": "\033[90m",
    }
    coord_color = coord_color_map.get(coord_info.status, "")
    coord_label = "not installed" if coord_info.status == "not_installed" else coord_info.status
    coord_status_str = f"{coord_color}{coord_label}\033[0m" if coord_color else coord_label
    binary_name = _background_service_binary_name()

    print(f"Background Service: {binary_name}")
    print("=" * 60)
    print(f"  Status:    {coord_status_str} ({coord_info.provider})")
    print(f"  Cadence:   every {_BACKGROUND_SERVICE_INTERVAL_SECS}s")
    print()

    # Consent — the operator's recorded decision about host-persistent agents,
    # and how to revise it. This is the surface for "what did I agree to?".
    from axiom.extensions.builtins.agents.consent import load_consent

    consent = load_consent()
    print(f"  Consent:   {_consent_status_line(consent)}")
    print()
    approved = None if not consent.decided else set(consent.enabled)

    # Per-agent view — what the Background Service is dispatching, not what's
    # registered as an OS service (those are no longer per-agent).
    print("Agents (dispatched by Background Service)")
    print("=" * 60)
    print(f"  {'Agent':<20} {'Startup':<10} {'Interval':<10} {'Last run':<20}")
    print(f"  {'-' * 20} {'-' * 10} {'-' * 10} {'-' * 20}")

    last_runs = _bg_last_runs()
    now = time.time()
    for ext in agents:
        interval = f"{ext.agent.heartbeat_interval}s"
        startup = ext.agent.startup
        last_run = last_runs.get(ext.name)
        if last_run is None:
            last_run_str = "—"
        else:
            elapsed = now - last_run
            last_run_str = _format_elapsed(elapsed) + " ago"
        # Flag agents the operator hasn't approved — they won't actually tick
        # even though they're dispatch-eligible.
        gated = (
            approved is not None
            and ext.agent.is_registrable
            and ext.name not in approved
        )
        note = "  \033[90m(not enabled)\033[0m" if gated else ""
        print(
            f"  {ext.name:<20} {startup:<10} {interval:<10} {last_run_str:<20}{note}"
        )

    # Watchers summary
    print()
    for ext in agents:
        if ext.agent.watchers:
            enabled = [w for w in ext.agent.watchers if w.enabled]
            if enabled:
                names = ", ".join(w.name for w in enabled)
                print(f"  {ext.name} watchers: {names}")

    print()
    return 0


def _bg_last_runs() -> dict[str, float]:
    """Read the background service's per-agent last-run timestamps."""
    from axiom.agents.background_service import StateStore, _state_path

    try:
        return StateStore(_state_path()).load()
    except Exception:
        return {}


def _format_elapsed(seconds: float) -> str:
    """Compact human-friendly elapsed-time formatter."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _cmd_start(args) -> int:
    """Start the per-slot Background Service (which dispatches all daemon agents).

    Pre-0.11.1 this command accepted an agent name to start an individual
    agent; post-0.11.1 there is exactly one Background Service per slot. The
    name argument is accepted but ignored (with a one-line note) so old
    muscle memory doesn't error out.
    """
    name = getattr(args, "name", None)
    if name:
        print(
            f"Note: post-0.11.1 there is exactly one Background Service per slot "
            f"(starting it; '{name}' would be dispatched by the Background Service)."
        )

    print("Starting Background Service...")
    results = register_all_daemon_agents()
    rc = 0
    for r in results:
        if r.agent_name.startswith("<legacy-cleanup:"):
            print(f"  Cleaned up legacy unit: {r.agent_name[16:-1]}")
        elif r.ok:
            print(f"  Background Service: running ({r.provider})")
        else:
            print(f"  Failed: {r.agent_name} — {r.error or 'unknown'}")
            rc = 1
    return rc


def _cmd_stop(args) -> int:
    """Stop the per-slot Background Service. Stops all agents because they're
    dispatched by the Background Service.
    """
    name = getattr(args, "name", None)
    if name:
        print(
            f"Note: post-0.11.1 there is exactly one Background Service per slot "
            f"(stopping it; '{name}' was dispatched by the Background Service)."
        )

    mgr = _make_background_service_manager()
    if mgr.stop():
        print("  background-service: stopped")
        return 0
    print("  background-service: failed to stop (may not be running)")
    return 1


# ---------------------------------------------------------------------------
# Informed-consent surface: describe each agent so the operator knows what
# they're granting host-persistent execution to before they enable it.
# ---------------------------------------------------------------------------


def _cli_name() -> str:
    try:
        from axiom.infra.branding import get_branding

        return (get_branding().cli_name or "axi").strip()
    except Exception:
        return "axi"


def _humanize_interval(secs: int) -> str:
    if secs % 3600 == 0 and secs >= 3600:
        return f"every {secs // 3600}h"
    if secs % 60 == 0 and secs >= 60:
        return f"every {secs // 60}m"
    return f"every {secs}s"


def _persona_summary(ext: Extension) -> str:
    """First prose paragraph of the agent's persona.md (best-effort, '' if none)."""
    try:
        for persona in sorted(ext.root.glob("agents/*/persona.md")):
            lines = persona.read_text(encoding="utf-8").splitlines()
            para: list[str] = []
            for line in lines:
                s = line.strip()
                if s.startswith("#") or s.startswith("---") or (not s and not para):
                    if para:
                        break
                    continue
                if not s and para:
                    break
                para.append(s)
            if para:
                return " ".join(para)
    except Exception:
        pass
    return ""


def _agent_brief(idx: int, ext: Extension) -> str:
    """One-line, list-friendly summary for the picker."""
    cadence = _humanize_interval(ext.agent.heartbeat_interval) if ext.agent else "?"
    desc = (ext.description or ext.name).rstrip(".")
    return f"  {idx}. {ext.name} — {desc}  · {cadence}"


def _print_agent_detail(ext: Extension) -> None:
    """Full drill-in detail: what it is, how often, and exactly what it runs."""
    cli = _cli_name()
    print(f"\n  {ext.name}")
    if ext.description:
        print(f"    {ext.description}")
    summary = _persona_summary(ext)
    if summary:
        print(f"    {summary[:400]}{'…' if len(summary) > 400 else ''}")
    if ext.agent:
        print(f"    Cadence : {_humanize_interval(ext.agent.heartbeat_interval)}")
        print(f"    Each tick runs: {cli} {ext.agent.heartbeat_command}")
    print(
        "    Safety  : reversible actions run autonomously; consequential ones "
        "ask first (ADR-045 D6)."
    )
    print()


def _cmd_info(args) -> int:
    """`axi agents info [name]` — learn what an agent does before enabling it."""
    name = getattr(args, "name", None)
    agents = [
        ext
        for ext in _discover_agent_extensions()
        if ext.agent and ext.agent.is_registrable
    ]
    if name:
        match = next((e for e in agents if e.name == name), None)
        if match is None:
            print(f"Unknown agent: {name}")
            print(f"Available: {', '.join(e.name for e in agents)}")
            return 1
        agents = [match]
    if not agents:
        print("No registrable daemon agents found.")
        return 0
    for ext in agents:
        _print_agent_detail(ext)
    return 0


def _print_registration_results(results) -> int:
    rc = 0
    for r in results:
        if r.agent_name.startswith("<legacy-cleanup:"):
            # Legacy cleanup is success when error is empty; the
            # RegistrationResult.ok=False is misleading here because nothing
            # was meant to be "registered" — cleanup is removal, not install.
            label = r.agent_name[len("<legacy-cleanup:") : -1]
            if not r.error:
                print(f"  Cleaned up legacy unit: {label}")
            else:
                print(f"  Cleanup failed: {label} — {r.error}")
                rc = 1
        elif r.ok:
            print(f"  Registered: {r.agent_name} ({r.provider})")
        else:
            detail = r.error or "install or start failed"
            print(f"  Failed: {r.agent_name} — {detail}")
            rc = 1
    return rc


def _interactive_select(candidates: list[Extension]) -> tuple[list[str], bool]:
    """Prompt loop with per-agent drill-in. Returns (enabled, opted_out).

    Typing ``?N`` shows agent N's full detail then re-prompts, so the operator
    can learn about each agent before committing. Raises ``ValueError`` to
    signal cancel-without-change (caller prints "No changes.").
    """
    from axiom.extensions.builtins.agents.consent import parse_register_selection

    names = [e.name for e in candidates]
    print("Daemon agents available as background services:")
    for i, ext in enumerate(candidates, 1):
        print(_agent_brief(i, ext))
    print(
        "Registering installs an OS task that survives reboots and runs on "
        "your host."
    )
    while True:
        raw = input(
            "Enable which? [a]ll / [n]one / numbers e.g. 1,3 / ?N for details: "
        ).strip()
        if raw.startswith("?"):
            target = raw[1:].strip()
            if target.isdigit() and 1 <= int(target) <= len(candidates):
                _print_agent_detail(candidates[int(target) - 1])
            else:
                for ext in candidates:  # bare "?" -> describe them all
                    _print_agent_detail(ext)
            continue
        return parse_register_selection(raw, names)


def _apply_registration_decision(selected: list[str], opted_out: bool) -> int:
    """Record the consent decision, then install or tear down accordingly."""
    from axiom.extensions.builtins.agents.consent import record_decision

    record_decision(enabled=selected, opted_out=opted_out)
    if opted_out or not selected:
        # Opted out / nothing chosen: ensure no background service lingers.
        try:
            _make_background_service_manager().uninstall()
        except Exception:
            pass
        print("Opted out — no agent background services will run.")
        return 0
    print(f"Enabling: {', '.join(selected)}")
    print("Registering Background Service...")
    return _print_registration_results(register_all_daemon_agents())


def run_interactive_registration() -> int:
    """Discover candidates, run the informed picker, apply the decision.

    Shared by `agents register` (TTY) and the startup prompt's "yes" path so
    granting host-persistent execution always goes through the same informed,
    à-la-carte surface (with ?N drill-in) — never a blind enable-all.
    """
    candidates = [
        ext
        for ext in _discover_agent_extensions()
        if ext.agent and ext.agent.is_registrable
    ]
    if not candidates:
        print("No daemon agents to register.")
        return 0
    try:
        selected, opted_out = _interactive_select(candidates)
    except (ValueError, EOFError, KeyboardInterrupt):
        print("No changes.")
        return 0
    return _apply_registration_decision(selected, opted_out)


def _cmd_register(args) -> int:
    """Register daemon agents as background services — consent-gated.

    Never installs without an explicit choice: ``--all`` / ``--agents`` /
    ``--none``, an interactive picker (with ``?N`` drill-in) on a TTY, or — when
    non-interactive with no flags — a detect-and-instruct listing that installs
    nothing.
    """
    candidates = [
        ext
        for ext in _discover_agent_extensions()
        if ext.agent and ext.agent.is_registrable
    ]
    if not candidates:
        print("No daemon agents to register.")
        return 0
    names = [e.name for e in candidates]

    if getattr(args, "all", False):
        return _apply_registration_decision(list(names), False)
    if getattr(args, "none", False):
        return _apply_registration_decision([], True)
    if getattr(args, "agents", None):
        requested = [a.strip() for a in args.agents.split(",") if a.strip()]
        unknown = [a for a in requested if a not in names]
        if unknown:
            print(f"Unknown agent(s): {', '.join(unknown)}")
            print(f"Available: {', '.join(names)}")
            return 1
        return _apply_registration_decision(requested, False)
    if sys.stdin.isatty() and sys.stdout.isatty():
        return run_interactive_registration()

    # Non-interactive, no flag: never install silently — list + instruct.
    cli = _cli_name()
    print("Daemon agents available as background services:")
    for i, ext in enumerate(candidates, 1):
        print(_agent_brief(i, ext))
    print(f"\n  {cli} agents info [name]       learn what each agent does")
    print(f"  {cli} agents register --all     enable all")
    print(f"  {cli} agents register --agents <a,b>   enable a subset")
    print(f"  {cli} agents register --none    opt out (no startup prompt)")
    return 0


def _cmd_unregister(args) -> int:
    """Unregister the per-slot Background Service + clean up any legacy per-agent units."""
    name = getattr(args, "name", None)
    if name:
        print(
            f"Note: post-0.11.1 there is exactly one Background Service per slot "
            f"(unregistering it; '{name}' was dispatched by the Background Service)."
        )

    rc = 0
    cleanup = _cleanup_legacy_per_agent_services()
    for r in cleanup:
        print(f"  Cleaned up legacy unit: {r.agent_name[16:-1] if r.agent_name.startswith('<legacy-cleanup:') else r.agent_name}")

    mgr = _make_background_service_manager()
    if mgr.uninstall():
        print("  Unregistered: background-service")
    else:
        print("  Failed to unregister background-service")
        rc = 1
    return rc


def _cmd_logs(args) -> int:
    from axiom.infra.paths import get_user_state_dir

    agents = _discover_agent_extensions()
    name = getattr(args, "name", None)

    if name:
        ext = _find_agent(agents, name)
        if ext is None:
            print(f"Unknown agent: {name}")
            return 1
        agents = [ext]

    services_dir = get_user_state_dir() / "services"
    for ext in agents:
        stdout_log = services_dir / f"{ext.name}-agent.stdout.log"
        stderr_log = services_dir / f"{ext.name}-agent.stderr.log"

        print(f"--- {ext.name} ---")
        for log_path in (stdout_log, stderr_log):
            if log_path.exists():
                lines = log_path.read_text(encoding="utf-8").splitlines()
                for line in lines[-20:]:
                    print(f"  {line}")
            else:
                print(f"  No log: {log_path.name}")
        print()

    return 0


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi agents",
        description="Manage always-on agent services",
    )
    sub = parser.add_subparsers(dest="action")

    sub.add_parser("status", help="Show agent service status")

    start_p = sub.add_parser("start", help="Start one or all agents")
    start_p.add_argument("name", nargs="?", help="Agent name (omit for all)")

    stop_p = sub.add_parser("stop", help="Stop one or all agents")
    stop_p.add_argument("name", nargs="?", help="Agent name (omit for all)")

    info_p = sub.add_parser(
        "info", help="Describe agents (what they do) before enabling them"
    )
    info_p.add_argument("name", nargs="?", help="Agent name (omit for all)")

    reg_p = sub.add_parser(
        "register", help="Register daemon agents as background services (consent-gated)"
    )
    reg_p.add_argument("--all", action="store_true", help="Enable all daemon agents")
    reg_p.add_argument(
        "--none",
        "--opt-out",
        dest="none",
        action="store_true",
        help="Opt out — install nothing and don't prompt at startup",
    )
    reg_p.add_argument(
        "--agents",
        metavar="A,B",
        help="Comma-separated subset to enable (à la carte)",
    )

    unreg_p = sub.add_parser("unregister", help="Remove service registrations")
    unreg_p.add_argument("name", nargs="?", help="Agent name (omit for all)")

    logs_p = sub.add_parser("logs", help="Tail agent service logs")
    logs_p.add_argument("name", nargs="?", help="Agent name (omit for all)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.action:
        args.action = "status"

    handlers = {
        "status": _cmd_status,
        "start": _cmd_start,
        "stop": _cmd_stop,
        "info": _cmd_info,
        "register": _cmd_register,
        "unregister": _cmd_unregister,
        "logs": _cmd_logs,
    }

    handler = handlers.get(args.action)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
