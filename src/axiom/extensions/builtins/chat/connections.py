# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Post-setup hooks and lifecycle management for neut_agent connections.

Called by neut connect when a connection declares post_setup_module
pointing here. Keeps tool-specific setup logic in the owning extension.

Uses infra.services.ServiceManager for persistent service lifecycle
(launchd on macOS, systemd on Linux) — independent of how the binary
was installed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
import urllib.request

log = logging.getLogger(__name__)

_OLLAMA_ENV = {
    "OLLAMA_FLASH_ATTENTION": "1",
    "OLLAMA_KV_CACHE_TYPE": "q8_0",
}


def _get_ollama_service():
    """Create a ServiceManager for Ollama."""
    from axiom.infra.services import ServiceManager

    binary = shutil.which("ollama") or "ollama"
    return ServiceManager(
        name="ollama",
        binary=binary,
        args=["serve"],
        env=_OLLAMA_ENV,
    )


def setup_ollama() -> int:
    """Post-install hook for Ollama: register as managed service + pull model.

    Called by `neut connect ollama` after installation. Installs a
    launchd/systemd service so Ollama runs persistently (survives reboots)
    with optimized settings. The user never thinks about Ollama again.
    """
    from axiom.extensions.builtins.settings.store import SettingsStore

    settings = SettingsStore()
    model = settings.get("routing.ollama_model", "llama3.2:1b")

    if not _is_ollama_serving():
        svc = _get_ollama_service()
        svc.install()
        svc.start()
        print("  Registering Ollama as managed service...")

        for _ in range(8):
            time.sleep(1)
            if _is_ollama_serving():
                print("  \u2713 Ollama running (managed service, starts at login)")
                break
        else:
            print("  \u26a0 Ollama service didn't start")
            svc_info = svc.status()
            print(f"    Status: {svc_info.status}")
            print("    Logs: ~/.neut/services/ollama.stderr.log")
            return 1
    else:
        print("  \u2713 Ollama already running")

    # Pull routing model if needed
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if model in result.stdout:
            print(f"  \u2713 Model {model} ready")
            print()
            return 0
    except Exception:
        pass

    print(f"  Pulling routing model ({model})...")
    try:
        subprocess.run(
            ["ollama", "pull", model],
            check=True,
            timeout=300,
        )
        print(f"  \u2713 Model {model} ready")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print(f"  \u2717 Pull failed \u2014 try manually: ollama pull {model}")
        return 1

    print()
    return 0


def ensure_ollama_running() -> bool:
    """Silently ensure Ollama is serving. Called by the router before inference.

    Returns True if Ollama is available, False if not installed or won't start.
    Never prompts, never prints — this is a background operation.
    """
    if not shutil.which("ollama"):
        return False

    if _is_ollama_serving():
        return True

    # Try to start via managed service
    try:
        svc = _get_ollama_service()
        info = svc.status()

        from axiom.infra.services import ServiceStatus

        if info.status == ServiceStatus.NOT_INSTALLED:
            # Install and start
            svc.install()
            svc.start()
        elif info.status == ServiceStatus.STOPPED:
            svc.start()
        # else: already running or unknown — try anyway

    except Exception as e:
        log.debug("Service manager failed, trying direct start: %s", e)
        # Fallback: raw Popen (e.g., services module not available)
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return False

    # Wait for it to come up
    for _ in range(5):
        time.sleep(0.5)
        if _is_ollama_serving():
            log.info("Ollama auto-started via managed service")
            return True

    log.debug("Ollama auto-start failed")
    return False


def setup_llm_provider() -> int:
    """Interactive LLM provider selection — called by `neut connect llm-provider`.

    Presents a menu of all configured cloud LLM providers (Anthropic, OpenAI,
    any others in llm-providers.toml). User picks one, enters their key.
    Also surfaces private-network providers (a self-hosted node, etc.) as options.
    """
    from axiom.infra.connections import get_registry, store_credential

    registry = get_registry()
    providers = [c for c in registry.all() if c.category == "llm" and c.kind == "api"]

    if not providers:
        print("  No LLM providers configured in extension manifests.")
        return 1

    print("\n  Choose your LLM provider(s):")
    print("  (You can set up more than one — neut routes automatically)\n")

    for i, conn in enumerate(providers, 1):
        import os

        key = os.environ.get(conn.credential_env_var, "") if conn.credential_env_var else ""
        status = " ✓ (key set)" if key else ""
        print(f"    {i}. {conn.display_name}{status}")

    print(f"    {len(providers) + 1}. Skip — I'll set keys manually later")
    print()

    configured_any = False
    for conn in providers:
        import os

        if os.environ.get(conn.credential_env_var or "", ""):
            configured_any = True
            continue  # already set

        try:
            answer = input(f"  Set up {conn.display_name}? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Skipped")
            break

        if answer not in ("y", "yes"):
            continue

        if conn.docs_url:
            print(f"  Get your key: {conn.docs_url}\n")

        try:
            value = input(f"  Paste {conn.display_name} API key: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Skipped")
            continue

        if value:
            store_credential(conn.name, value)
            os.environ[conn.credential_env_var] = value
            print(f"  ✓ {conn.display_name} key saved")
            configured_any = True

    if not configured_any:
        print("\n  No providers configured. neut will use Ollama locally if available.")
        print("  Set keys later:")
        for conn in providers:
            if conn.credential_env_var:
                print(f"    neut connect {conn.name}")

    print()
    return 0


