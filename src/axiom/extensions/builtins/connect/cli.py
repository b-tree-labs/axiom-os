# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi connect` — manage external connections.

Usage:
    axi connect                    List all connections and status
    axi connect <name>             Set up a specific connection
    axi connect <name> --clear     Remove saved credentials
    axi connect --check            Health check all connections
    axi connect --json             Machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sys

# format_status_section and _connection_status_info are in infra/connections.py
# (shared platform layer, not extension-specific)
from axiom.infra.connections import (
    Connection,
    ConnectionHealth,
    ConnectionRegistry,
    HealthStatus,
    _connection_status_info,
    check_health,
    clear_credential,
    format_status_section,
    get_credential,
    get_install_command,
    get_registry,
    has_credential,
    run_post_setup_hook,
    store_credential,
)

# ---------------------------------------------------------------------------
# Health check all
# ---------------------------------------------------------------------------


def _check_all(registry: ConnectionRegistry, as_json: bool = False) -> int:
    """Run health checks on all connections. Returns 0 if all healthy."""
    connections = registry.all()
    results = []
    any_unhealthy = False

    for conn in sorted(connections, key=lambda c: c.name):
        if conn.health_check:
            health = check_health(conn.name, registry=registry)
        else:
            # No health check — just check credential
            has_cred = has_credential(conn.name, registry=registry)
            if conn.credential_type == "none" or conn.kind == "cli":
                from axiom.infra.connections import get_cli_tool

                tool = get_cli_tool(conn.name, registry=registry) if conn.kind == "cli" else True
                health = ConnectionHealth(
                    status=HealthStatus.HEALTHY if tool else HealthStatus.UNHEALTHY,
                    message="OK" if tool else "Not found",
                )
            elif has_cred:
                health = ConnectionHealth(status=HealthStatus.HEALTHY, message="Credential set")
            else:
                health = ConnectionHealth(
                    status=HealthStatus.UNHEALTHY if conn.required else HealthStatus.UNKNOWN,
                    message="No credential",
                )

        if health.status == HealthStatus.UNHEALTHY:
            any_unhealthy = True

        results.append(
            {
                "name": conn.name,
                "display_name": conn.display_name,
                "status": health.status,
                "message": health.message,
                "latency_ms": health.latency_ms,
            }
        )

    if as_json:
        print(json.dumps(results, indent=2))
    else:
        print("\n  Connection Health Check")
        print("  " + "\u2500" * 40)
        for r in results:
            symbol = (
                "\u2713"
                if r["status"] == "healthy"
                else ("\u26a0" if r["status"] in ("degraded", "unknown") else "\u2717")
            )
            latency = f" ({r['latency_ms']:.0f}ms)" if r["latency_ms"] > 0 else ""
            print(f"  {symbol} {r['display_name']:25s} {r['message']}{latency}")
        print()

    return 1 if any_unhealthy else 0


# ---------------------------------------------------------------------------
# Setup flow
# ---------------------------------------------------------------------------


def setup_connection(name: str, registry: ConnectionRegistry) -> int:
    """Interactive setup for a single connection."""
    conn = registry.get(name)
    if conn is None:
        print(f"\n  Connection not found: {name}")
        print("  Available connections:")
        for c in sorted(registry.all(), key=lambda c: c.name):
            print(f"    {c.name:20s} {c.display_name}")
        print()
        return 1

    print(f"\n  {conn.display_name}")
    print("  " + "\u2500" * len(conn.display_name))

    if conn.kind == "cli":
        return _setup_cli_tool(conn, registry)

    if conn.credential_type == "none":
        print("  No credentials needed for this connection.")
        print()
        return 0

    return _setup_api_credential(conn, registry)


