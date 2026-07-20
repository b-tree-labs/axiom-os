# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Consumer environment installer.

Reads runtime/config/install.toml, detects the current environment by hostname
or NEUT_ENV, and runs the declared steps idempotently.

Step types:
  connect       — delegates to axi connect <name> setup flow
  settings      — writes a key=value via SettingsStore
  port_forward  — installs a persistent kubectl port-forward service (launchd/systemd)
  rag_index     — indexes one or more paths into the RAG store
  shell         — runs an arbitrary shell command (use sparingly)

State is tracked in .neut/install-state.json — a step is skipped if it was
already completed successfully in a prior run.  Pass --force to re-run all steps.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.config_loader import load_json
from axiom.infra.state import atomic_write

log = logging.getLogger(__name__)

_INSTALL_TOML = _REPO_ROOT / "runtime" / "config" / "install.toml"
_INSTALL_LOCAL_TOML = _REPO_ROOT / "runtime" / "config" / "install.local.toml"
_INSTALL_EXAMPLE_TOML = _REPO_ROOT / "runtime" / "config.example" / "install.toml"
_STATE_PATH = _REPO_ROOT / ".neut" / "install-state.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class InstallStep:
    id: str
    type: str                            # connect | settings | port_forward | rag_index | pack_install | shell
    description: str = ""
    depends_on: str = ""                 # step id that must complete first

    # connect
    connection: str = ""

    # settings
    key: str = ""
    value: Any = None

    # port_forward
    namespace: str = "neut"
    service: str = ""
    local_port: int = 0
    remote_port: int = 0
    persistent: bool = True              # install as launchd/systemd service

    # rag_index
    paths: list[str] = field(default_factory=list)
    corpus: str = ""                     # override corpus (default: rag-internal)

    # pack_install
    pack_id: str = ""
    pack_version: str = ""               # empty = latest
    server: str = "pack-server"          # connection name of the pack server

    # shell
    command: str = ""
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class Environment:
    name: str
    description: str = ""
    match_hostname: list[str] = field(default_factory=list)  # glob patterns
    match_env: str = ""                  # NEUT_ENV=<value>
    default: bool = False                # fallback when no hostname matches
    steps: list[InstallStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Manifest loading
# ---------------------------------------------------------------------------

def _load_toml(path: Path) -> dict:
    from axiom.infra.toml_compat import load_toml
    return load_toml(path)


def _parse_environments(data: dict) -> list[Environment]:
    envs = []
    for raw_env in data.get("environments", []):
        steps = []
        for raw_step in raw_env.get("steps", []):
            steps.append(InstallStep(
                id=raw_step["id"],
                type=raw_step["type"],
                description=raw_step.get("description", ""),
                depends_on=raw_step.get("depends_on", ""),
                connection=raw_step.get("connection", ""),
                key=raw_step.get("key", ""),
                value=raw_step.get("value"),
                namespace=raw_step.get("namespace", "neut"),
                service=raw_step.get("service", ""),
                local_port=raw_step.get("local_port", 0),
                remote_port=raw_step.get("remote_port", 0),
                persistent=raw_step.get("persistent", True),
                paths=raw_step.get("paths", []),
                corpus=raw_step.get("corpus", ""),
                pack_id=raw_step.get("pack_id", ""),
                pack_version=raw_step.get("pack_version", ""),
                server=raw_step.get("server", "pack-server"),
                command=raw_step.get("command", ""),
                env=raw_step.get("env", {}),
            ))
        envs.append(Environment(
            name=raw_env["name"],
            description=raw_env.get("description", ""),
            match_hostname=_as_list(raw_env.get("match_hostname", [])),
            match_env=raw_env.get("match_env", ""),
            default=raw_env.get("default", False),
            steps=steps,
        ))
    return envs


def load_manifest() -> list[Environment]:
    """Load install manifest, merging install.local.toml on top of install.toml.

    Resolution order (highest priority last — later steps append/override):
      1. config.example/install.toml   (shared template, always read as base)
      2. config/install.toml           (local full override if present)
      3. config/install.local.toml     (local-only additions, always merged in)

    install.local.toml appends extra steps to matching environments by name,
    and creates new environments if none match. This lets a machine-specific
    warm-up (e.g. one-time RAG ingest of a local knowledge dump) live in its
    own small file without duplicating the full template.
    """
    # Determine base file. If the user has not yet customized
    # config/install.toml, auto-init it from the shipped example so the
    # very first `axi install` / `neut install` invocation has something
    # to load — no "copy this file" pre-step. The user can edit the
    # generated file in place; subsequent runs read the customized
    # version. If neither file exists (e.g. the example was not shipped
    # in this distribution), fall back to the empty-envs path so the
    # caller can emit a useful diagnostic instead of crashing.
    if not _INSTALL_TOML.exists() and _INSTALL_EXAMPLE_TOML.exists():
        try:
            _INSTALL_TOML.parent.mkdir(parents=True, exist_ok=True)
            _INSTALL_TOML.write_text(_INSTALL_EXAMPLE_TOML.read_text())
            log.info(
                "Initialized %s from %s (first-run auto-init)",
                _INSTALL_TOML,
                _INSTALL_EXAMPLE_TOML,
            )
        except OSError as exc:
            # Read-only filesystem, permission denied, etc. — log and
            # continue with the example as the base; the install step
            # itself will fail loudly if state can't be persisted.
            log.warning("Could not auto-init %s: %s", _INSTALL_TOML, exc)

    base_path = _INSTALL_TOML if _INSTALL_TOML.exists() else _INSTALL_EXAMPLE_TOML
    if not base_path.exists():
        return []

    envs = _parse_environments(_load_toml(base_path))

    # Merge install.local.toml additions on top
    if _INSTALL_LOCAL_TOML.exists():
        local_envs = _parse_environments(_load_toml(_INSTALL_LOCAL_TOML))
        env_by_name = {e.name: e for e in envs}
        for local_env in local_envs:
            if local_env.name in env_by_name:
                # Append steps; existing step IDs win (no duplicates)
                existing_ids = {s.id for s in env_by_name[local_env.name].steps}
                for step in local_env.steps:
                    if step.id not in existing_ids:
                        env_by_name[local_env.name].steps.append(step)
            else:
                envs.append(local_env)

    return envs


def _as_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        return [v]
    return []


# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

def detect_environment(envs: list[Environment], override: str = "") -> Environment | None:
    """Return the first environment matching the current host."""
    if override:
        for env in envs:
            if env.name == override:
                return env
        return None

    # NEUT_ENV env var takes priority
    neut_env = os.environ.get("NEUT_ENV", "")
    if neut_env:
        for env in envs:
            if neut_env in (env.name, env.match_env):
                return env

    # Hostname glob match
    hostname = socket.gethostname().lower()
    for env in envs:
        for pattern in env.match_hostname:
            if fnmatch.fnmatch(hostname, pattern.lower()):
                return env

    # Fall back to the environment marked default = true
    for env in envs:
        if env.default:
            return env

    return None


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def _load_state() -> dict[str, bool]:
    return load_json(_STATE_PATH)


def _save_state(state: dict[str, bool]) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(_STATE_PATH, state)


# ---------------------------------------------------------------------------
# Step runners
# ---------------------------------------------------------------------------

def run_step(step: InstallStep, state: dict[str, bool], force: bool = False) -> bool:
    """Run a single step. Returns True on success. Updates state in-place."""
    if state.get(step.id) and not force:
        print(f"  ✓ {step.description or step.id}  (already done)")
        return True

    if step.depends_on and not state.get(step.depends_on):
        print(f"  ⚠ Skipping '{step.id}' — dependency '{step.depends_on}' not completed")
        return False

    print(f"\n  ▶ {step.description or step.id}")

    try:
        if step.type == "connect":
            ok = _run_connect(step)
        elif step.type == "settings":
            ok = _run_settings(step)
        elif step.type == "port_forward":
            ok = _run_port_forward(step)
        elif step.type == "rag_index":
            ok = _run_rag_index(step)
        elif step.type == "pack_install":
            ok = _run_pack_install(step)
        elif step.type == "shell":
            ok = _run_shell(step)
        else:
            print(f"    Unknown step type: {step.type}")
            return False

        if ok:
            state[step.id] = True
            return True
        return False
    except KeyboardInterrupt:
        print("\n    Skipped by user")
        return False
    except Exception as e:
        print(f"    ✗ Error: {e}")
        return False


def _run_connect(step: InstallStep) -> bool:
    from axiom.extensions.builtins.connect.cli import setup_connection
    from axiom.infra.connections import get_registry
    return setup_connection(step.connection, get_registry()) == 0


def _run_settings(step: InstallStep) -> bool:
    from axiom.extensions.builtins.settings.store import SettingsStore
    value = step.value
    # Expand env vars in string values
    if isinstance(value, str):
        value = os.path.expandvars(value)
    SettingsStore().set(step.key, value)
    print(f"    {step.key} = {value}")
    return True


def _run_port_forward(step: InstallStep) -> bool:
    """Install (or verify) a kubectl port-forward as a managed background service."""
    if not step.service or not step.local_port:
        print("    ✗ port_forward step requires service and local_port")
        return False

    # Check if already forwarded
    import socket as _socket
    try:
        with _socket.create_connection(("localhost", step.local_port), timeout=1):
            print(f"    ✓ localhost:{step.local_port} already reachable")
            if not step.persistent:
                return True
    except OSError:
        pass

    if step.persistent:
        return _install_port_forward_service(step)
    else:
        return _start_port_forward_once(step)


def _install_port_forward_service(step: InstallStep) -> bool:
    """Register kubectl port-forward as a launchd/systemd managed service."""
    try:
        from axiom.infra.services import ServiceManager
    except ImportError:
        print("    ServiceManager not available — falling back to background process")
        return _start_port_forward_once(step)

    svc_name = f"neut-pf-{step.service.replace('/', '-')}-{step.local_port}"
    kubectl = _find_kubectl()
    if not kubectl:
        print("    ✗ kubectl not found — install kubectl or run port-forward manually:")
        print(f"      kubectl port-forward -n {step.namespace} svc/{step.service} "
              f"{step.local_port}:{step.remote_port}")
        return False

    cmd_args = [
        "port-forward",
        "-n", step.namespace,
        f"svc/{step.service}",
        f"{step.local_port}:{step.remote_port or step.local_port}",
    ]
    svc = ServiceManager(
        name=svc_name,
        binary=kubectl,
        args=cmd_args,
    )
    svc.install()
    svc.start()

    import socket as _socket
    import time
    for _ in range(8):
        time.sleep(1)
        try:
            with _socket.create_connection(("localhost", step.local_port), timeout=1):
                print(f"    ✓ port-forward running as managed service ({svc_name})")
                print(f"      localhost:{step.local_port} → {step.namespace}/{step.service}:{step.remote_port}")
                return True
        except OSError:
            pass

    print(f"    ⚠ Service installed but port {step.local_port} not yet responsive")
    print(f"      Check: kubectl get pods -n {step.namespace}")
    return False


def _start_port_forward_once(step: InstallStep) -> bool:
    """Start a one-shot background port-forward (not persistent across reboots)."""
    kubectl = _find_kubectl()
    if not kubectl:
        print("    ✗ kubectl not found")
        return False
    cmd = [
        kubectl, "port-forward",
        "-n", step.namespace,
        f"svc/{step.service}",
        f"{step.local_port}:{step.remote_port or step.local_port}",
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import socket as _socket
    import time
    for _ in range(5):
        time.sleep(1)
        try:
            with _socket.create_connection(("localhost", step.local_port), timeout=1):
                print("    ✓ port-forward started (background, not persistent)")
                return True
        except OSError:
            pass
    print("    ⚠ port-forward started but not yet responsive")
    return False


def _run_rag_index(step: InstallStep) -> bool:
    from axiom.extensions.builtins.settings.store import SettingsStore
    db_url = SettingsStore().get("rag.database_url", "")
    if not db_url:
        print("    ✗ rag.database_url not set — run 'axi connect postgresql' first")
        return False

    from axiom.rag.ingest import ingest_path
    from axiom.rag.store import CORPUS_INTERNAL, RAGStore

    corpus = step.corpus or CORPUS_INTERNAL
    store = RAGStore(db_url)
    store.connect()

    for p in step.paths:
        path = Path(p) if Path(p).is_absolute() else _REPO_ROOT / p
        if not path.exists():
            print(f"    ⚠ Path not found: {path}")
            continue
        print(f"    Indexing {path} → {corpus}...")
        ingest_path(path, store, corpus=corpus)
        print(f"    ✓ {path}")

    store.close()
    return True


def _run_pack_install(step: InstallStep) -> bool:
    """Download and install a domain pack from a registered pack server."""
    if not step.pack_id:
        print("    ✗ pack_install step requires pack_id")
        return False

    # Resolve pack server URL from settings
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore
        settings = SettingsStore()
        server_url = settings.get(f"rag.pack_server_url.{step.server}", "") or settings.get("rag.pack_server_url", "")
        server_key = settings.get(f"rag.pack_server_key.{step.server}", "") or settings.get("rag.pack_server_key", "")
    except Exception as e:
        print(f"    ✗ Could not read pack server settings: {e}")
        return False

    if not server_url:
        print(f"    ✗ No pack server URL configured — run: axi connect {step.server}")
        return False

    version_part = f"/{step.pack_version}" if step.pack_version else "/latest"
    download_url = f"{server_url.rstrip('/')}/packs/{step.pack_id}{version_part}.neutpack"

    print(f"    Downloading {step.pack_id}{(' v' + step.pack_version) if step.pack_version else ' (latest)'}...")

    # Delegate to axi rag pack install
    cmd = ["neut", "rag", "pack", "install", download_url]
    if server_key:
        import os
        env = {**os.environ, "PACK_SERVER_KEY": server_key}
    else:
        env = None

    try:
        result = subprocess.run(cmd, env=env)
        return result.returncode == 0
    except FileNotFoundError:
        print("    ✗ axi CLI not found in PATH")
        return False


def _run_shell(step: InstallStep) -> bool:
    env = {**os.environ, **step.env}
    result = subprocess.run(step.command, shell=True, env=env)
    return result.returncode == 0


def _find_kubectl() -> str | None:
    import shutil
    return shutil.which("kubectl")
