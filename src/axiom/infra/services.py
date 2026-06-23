# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Managed service lifecycle — provider pattern with platform backends.

Supports launchd (macOS), systemd (Linux), and Windows Task Scheduler.
Providers are tried in order with automatic fallback.

Service definitions live in ~/<state-dir>/services/.

Usage:
    from axiom.infra.services import get_service_manager

    svc = get_service_manager("ollama", binary="ollama", args=["serve"])
    svc.install()   # Register with OS (persists across reboots)
    svc.start()     # Start the service
    svc.stop()      # Stop the service
    svc.status()    # Returns ServiceInfo
    svc.uninstall() # Remove registration
"""

from __future__ import annotations

import abc
import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from axiom.infra.branding import get_branding
from axiom.infra.paths import get_user_state_dir

log = logging.getLogger(__name__)


# On Windows, every subprocess of a console-mode executable (schtasks.exe,
# pip-generated console_scripts, etc.) pops a new cmd window unless the
# parent passes CREATE_NO_WINDOW. The visual flash is jarring and
# Austin's 2026-05-22 onboarding pass surfaced it during `axi install`.
# Spread `**_WIN_NO_WINDOW` across every Windows-side subprocess call to
# suppress.
if platform.system() == "Windows":
    _WIN_NO_WINDOW: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_NO_WINDOW = {}


def _get_services_dir() -> Path:
    return get_user_state_dir() / "services"


# ---------------------------------------------------------------------------
# Bounded service-env contract (ADR-036 §D9)
# ---------------------------------------------------------------------------

# Curated PATH allow-list: directories the platform considers safe enough to
# inject into a daemon's persistent service definition. The bake-in survives
# reboots, so anything writable by an attacker (or by a forgetful operator)
# becomes a long-lived foothold — the allow-list is what prevents that.
_PATH_ALLOW_LIST: tuple[str, ...] = (
    "/usr/bin",
    "/usr/sbin",
    "/usr/local/bin",
    "/usr/local/sbin",
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/bin",
    "/sbin",
)


def bounded_path(captured_path: str, venv_bin: str | os.PathLike) -> tuple[str, list[str]]:
    """Intersect ``captured_path`` with the curated allow-list.

    Returns ``(bounded, dropped)`` where ``bounded`` is the safe PATH string to
    inject into a service env, and ``dropped`` is the list of entries that were
    refused so the caller can warn the operator.

    The venv's ``bin/`` is always prepended (the install's own venv is trusted
    by definition). ``~/.local/bin`` is allowed (per-user, owned by the user).
    Empty segments and ``.`` are refused — both are shorthand for
    "current working directory at exec time," a textbook footgun.
    World-writable directories (mode bit 0o002) are refused with a warning.

    See ADR-036 §D9 for the full contract.
    """
    venv_bin = str(venv_bin)
    user_local_bin = os.path.expanduser("~/.local/bin")
    allow_set: set[str] = set(_PATH_ALLOW_LIST) | {venv_bin, user_local_bin}

    kept: list[str] = []
    dropped: list[str] = []

    # venv_bin first; always trusted.
    if venv_bin not in kept:
        kept.append(venv_bin)

    for raw in captured_path.split(":"):
        entry = raw.strip()
        if not entry or entry == ".":
            dropped.append(entry or "")
            continue
        # Refuse paths starting with a literal '~' — never expand at install
        # time, since shell expansion semantics differ from launchd/systemd.
        if entry.startswith("~"):
            dropped.append(entry)
            continue
        if entry not in allow_set:
            dropped.append(entry)
            continue
        # Refuse world-writable directories. If the dir doesn't exist on this
        # host, that's fine — including a non-existent allow-listed path is
        # harmless (PATH lookup just skips it), and the operator may install
        # to it later (e.g., homebrew not yet installed at register time).
        # Dropping non-existent allow-listed dirs would create a brittle
        # PATH that depends on install-time filesystem state.
        try:
            mode = os.stat(entry).st_mode
            if mode & 0o002:
                dropped.append(entry)
                continue
        except OSError:
            pass  # Doesn't exist; harmless — keep it.
        if entry not in kept:
            kept.append(entry)

    return ":".join(kept), dropped


def _resolve_venv_bin(binary_path: str) -> str:
    """Best-effort: derive the venv's bin/ directory from the resolved binary.

    Returns the directory containing ``binary_path``. For typical
    ``<venv>/bin/axi`` that's ``<venv>/bin/``; for system installs it's
    whatever directory the binary resolves to (still safe; the binary
    itself is what the plist will exec).
    """
    return str(Path(binary_path).resolve().parent)


def _platform_env_for_service(svc_env: dict[str, str], binary_path: str) -> dict[str, str]:
    """Compute the env dict to inject into a managed service per ADR-036 §D9.

    - PATH is always present, computed from os.environ at install time and
      bounded by the allow-list.
    - HOME is NOT default-injected; only present if explicitly in svc_env.
    - LANG / LC_ALL pass through if set in os.environ.
    - All other svc_env entries pass through verbatim (extension-declared env).

    svc_env wins on conflicts (an extension that explicitly sets PATH
    overrides the platform default; this is intentional but loud-warned
    in the install path so the operator sees the relaxation).
    """
    env: dict[str, str] = {}

    venv_bin = _resolve_venv_bin(binary_path)
    captured = os.environ.get("PATH", "")
    bounded, dropped = bounded_path(captured, venv_bin=venv_bin)
    if dropped:
        log.warning(
            "Bounded service PATH dropped %d non-allow-listed entries: %s",
            len(dropped),
            ", ".join(d if d else "<empty>" for d in dropped),
        )
    env["PATH"] = bounded

    for key in ("LANG", "LC_ALL"):
        if val := os.environ.get(key):
            env[key] = val

    # Extension-declared env wins last so it overrides platform defaults.
    env.update(svc_env)
    return env


def _systemd_sandbox_directives(svc: object) -> str:
    """Return systemd hardening directives per ADR-036 §D10.

    Phase 0 ships a uniform set of defaults across all daemon agents. Phase
    2/3 will accept per-extension relaxation via the manifest's
    ``[agent.sandbox]`` block; until then this is a single contract.

    Notes on each directive:
    - NoNewPrivileges: blocks the daemon from gaining privilege via setuid
      binaries it might exec (gh, glab, etc).
    - PrivateTmp: per-service /tmp that is unlinked on stop; prevents
      cross-service temp-file leakage.
    - ProtectSystem=strict: most of /, /usr, /boot becomes read-only.
    - ProtectHome=read-only: $HOME is read-only by default. The slot's
      state dir is added to ReadWritePaths so the agent can write its
      persistent state.
    - RestrictAddressFamilies: the agent only needs UNIX (IPC), INET (HTTP),
      INET6. No raw sockets, no netlink, no AF_PACKET.
    - RestrictNamespaces: refuses CLONE_NEW* — the agent has no business
      creating namespaces.
    - LockPersonality: blocks personality(2) changes that confuse syscall
      filtering.
    - MemoryDenyWriteExecute: blocks W^X violations — a defense against
      JIT-ROP and similar memory-corruption exploitation primitives.
    """
    state_dir = get_user_state_dir()
    return "\n".join(
        [
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "ProtectSystem=strict",
            "ProtectHome=read-only",
            f"ReadWritePaths={state_dir}",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
            "RestrictNamespaces=true",
            "LockPersonality=true",
            "MemoryDenyWriteExecute=true",
        ]
    )


class ServiceStatus:
    RUNNING = "running"
    STARTING = "starting"  # intermediate: activating, queued; not yet running
    FAILED = "failed"  # service exited with an error
    STOPPED = "stopped"
    NOT_INSTALLED = "not_installed"
    UNKNOWN = "unknown"


@dataclass
class ServiceInfo:
    """Status of a managed service."""

    name: str
    status: str  # ServiceStatus value
    pid: int = 0
    message: str = ""
    provider: str = ""  # Which backend is managing this


# ---------------------------------------------------------------------------
# Abstract provider
# ---------------------------------------------------------------------------


class ServiceProvider(abc.ABC):
    """Abstract base for platform-specific service management."""

    @abc.abstractmethod
    def name(self) -> str:
        """Provider name (e.g., 'launchd', 'systemd', 'subprocess')."""

    @abc.abstractmethod
    def available(self) -> bool:
        """Whether this provider works on the current platform."""

    @abc.abstractmethod
    def install(self, svc: ServiceDef) -> bool:
        """Register the service for persistent startup."""

    @abc.abstractmethod
    def start(self, svc: ServiceDef) -> bool:
        """Start the service."""

    @abc.abstractmethod
    def stop(self, svc: ServiceDef) -> bool:
        """Stop the service."""

    @abc.abstractmethod
    def status(self, svc: ServiceDef) -> ServiceInfo:
        """Check if the service is running."""

    @abc.abstractmethod
    def uninstall(self, svc: ServiceDef) -> bool:
        """Remove the service registration."""


@dataclass
class ServiceDef:
    """Definition of a managed service.

    `interval_secs > 0` flips the service into **periodic mode**: providers
    register a timer that fires the command on schedule as a one-shot
    process instead of a long-running daemon. This matters for two reasons:

    1. Robustness: a crashing oneshot is retried on the next timer fire
       instead of entering a crash-restart loop.
    2. Auto-upgrade: each fire spawns a fresh interpreter, so
       `pip install -U ...` automatically takes effect on the next tick.
       No explicit service restart is needed after upgrades.

    `interval_secs == 0` preserves the original long-running-daemon
    semantics for services that genuinely need a persistent process
    (e.g., an HTTP server).
    """

    name: str
    binary: str
    args: list[str]
    env: dict[str, str]
    service_id: str = ""
    interval_secs: int = 0

    def __post_init__(self):
        if not self.service_id:
            pkg = get_branding().package_name
            self.service_id = f"com.{pkg}.{self.name}"

    @property
    def is_periodic(self) -> bool:
        return self.interval_secs > 0


# ---------------------------------------------------------------------------
# macOS: launchd provider
# ---------------------------------------------------------------------------


class LaunchdProvider(ServiceProvider):
    """macOS launchd service management."""

    def name(self) -> str:
        return "launchd"

    def available(self) -> bool:
        return platform.system() == "Darwin"

    def _plist_path(self, svc: ServiceDef) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{svc.service_id}.plist"

    def _compute_plist(self, svc: ServiceDef) -> str:
        """Render the plist XML body for `svc`. Pure function — no I/O —
        so install() can compare against an existing on-disk file for
        idempotence."""
        binary_path = shutil.which(svc.binary) or svc.binary

        # Bounded service env per ADR-036 §D9 — bounded PATH always present;
        # HOME not default-injected; LANG/LC_ALL pass through; svc.env wins.
        full_env = _platform_env_for_service(svc.env, binary_path)
        entries = "\n".join(
            f"            <key>{k}</key>\n            <string>{v}</string>"
            for k, v in full_env.items()
        )
        env_xml = f"""
        <key>EnvironmentVariables</key>
        <dict>
{entries}
        </dict>"""

        args_xml = "\n".join(f"        <string>{a}</string>" for a in [binary_path] + svc.args)
        log_dir = _get_services_dir()
        log_dir.mkdir(parents=True, exist_ok=True)

        # Periodic mode: oneshot fired by StartInterval. KeepAlive here would
        # hot-loop the heartbeat (launchd respawns the moment it exits, hammering
        # any external API the heartbeat polls). Mirrors the systemd .timer path.
        if svc.is_periodic:
            schedule_xml = (
                f"    <key>StartInterval</key>\n"
                f"    <integer>{svc.interval_secs}</integer>"
            )
        else:
            schedule_xml = "    <key>KeepAlive</key>\n    <true/>"

        # Sandbox-light per ADR-036 §D10:
        # - ProcessType=Background marks this as a non-interactive daemon
        #   (lower scheduling priority than UI/Standard processes).
        # - LowPriorityIO yields the disk to interactive workloads.
        # - Future: SandboxProfilePath hook for sandbox-exec profiles
        #   authored in Phase 2/3 (RIVET vs TIDY have different shapes).
        sandbox_xml = """    <key>ProcessType</key>
    <string>Background</string>
    <key>LowPriorityIO</key>
    <true/>"""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{svc.service_id}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>RunAtLoad</key>
    <true/>
{schedule_xml}
{sandbox_xml}{env_xml}
    <key>StandardOutPath</key>
    <string>{log_dir / f"{svc.name}.stdout.log"}</string>
    <key>StandardErrorPath</key>
    <string>{log_dir / f"{svc.name}.stderr.log"}</string>
</dict>
</plist>
"""

    def install(self, svc: ServiceDef) -> bool:
        """Write the plist iff its content would change.

        macOS surfaces the "App Background Activity" toast every time a
        plist in `~/Library/LaunchAgents/` is touched — write + load.
        Skipping no-op writes silences the toast on re-installs /
        upgrades / repeated `axi install` runs (issue #208).
        """
        plist = self._compute_plist(svc)
        plist_path = self._plist_path(svc)
        try:
            if plist_path.exists() and plist_path.read_text(encoding="utf-8") == plist:
                log.debug("plist unchanged for %s — skipping write", svc.name)
                return True
        except OSError:
            pass  # unreadable → fall through to write
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist, encoding="utf-8")
        log.info("Wrote launchd plist: %s", plist_path)
        return True

    def _is_loaded(self, svc: ServiceDef) -> bool:
        """True iff `launchctl list <label>` exits 0 (agent loaded into
        the current session)."""
        try:
            result = subprocess.run(
                ["launchctl", "list", svc.service_id],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def start(self, svc: ServiceDef) -> bool:
        # If the agent is already loaded, `launchctl load` would re-fire
        # the Login Items toast on macOS (issue #208). Probe + skip.
        if self._is_loaded(svc):
            log.debug("already loaded: %s — skipping launchctl load", svc.name)
            return True
        try:
            subprocess.run(
                ["launchctl", "load", "-w", str(self._plist_path(svc))],
                capture_output=True,
                timeout=10,
            )
            return True
        except Exception as e:
            log.warning("launchctl load failed: %s", e)
            return False

    def stop(self, svc: ServiceDef) -> bool:
        try:
            subprocess.run(
                ["launchctl", "unload", str(self._plist_path(svc))],
                capture_output=True,
                timeout=10,
            )
            return True
        except Exception:
            return False

    def status(self, svc: ServiceDef) -> ServiceInfo:
        if not self._plist_path(svc).exists():
            return ServiceInfo(
                name=svc.name, status=ServiceStatus.NOT_INSTALLED, provider="launchd"
            )
        try:
            result = subprocess.run(
                ["launchctl", "list", svc.service_id],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return ServiceInfo(name=svc.name, status=ServiceStatus.RUNNING, provider="launchd")
            return ServiceInfo(name=svc.name, status=ServiceStatus.STOPPED, provider="launchd")
        except Exception:
            return ServiceInfo(name=svc.name, status=ServiceStatus.UNKNOWN, provider="launchd")

    def uninstall(self, svc: ServiceDef) -> bool:
        self.stop(svc)
        self._plist_path(svc).unlink(missing_ok=True)
        return True


# ---------------------------------------------------------------------------
# Linux: systemd provider
# ---------------------------------------------------------------------------


class SystemdProvider(ServiceProvider):
    """Linux systemd user service management."""

    def name(self) -> str:
        return "systemd"

    def available(self) -> bool:
        return platform.system() == "Linux" and shutil.which("systemctl") is not None

    def _unit_dir(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user"

    def _unit_path(self, svc: ServiceDef) -> Path:
        return self._unit_dir() / f"neut-{svc.name}.service"

    def _timer_path(self, svc: ServiceDef) -> Path:
        return self._unit_dir() / f"neut-{svc.name}.timer"

    def _primary_unit_name(self, svc: ServiceDef) -> str:
        """The unit systemctl commands should target.

        For periodic services we target the timer (enable/start/status
        operate on the scheduler). For long-running daemons we target the
        service directly.
        """
        suffix = "timer" if svc.is_periodic else "service"
        return f"neut-{svc.name}.{suffix}"

    @staticmethod
    def _write_if_changed(path: Path, content: str) -> bool:
        """Write content to path only if it differs from current.

        Returns True if the file was written (or created), False if the
        existing content already matched — the caller can use this to skip
        daemon-reload when nothing actually changed.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.exists() and path.read_text(encoding="utf-8") == content:
                return False
        except OSError:
            pass  # fall through to write
        path.write_text(content, encoding="utf-8")
        return True

    def _linger_enabled(self) -> bool:
        """Check whether lingering is enabled for the current user.

        Without linger, user services die when the SSH/login session ends and
        do NOT restart on reboot — the common root cause of daemon agents
        disappearing after a headless server reboot.
        """
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if not user:
            return False
        try:
            result = subprocess.run(
                ["loginctl", "show-user", user, "--property=Linger", "--value"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip().lower() == "yes"
        except Exception:
            return False

    def _try_enable_linger(self) -> tuple[bool, str]:
        """Attempt to enable linger via passwordless sudo.

        Returns (success, message). On failure, message is the exact command
        the operator should run manually. Never raises.
        """
        user = os.environ.get("USER") or os.environ.get("LOGNAME") or ""
        if not user:
            return False, "Could not determine $USER to enable linger"
        cmd_str = f"sudo loginctl enable-linger {user}"
        try:
            result = subprocess.run(
                ["sudo", "-n", "loginctl", "enable-linger", user],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                return True, f"Enabled linger for {user}"
            return False, (
                f"Linger not enabled; user services will NOT survive logout/reboot. "
                f"Run manually: {cmd_str}"
            )
        except Exception as exc:
            return False, f"{exc}; run manually: {cmd_str}"

    def install(self, svc: ServiceDef) -> bool:
        binary_path = shutil.which(svc.binary) or svc.binary
        exec_start = f"{binary_path} {' '.join(svc.args)}".strip()
        # Bounded service env per ADR-036 §D9 — bounded PATH always present;
        # HOME not default-injected; LANG/LC_ALL pass through; svc.env wins.
        full_env = _platform_env_for_service(svc.env, binary_path)
        env_lines = "\n".join(f"Environment={k}={v}" for k, v in full_env.items())
        # Sandbox-light defaults per ADR-036 §D10 — daemon agents must not have
        # unbounded read access to the user's filesystem. Manifests may relax
        # via [agent.sandbox] in Phase 2/3; Phase 0 is uniform per provider.
        sandbox_lines = _systemd_sandbox_directives(svc)
        product = get_branding().product_name

        changed = False

        if svc.is_periodic:
            # Type=oneshot + .timer pattern. Each tick spawns a fresh
            # process — resilient to crashes and auto-picks up new code
            # after `pip install -U`, without needing a restart.
            service_unit = f"""[Unit]
Description={product} managed: {svc.name} (periodic)

[Service]
Type=oneshot
ExecStart={exec_start}
{env_lines}
{sandbox_lines}
"""
            timer_unit = f"""[Unit]
Description={product} managed: {svc.name} timer — every {svc.interval_secs}s

[Timer]
OnBootSec=60
OnUnitActiveSec={svc.interval_secs}
Persistent=true
Unit=neut-{svc.name}.service

[Install]
WantedBy=timers.target
"""
            changed |= self._write_if_changed(self._unit_path(svc), service_unit)
            changed |= self._write_if_changed(self._timer_path(svc), timer_unit)
        else:
            # Original long-running daemon semantics — keep for services
            # that genuinely need a persistent process (e.g., HTTP server).
            service_unit = f"""[Unit]
Description={product} managed: {svc.name}
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
{env_lines}
{sandbox_lines}

[Install]
WantedBy=default.target
"""
            # If a stale timer exists from a previous periodic config,
            # remove it so we don't end up with a double-schedule.
            stale_timer = self._timer_path(svc)
            if stale_timer.exists():
                subprocess.run(
                    ["systemctl", "--user", "disable", "--now", f"neut-{svc.name}.timer"],
                    capture_output=True,
                )
                stale_timer.unlink(missing_ok=True)
                changed = True
            changed |= self._write_if_changed(self._unit_path(svc), service_unit)

        if changed:
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            log.info("Wrote systemd unit(s) for %s", svc.name)
        else:
            log.debug("Unit content unchanged for %s — skipping daemon-reload", svc.name)

        subprocess.run(
            ["systemctl", "--user", "enable", self._primary_unit_name(svc)],
            capture_output=True,
        )

        # Ensure linger is enabled so user services survive logout + reboot.
        if not self._linger_enabled():
            ok, msg = self._try_enable_linger()
            if ok:
                log.info(msg)
            else:
                log.warning(msg)
        return True

    def start(self, svc: ServiceDef) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "start", self._primary_unit_name(svc)],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def stop(self, svc: ServiceDef) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "stop", self._primary_unit_name(svc)],
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def status(self, svc: ServiceDef) -> ServiceInfo:
        if not self._unit_path(svc).exists():
            return ServiceInfo(
                name=svc.name, status=ServiceStatus.NOT_INSTALLED, provider="systemd"
            )
        try:
            # For periodic services the timer is the thing that's "active".
            # For one-shot services driven by a timer the .service itself is
            # rarely "active" (it runs briefly then returns inactive). So we
            # probe the primary unit (timer for periodic, service for daemon).
            result = subprocess.run(
                ["systemctl", "--user", "is-active", self._primary_unit_name(svc)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            active = result.stdout.strip()
            # Map systemd states onto our coarse ServiceStatus values.
            # See systemd.unit(5): active | reloading | inactive |
            # failed | activating | deactivating.
            status_map = {
                "active": ServiceStatus.RUNNING,
                "reloading": ServiceStatus.RUNNING,
                "activating": ServiceStatus.STARTING,
                "deactivating": ServiceStatus.STOPPED,
                "inactive": ServiceStatus.STOPPED,
                "failed": ServiceStatus.FAILED,
            }
            mapped = status_map.get(active, ServiceStatus.UNKNOWN)
            return ServiceInfo(
                name=svc.name,
                status=mapped,
                provider="systemd",
                message=active,
            )
        except Exception:
            return ServiceInfo(name=svc.name, status=ServiceStatus.UNKNOWN, provider="systemd")

    def uninstall(self, svc: ServiceDef) -> bool:
        # Stop + disable both the service and the timer so we don't leave
        # an orphaned periodic schedule behind.
        for unit_suffix in ("timer", "service"):
            unit = f"neut-{svc.name}.{unit_suffix}"
            subprocess.run(
                ["systemctl", "--user", "disable", "--now", unit],
                capture_output=True,
            )
        self._unit_path(svc).unlink(missing_ok=True)
        self._timer_path(svc).unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return True


# ---------------------------------------------------------------------------
# Windows: Task Scheduler provider
# ---------------------------------------------------------------------------


class WindowsTaskProvider(ServiceProvider):
    """Windows Task Scheduler service management."""

    def name(self) -> str:
        return "windows_task"

    def available(self) -> bool:
        return platform.system() == "Windows"

    def install(self, svc: ServiceDef) -> bool:
        binary_path = shutil.which(svc.binary) or svc.binary
        args_str = " ".join(svc.args)
        task_name = f"{get_branding().product_name}_{svc.name}"
        try:
            subprocess.run(
                [
                    "schtasks",
                    "/Create",
                    "/TN",
                    task_name,
                    "/TR",
                    f'"{binary_path}" {args_str}',
                    "/SC",
                    "ONLOGON",
                    "/RL",
                    "LIMITED",
                    "/F",
                ],
                capture_output=True,
                check=True,
                timeout=15,
                **_WIN_NO_WINDOW,
            )
            log.info("Created Windows task: %s", task_name)
            return True
        except Exception as e:
            log.warning("schtasks create failed: %s", e)
            return False

    def start(self, svc: ServiceDef) -> bool:
        try:
            result = subprocess.run(
                ["schtasks", "/Run", "/TN", f"{get_branding().product_name}_{svc.name}"],
                capture_output=True,
                timeout=10,
                **_WIN_NO_WINDOW,
            )
            return result.returncode == 0
        except Exception:
            return False

    def stop(self, svc: ServiceDef) -> bool:
        try:
            result = subprocess.run(
                ["schtasks", "/End", "/TN", f"{get_branding().product_name}_{svc.name}"],
                capture_output=True,
                timeout=10,
                **_WIN_NO_WINDOW,
            )
            return result.returncode == 0
        except Exception:
            return False

    def status(self, svc: ServiceDef) -> ServiceInfo:
        try:
            result = subprocess.run(
                [
                    "schtasks",
                    "/Query",
                    "/TN",
                    f"{get_branding().product_name}_{svc.name}",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                **_WIN_NO_WINDOW,
            )
            if result.returncode != 0:
                return ServiceInfo(
                    name=svc.name, status=ServiceStatus.NOT_INSTALLED, provider="windows_task"
                )
            if "Running" in result.stdout:
                return ServiceInfo(
                    name=svc.name, status=ServiceStatus.RUNNING, provider="windows_task"
                )
            return ServiceInfo(name=svc.name, status=ServiceStatus.STOPPED, provider="windows_task")
        except Exception:
            return ServiceInfo(name=svc.name, status=ServiceStatus.UNKNOWN, provider="windows_task")

    def uninstall(self, svc: ServiceDef) -> bool:
        self.stop(svc)
        try:
            subprocess.run(
                ["schtasks", "/Delete", "/TN", f"{get_branding().product_name}_{svc.name}", "/F"],
                capture_output=True,
                timeout=10,
                **_WIN_NO_WINDOW,
            )
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Fallback: subprocess provider (no persistence)
# ---------------------------------------------------------------------------


class SubprocessProvider(ServiceProvider):
    """Fallback: raw background process. Does not survive reboots."""

    def name(self) -> str:
        return "subprocess"

    def available(self) -> bool:
        return True  # Always available as last resort

    def install(self, svc: ServiceDef) -> bool:
        return True  # No-op — subprocess doesn't persist

    def start(self, svc: ServiceDef) -> bool:
        binary_path = shutil.which(svc.binary) or svc.binary
        try:
            env = {**os.environ, **svc.env}
            subprocess.Popen(
                [binary_path] + svc.args,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                **_WIN_NO_WINDOW,
            )
            return True
        except Exception as e:
            log.warning("subprocess start failed: %s", e)
            return False

    def stop(self, svc: ServiceDef) -> bool:
        return False  # Can't reliably stop a detached subprocess

    def status(self, svc: ServiceDef) -> ServiceInfo:
        return ServiceInfo(name=svc.name, status=ServiceStatus.UNKNOWN, provider="subprocess")

    def uninstall(self, svc: ServiceDef) -> bool:
        return True  # Nothing to uninstall


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------

_PROVIDERS: list[ServiceProvider] = [
    LaunchdProvider(),
    SystemdProvider(),
    WindowsTaskProvider(),
    SubprocessProvider(),  # Always-available fallback
]


class ServiceManager:
    """Facade that picks the best available provider and delegates."""

    def __init__(
        self,
        name: str,
        binary: str,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        interval_secs: int = 0,
        service_id: str = "",
    ):
        self._svc = ServiceDef(
            name=name,
            binary=binary,
            args=args or [],
            env=env or {},
            interval_secs=interval_secs,
            service_id=service_id,
        )
        self._provider = self._pick_provider()

    def _pick_provider(self) -> ServiceProvider:
        for p in _PROVIDERS:
            if p.available():
                return p
        return SubprocessProvider()  # Should never happen

    @property
    def provider_name(self) -> str:
        return self._provider.name()

    def install(self) -> bool:
        return self._provider.install(self._svc)

    def start(self) -> bool:
        return self._provider.start(self._svc)

    def stop(self) -> bool:
        return self._provider.stop(self._svc)

    def status(self) -> ServiceInfo:
        return self._provider.status(self._svc)

    def uninstall(self) -> bool:
        return self._provider.uninstall(self._svc)