def setup_private_network_llm() -> int:
    """Post-setup hook for a private-network LLM provider (e.g. a self-hosted node).

    Reads all connection details (endpoint, credential_env_var, health_endpoint,
    vpn_name, vpn_connect_guide) from the 'private-llm' connection definition in
    the extension manifest.  No values are hardcoded here — the manifest is the
    single source of truth, so any deployment can point this at its own host.
    """
    return _setup_private_network_llm("private-llm")


def _setup_private_network_llm(connection_name: str) -> int:
    """Generic setup for any private-network LLM provider.

    Reads connection metadata from the registry (populated from axiom-extension.toml)
    so this function never hardcodes hostnames, ports, model names, or credentials.

    Args:
        connection_name: The `name` field from the [[connections]] block in the manifest.
    """
    import socket

    from axiom import REPO_ROOT

    # -- Resolve connection metadata from the registry -----------------------
    conn = None
    try:
        from axiom.infra.connections import get_registry

        conn = get_registry().get(connection_name)
    except Exception:
        pass

    if conn is None:
        print(f"  \u2717 Connection '{connection_name}' not found in registry.")
        return 1

    endpoint = conn.endpoint or ""
    cred_env = conn.credential_env_var or ""
    display = conn.display_name or connection_name
    vpn_name = conn.vpn_name or "VPN"
    vpn_guide = conn.vpn_connect_guide or f"Connect to {vpn_name} and retry."

    # health_endpoint may be "host:port" for tcp_connect checks
    health_host = ""
    health_port = 0
    if conn.health_endpoint:
        try:
            h, p = conn.health_endpoint.rsplit(":", 1)
            health_host, health_port = h, int(p)
        except ValueError:
            pass

    # -- Locate llm-providers.toml (support legacy models.toml name) ---------
    config_dir = REPO_ROOT / "runtime" / "config"
    providers_path = config_dir / "llm-providers.toml"
    if not providers_path.exists():
        providers_path = config_dir / "models.toml"
    if not providers_path.exists():
        print("  \u2717 llm-providers.toml not found — run: neut config")
        return 1

    content = providers_path.read_text(encoding="utf-8")

    # Pull LLM-specific fields from connection (added to Connection dataclass)
    model = getattr(conn, "model", "") or connection_name
    routing_tier = getattr(conn, "routing_tier", "") or "restricted"
    verify_ssl = getattr(conn, "verify_ssl", True)

    # -- Check if already configured -----------------------------------------
    if f'name = "{connection_name}"' in content:
        print(f"  \u2713 {display} already in llm-providers.toml")
    else:
        new_block = (
            f"\n[[gateway.providers]]\n"
            f'name = "{connection_name}"\n'
            f'endpoint = "{endpoint}"\n'
            f'model = "{model}"\n'
            f'api_key_env = "{cred_env}"\n'
            f"priority = 1\n"
            f'routing_tier = "{routing_tier}"\n'
            f'routing_tags = ["{routing_tier}", "private_network"]\n'
            f"requires_vpn = true\n"
            f"verify_ssl = {'true' if verify_ssl else 'false'}\n"
            f'use_for = ["extraction", "synthesis", "fallback"]\n'
        )
        providers_path.write_text(content.rstrip() + "\n" + new_block, encoding="utf-8")
        print(f"  \u2713 Added {display} to llm-providers.toml")
        if routing_tier == "restricted":
            print("    routing_tier = restricted (private LLM, falls back to cloud when VPN down)")
        else:
            print(f"    routing_tier = {routing_tier}")

    # -- Set routing preference in settings ----------------------------------
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        settings = SettingsStore()
        current_prefer = settings.get("routing.prefer_provider", [])
        if isinstance(current_prefer, str):
            current_prefer = [p.strip() for p in current_prefer.split(",") if p.strip()]
        if connection_name not in current_prefer:
            settings.set("routing.prefer_provider", [connection_name] + list(current_prefer))
            settings.set("routing.prefer_when", "reachable")
            print(f"  \u2713 Set routing.prefer_provider = [{connection_name}]")
            print(f"    When {vpn_name} is up → {display}")
            print(f"    When {vpn_name} is down → cloud fallback")
        else:
            print(f"  \u2713 routing.prefer_provider already includes {connection_name}")
    except Exception as e:
        print(f"  \u26a0 Could not update routing settings: {e}")

    # -- Check VPN / network reachability ------------------------------------
    if health_host and health_port:
        try:
            with socket.create_connection((health_host, health_port), timeout=2):
                pass
            print(f"  \u2713 {vpn_name} connected — {display} is reachable")
        except OSError:
            print(f"  \u26a0 {display} not reachable — {vpn_guide}")
    else:
        print(f"  \u2139 No health endpoint configured for {display}; skipping reachability check")

    print()
    return 0


def _is_ollama_serving() -> bool:
    """Check if Ollama API is responding."""
    try:
        urllib.request.urlopen("http://localhost:11434", timeout=1)
        return True
    except Exception:
        return False