def _setup_cli_tool(conn: Connection, registry: ConnectionRegistry) -> int:
    """Set up a CLI tool connection — install, configure, verify.

    Uses declarative install_commands from TOML and extensible
    post_setup hooks from the owning extension.
    """
    import subprocess

    from axiom.infra.connections import get_cli_tool

    binary = conn.endpoint or conn.name
    tool = get_cli_tool(conn.name, registry=registry)

    if tool:
        print(f"  \u2713 Installed at {tool.path}")
        if tool.version:
            print(f"    Version: {tool.version}")
    else:
        print(f"  \u2717 {binary} not found on PATH\n")

        # Use declarative install_commands from TOML
        install_cmd = get_install_command(conn)
        if install_cmd:
            try:
                answer = input(f"  Install with `{install_cmd}`? [Y/n] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  Skipped")
                return 0

            if answer in ("", "y", "yes"):
                print("  Installing...")
                try:
                    subprocess.run(
                        install_cmd,
                        shell=True,
                        check=True,
                        timeout=120,
                    )
                    print("  \u2713 Installed")
                    tool = get_cli_tool(conn.name, registry=registry)
                except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                    print(f"  \u2717 Install failed: {e}")
                    if conn.docs_url:
                        print(f"  Manual install: {conn.docs_url}")
                    print()
                    return 1
            else:
                if conn.docs_url:
                    print(f"  Install manually: {conn.docs_url}")
                print()
                return 0
        else:
            if conn.docs_url:
                print(f"  Install: {conn.docs_url}")
            print()
            return 0

    # Run extension-provided post-setup hook if declared
    hook_result = run_post_setup_hook(conn)
    if hook_result >= 0:
        return hook_result

    print()
    return 0


def _setup_with_auth_method_choice(conn: Connection, registry: ConnectionRegistry) -> int:
    """Present auth method options and execute the chosen one."""
    print("  Choose auth method:\n")
    for i, method in enumerate(conn.auth_methods, 1):
        desc = method.get("description", method.get("method", "Unknown"))
        print(f"    {i}. {desc}")
    print()

    try:
        choice = input("  > ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Skipped")
        return 0

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(conn.auth_methods):
            print("  Invalid choice")
            return 1
    except ValueError:
        print("  Invalid choice")
        return 1

    selected = conn.auth_methods[idx]
    method_type = selected.get("method", "")

    if method_type == "browser":
        return _setup_browser_auth(conn, selected)
    elif method_type in ("graph_api", "api_key"):
        # Show credential prompt with method-specific env var
        env_var = selected.get("credential_env_var", conn.credential_env_var)
        docs = selected.get("docs_url", conn.docs_url)
        if docs:
            print(f"\n  Get your key: {docs}\n")
        try:
            value = input("  Paste key (Enter to skip): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Skipped")
            return 0
        if not value:
            if env_var:
                print(f"  Or set: export {env_var}=<key>")
            print()
            return 0
        store_credential(conn.name, value)
        print("  \u2713 Saved")
        print()
        return 0
    elif method_type == "manual":
        print("\n  Place files manually in the expected location.")
        if conn.credential_file:
            print(f"  Path: ~/.neut/credentials/{conn.credential_file}")
        print()
        return 0
    else:
        print(f"\n  Unknown auth method: {method_type}")
        return 1


def _setup_browser_auth(conn: Connection, method: dict) -> int:
    """Launch browser for authentication (Playwright)."""

    from axiom.infra.paths import get_user_state_dir

    print("\n  Launching browser for login...")
    print("  Complete login + MFA in the browser window.\n")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  Playwright not installed. Installing...")
        import subprocess

        try:
            subprocess.run(
                ["pip", "install", "playwright"],
                capture_output=True,
                check=True,
                timeout=60,
            )
            subprocess.run(
                ["playwright", "install", "chromium"],
                capture_output=True,
                check=True,
                timeout=120,
            )
            from playwright.sync_api import sync_playwright
        except Exception as e:
            print(f"  \u2717 Could not install Playwright: {e}")
            print("  Run manually: pip install playwright && playwright install chromium")
            return 1

    # Launch browser to the connection's endpoint
    endpoint = method.get("endpoint", conn.endpoint)
    cred_dir = get_user_state_dir() / "credentials" / conn.name
    cred_dir.mkdir(parents=True, exist_ok=True)
    state_path = cred_dir / "state.json"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()

            if state_path.exists():
                # Resume from saved session
                context = browser.new_context(storage_state=str(state_path))

            page = context.new_page()
            page.goto(endpoint)

            print("  Waiting for you to complete login...")
            print("  (Close the browser window when done)\n")

            try:
                page.wait_for_timeout(300_000)  # 5 min max
            except Exception:
                pass  # Browser closed by user

            # Save session state
            context.storage_state(path=str(state_path))
            state_path.chmod(0o600)
            browser.close()

        print(f"  \u2713 Session saved to ~/.neut/credentials/{conn.name}/state.json")
        print()
        return 0

    except Exception as e:
        print(f"  \u2717 Browser auth failed: {e}")
        print()
        return 1


