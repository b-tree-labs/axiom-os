# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for ``axi nodes`` — fleet view, node monitoring and management.

Usage:
    axi nodes add <name> <user@host>       Register a node via SSH
    axi nodes add <name> --url <url>       Register via A2A agent card URL
    axi nodes add local                    Register this machine
    axi nodes status                       Check all registered nodes
    axi nodes status <name>                Check a specific node
    axi nodes upgrade <name>               Run remote upgrade on a node
    axi nodes remove <name>                Unregister a node
    axi nodes list                         List all registered nodes
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi nodes",
        description="Fleet view — monitor and manage Axiom nodes",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  axi nodes add <node-name> <user>@<host>
  axi nodes add local
  axi nodes status
  axi nodes list --json
""",
    )

    sub = parser.add_subparsers(dest="action")

    def _add_json(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--json", action="store_true", help="Output as JSON")
        return p

    add_p = _add_json(sub.add_parser("add", help="Register a node"))
    add_p.add_argument("name", help="Node name (or 'local' for this machine)")
    add_p.add_argument("ssh_target", nargs="?", help="SSH target (user@host)")
    add_p.add_argument("-u", "--url", help="A2A agent card URL (instead of SSH)")
    add_p.add_argument(
        "--confirm-key-change",
        action="store_true",
        help="Accept a peer pubkey change (key rotation). Only use after "
        "confirming with the peer operator out-of-band.",
    )

    status_p = _add_json(sub.add_parser("status", help="Check node health"))
    status_p.add_argument("name", nargs="?", help="Specific node name (default: all)")

    upgrade_p = _add_json(sub.add_parser("upgrade", help="Run remote upgrade"))
    upgrade_p.add_argument("name", help="Node to upgrade")

    remove_p = _add_json(sub.add_parser("remove", help="Unregister a node"))
    remove_p.add_argument("name", help="Node name to remove")
    remove_p.add_argument(
        "--confirm",
        action="store_true",
        help="Required — confirm you want to remove this node",
    )

    _add_json(sub.add_parser("list", help="List all registered nodes"))

    parser.add_argument("--json", action="store_true", help="Output as JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.action:
        args.action = "list"

    handlers = {
        "add": _cmd_add,
        "status": _cmd_status,
        "upgrade": _cmd_upgrade,
        "remove": _cmd_remove,
        "list": _cmd_list,
    }

    handler = handlers.get(args.action)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_registry():
    from axiom.vega.federation.discovery import NodeRegistry

    return NodeRegistry()


def _ssh_health_check(ssh_target: str) -> dict:
    """Run remote health check over SSH, return parsed JSON or error dict.

    The remote command runs inside ``bash -lc`` (login shell) so the peer's
    ~/.profile / ~/.bash_profile is sourced. That's the only way to pick up
    PATH entries like ``~/.local/bin`` (where ``axi install-shim`` writes the
    federation entry-point) on Debian/Ubuntu, whose default ~/.bashrc returns
    early for non-interactive sessions.
    """
    remote = (
        "bash -lc 'neut tidy health --json 2>/dev/null || "
        "axi hygiene stat health --json 2>/dev/null'"
    )
    cmd = [
        "ssh",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "BatchMode=yes",
        ssh_target,
        remote,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
        err = result.stderr.strip() or "no output (axi/neut not on PATH for non-interactive SSH? try `axi install-shim` on the peer)"
        return {
            "status": "unreachable",
            "error": err,
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "error": "SSH connection timed out"}
    except json.JSONDecodeError:
        return {"status": "error", "error": "Invalid JSON from remote"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_add(args) -> int:
    from axiom.vega.federation.discovery import KnownNode, NodeState, _now_iso

    registry = _get_registry()
    name = args.name

    # Special case: local node
    if name == "local":
        from axiom.vega.federation.identity import load_identity

        identity = load_identity()
        import hashlib

        host = socket.gethostname()
        node_id = hashlib.sha256(f"local:{host}".encode()).hexdigest()[:16]

        node = KnownNode(
            node_id=node_id,
            display_name=identity.display_name if identity else host,
            url=f"local://{host}",
            transport="local",
            state=NodeState.VERIFIED,
            last_seen=_now_iso(),
        )
        registry.add(node)
        registry.save()

        data = {"added": name, "node_id": node_id, "kind": "local"}
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            print(f"Registered local node (node_id={node_id}).")
        return 0

    # A2A URL-based
    if getattr(args, "url", None):
        node = registry.discover_a2a(name, args.url)
        registry.save()

        data = {"added": name, "node_id": node.node_id, "kind": "a2a", "url": args.url}
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            print(f"Registered node '{name}' via A2A ({args.url}).")
        return 0

    # SSH-based
    ssh_target = getattr(args, "ssh_target", None)
    if not ssh_target:
        print("Error: provide user@host or --url for non-local nodes.")
        return 1

    if "@" not in ssh_target:
        print(f"Error: SSH target must be user@host, got '{ssh_target}'.")
        return 1

    user, host = ssh_target.split("@", 1)
    node = registry.discover_ssh(name, user, host)

    # Fetch the peer's real identity over SSH and bind it. Refuse if the
    # peer has rotated its key silently (possible MITM); --confirm-key-change
    # overrides for legitimate rotations.
    on_key_change = "accept" if getattr(args, "confirm_key_change", False) else "refuse"
    ok, msg = registry.fetch_identity_ssh(
        node.node_id,
        on_key_change=on_key_change,
    )
    registry.save()

    # After fetch the node_id may have changed to the peer's real one.
    bound = None
    if ok:
        for n in registry.list_all():
            if n.ssh_user == user and n.ssh_host == host and n.has_verified_identity:
                bound = n
                break

    data: dict = {
        "added": name,
        "kind": "ssh",
        "target": ssh_target,
        "identity_bound": ok,
        "message": msg,
    }
    if bound:
        data.update(
            node_id=bound.node_id,
            public_key=bound.public_key,
            owner=bound.owner,
            fingerprint=bound.fingerprint,
        )
    else:
        data["node_id"] = node.node_id  # placeholder

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
        return 0 if ok else 1

    if ok and bound:
        print(f"Registered node '{name}' via SSH ({ssh_target}).")
        print(f"  node_id:     {bound.node_id}")
        print(f"  owner:       {bound.owner or '(unknown)'}")
        print(f"  fingerprint: {bound.fingerprint}")
        print()
        print(
            "  → Verify this fingerprint with the peer operator through a "
            "side channel\n    (chat, phone, in-person) before trusting "
            "directives from this node."
        )
        return 0

    # Identity fetch failed — the node is in DISCOVERED state. Transport works
    # but we don't know who they cryptographically are yet.
    print(f"Registered node '{name}' via SSH ({ssh_target}).")
    print(f"  node_id (placeholder): {node.node_id}")
    print()
    print(f"  ⚠ Identity NOT yet bound: {msg}")
    print(
        "  Re-run `axi nodes add` after resolving, or the peer will be "
        "usable for\n  transport only (no signature verification possible)."
    )
    return 1


def _cmd_status(args) -> int:
    registry = _get_registry()
    nodes = registry.list_all()

    if not nodes:
        data = {"nodes": [], "message": "No nodes registered. Use `axi nodes add` to register."}
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            print("No nodes registered.")
            print("  Use `axi nodes add <name> <user@host>` to register a node.")
            print("  Use `axi nodes add local` to register this machine.")
        return 0

    target_name = getattr(args, "name", None)

    if target_name:
        # Find by display_name
        matches = [n for n in nodes if n.display_name == target_name]
        if not matches:
            print(f"Node '{target_name}' not found.")
            return 1
        nodes = matches

    results = []
    for node in nodes:
        if node.transport == "ssh" and node.ssh_host:
            target = f"{node.ssh_user}@{node.ssh_host}" if node.ssh_user else node.ssh_host
            health = _ssh_health_check(target)
        else:
            health = {"status": node.state.value, "transport": node.transport}

        results.append(
            {
                "node_id": node.node_id,
                "display_name": node.display_name,
                "transport": node.transport,
                "health": health,
            }
        )

    if getattr(args, "json", False):
        print(json.dumps(results, indent=2))
        return 0

    print(f"{'Name':<20} {'Transport':<10} {'Status'}")
    print("-" * 50)
    for r in results:
        status = r["health"].get("status", r["health"].get("healthy", "unknown"))
        if status is True:
            status = "healthy"
        elif status is False:
            status = "unhealthy"
        print(f"{r['display_name']:<20} {r['transport']:<10} {status}")

    return 0


def _cmd_upgrade(args) -> int:
    registry = _get_registry()
    nodes = registry.list_all()
    name = args.name

    matches = [n for n in nodes if n.display_name == name]
    if not matches:
        print(f"Node '{name}' not found.")
        return 1

    node = matches[0]
    if node.transport != "ssh":
        print(f"Upgrade only supported for SSH nodes ('{name}' is {node.transport}).")
        return 1

    target = f"{node.ssh_user}@{node.ssh_host}"
    data = {
        "action": "upgrade",
        "node": name,
        "target": target,
        "message": "Remote upgrade not yet implemented.",
    }

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print(f"Upgrade for '{name}' ({target})")
        print("  Remote upgrade not yet implemented.")

    return 0


def _cmd_remove(args) -> int:
    if not getattr(args, "confirm", False):
        print("Removing a node unregisters it from the fleet.")
        print("\nRun with --confirm to proceed:")
        print(f"  axi nodes remove {getattr(args, 'name', '<name>')} --confirm")
        return 1

    registry = _get_registry()
    nodes = registry.list_all()
    name = args.name

    matches = [n for n in nodes if n.display_name == name]
    if not matches:
        print(f"Node '{name}' not found.")
        return 1

    for node in matches:
        registry.remove(node.node_id)
    registry.save()

    data = {"removed": name, "count": len(matches)}
    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print(f"Removed node '{name}'.")

    return 0


def _cmd_list(args) -> int:
    registry = _get_registry()
    nodes = registry.list_all()

    if getattr(args, "json", False):
        print(json.dumps([n.to_dict() for n in nodes], indent=2))
        return 0

    if not nodes:
        print("No nodes registered.")
        return 0

    print(f"{'Name':<20} {'Transport':<10} {'State':<12} {'URL/Host'}")
    print("-" * 70)
    for n in nodes:
        host = n.url
        if n.ssh_host:
            host = f"{n.ssh_user}@{n.ssh_host}" if n.ssh_user else n.ssh_host
        print(f"{n.display_name:<20} {n.transport:<10} {n.state.value:<12} {host}")

    print(f"\n{len(nodes)} nodes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