def _setup_api_credential(conn: Connection, registry: ConnectionRegistry) -> int:
    """Set up an API credential — check, prompt, save, verify."""
    current = get_credential(conn.name, registry=registry)
    if current:
        masked = current[:4] + "..." + current[-4:] if len(current) > 10 else "****"
        print(f"  \u2713 Credential: {masked}")

        # Offer to health-check
        if conn.health_check:
            health = check_health(conn.name, registry=registry)
            latency = f" ({health.latency_ms:.0f}ms)" if health.latency_ms else ""
            if health.status == HealthStatus.HEALTHY:
                print(f"  \u2713 Verified{latency}")
            else:
                print(f"  \u26a0 Health check: {health.message}")
        print()
        return 0

    # Multiple auth methods? Let user choose
    if conn.auth_methods and len(conn.auth_methods) > 1:
        return _setup_with_auth_method_choice(conn, registry)

    # No credential — prompt for one
    if conn.docs_url:
        print(f"  Get your key: {conn.docs_url}\n")

    try:
        value = input(f"  Paste {conn.display_name} key (Enter to skip): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Skipped")
        return 0

    if not value:
        if conn.credential_env_var:
            print(f"  Or set: export {conn.credential_env_var}=<key>")
        print()
        return 0

    # Save it
    store_credential(conn.name, value)
    print("  \u2713 Saved")

    # Also set in current process for immediate use
    if conn.credential_env_var:
        import os

        os.environ[conn.credential_env_var] = value

    # Verify if possible
    if conn.health_check:
        from axiom.infra.connections import reset_registry

        reset_registry()  # re-discover so new cred is picked up
        health = check_health(conn.name, registry=get_registry())
        if health.status == HealthStatus.HEALTHY:
            print("  \u2713 Verified")
        else:
            print(f"  \u26a0 Could not verify: {health.message}")
            print("    (credential saved — it may still work)")
    print()
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi connect",
        description="Manage external connections and credentials.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  axi connect                    List all connections
  axi connect github             Set up GitHub connection
  axi connect --check            Health check all connections
  axi connect teams --clear      Remove Teams credentials
  axi connect --json             Machine-readable output
""",
    )

    name_arg = parser.add_argument(
        "name",
        nargs="?",
        help="Connection to set up or manage",
    )

    # argcomplete: offer registered connection names as completions
    try:

        def _complete_connection_names(prefix, _parsed_args, **kwargs):
            try:
                return [c.name for c in get_registry().all() if c.name.startswith(prefix)]
            except Exception:
                return []

        name_arg.completer = _complete_connection_names  # type: ignore[attr-defined]
    except Exception:
        pass
    parser.add_argument(
        "--check",
        action="store_true",
        help="Health check all connections",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Remove saved credentials for a connection",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    registry = get_registry()

    # axi connect --check
    if args.check:
        return _check_all(registry, as_json=args.json)

    # axi connect <name> --clear
    if args.name and args.clear:
        clear_credential(args.name)
        print(f"\n  Cleared credentials for {args.name}\n")
        return 0

    # axi connect <name> (setup)
    if args.name:
        return setup_connection(args.name, registry)

    # axi connect (list all)
    connections = registry.all()

    if args.json:
        data = [_connection_status_info(c, registry) for c in connections]
        print(json.dumps(data, indent=2))
        return 0

    if not connections:
        print("\n  No connections registered.")
        print("  Extensions declare connections in axiom-extension.toml\n")
        return 0

    print("\n  Connections")
    print("  " + "\u2550" * 40)
    print(format_status_section(registry=registry))
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
