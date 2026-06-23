# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for ``axi federation`` — federation membership and resources.

Usage:
    axi federation status       Show federation status (identity, peers, resources)
    axi federation init         Initialize node identity (generates Ed25519 keypair)
    axi federation join <url>   Join a federation via invitation URL
    axi federation leave        Leave current federation
    axi federation invite       Generate invitation token for another node
    axi federation resources    List shared resources across federation
    axi federation peers        List federated peers with health
    axi federation inference ls List peer-advertised inference providers (ADR-030 P1)
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axi federation",
        description="Manage federation membership and resources",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  axi federation status       # Show identity and federation state
  axi federation init         # Generate Ed25519 keypair
  axi federation invite       # Create invitation token
  axi federation peers        # List connected peers
""",
    )

    sub = parser.add_subparsers(dest="action")

    def _add_json(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--json", action="store_true", help="Output as JSON")
        return p

    _add_json(sub.add_parser("status", help="Show federation status"))

    init_p = _add_json(sub.add_parser("init", help="Initialize node identity"))
    init_p.add_argument("--owner", help="Owner identifier (e.g., email)")
    init_p.add_argument("--name", help="Display name for this node")
    init_p.add_argument(
        "--profile",
        choices=["leaf", "standard", "provider", "coordinator"],
        default="standard",
        help="Node profile (default: standard)",
    )

    join_p = _add_json(sub.add_parser("join", help="Join a federation"))
    join_p.add_argument("url", help="Invitation URL or token")
    join_p.add_argument(
        "--confirm",
        action="store_true",
        help="Required — confirm you want to share your identity and join",
    )

    leave_p = _add_json(sub.add_parser("leave", help="Leave current federation"))
    leave_p.add_argument(
        "--confirm",
        action="store_true",
        help="Required — confirm you want to leave the federation",
    )

    invite_p = _add_json(sub.add_parser("invite", help="Generate invitation token"))
    invite_p.add_argument(
        "--ttl",
        type=int,
        default=24,
        help="Token time-to-live in hours (default: 24)",
    )

    _add_json(sub.add_parser("resources", help="List shared resources"))
    _add_json(sub.add_parser("peers", help="List federated peers"))

    # --- inference (ADR-030 Phase 1: read-only capability catalog) ------
    inf_p = sub.add_parser(
        "inference",
        help="Federated inference capability catalog (ADR-030 Phase 1)",
    )
    inf_sub = inf_p.add_subparsers(dest="inf_action")

    ls_p = _add_json(
        inf_sub.add_parser("ls", help="List peer-advertised inference providers")
    )
    ls_p.add_argument("--node", help="Filter by serving node ID")
    ls_p.add_argument(
        "--tier",
        help="Filter by routing_tier (e.g. public | export_controlled | any)",
    )
    ls_p.add_argument("--tag", help="Filter by routing_tag")
    ls_p.add_argument(
        "--fresher-than-hours",
        type=float,
        default=None,
        help="Exclude advertisements older than N hours",
    )
    ls_p.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="Path to artifacts.db (default: ~/.axi/artifacts.db)",
    )

    parser.add_argument("--json", action="store_true", help="Output as JSON")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.action:
        args.action = "status"

    handlers = {
        "status": _cmd_status,
        "init": _cmd_init,
        "join": _cmd_join,
        "leave": _cmd_leave,
        "invite": _cmd_invite,
        "resources": _cmd_resources,
        "peers": _cmd_peers,
        "inference": _cmd_inference,
    }

    handler = handlers.get(args.action)
    if handler is None:
        parser.print_help()
        return 1

    return handler(args)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_identity(keys_dir: Path | None = None):
    from axiom.vega.federation.identity import load_identity

    return load_identity(keys_dir)


def _get_registry():
    from axiom.vega.federation.discovery import NodeRegistry

    return NodeRegistry()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _cmd_status(args) -> int:
    identity = _get_identity()

    if identity is None:
        data = {
            "initialized": False,
            "message": "No identity found. Run `axi federation init` to create one.",
        }
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            print("Federation Status")
            print("  Not initialized.")
            print("  Run `axi federation init` to generate node identity.")
        return 0

    registry = _get_registry()
    peers = registry.list_all()

    data = {
        "initialized": True,
        "node_id": identity.node_id,
        "owner": identity.owner,
        "display_name": identity.display_name,
        "profile": identity.profile,
        "public_key": identity.public_key,
        "peers": len(peers),
    }

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print("Federation Status")
        print(f"  Node ID:      {identity.node_id}")
        print(f"  Owner:        {identity.owner}")
        print(f"  Display Name: {identity.display_name}")
        print(f"  Profile:      {identity.profile}")
        print(f"  Public Key:   {identity.public_key[:32]}...")
        print(f"  Peers:        {len(peers)}")

    return 0


def _cmd_init(args) -> int:
    from axiom.vega.federation.identity import generate_identity, load_identity

    existing = load_identity()
    if existing is not None:
        data = {
            "error": "Identity already exists",
            "node_id": existing.node_id,
        }
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            print(f"Identity already exists (node_id={existing.node_id}).")
            print("  Delete ~/.axi/identity/ to reinitialize.")
        return 1

    owner = getattr(args, "owner", None) or _prompt_owner()
    display_name = getattr(args, "name", None) or ""
    profile = getattr(args, "profile", "standard")

    identity = generate_identity(
        owner=owner,
        display_name=display_name,
        profile=profile,
    )

    data = {
        "initialized": True,
        "node_id": identity.node_id,
        "owner": identity.owner,
        "display_name": identity.display_name,
        "profile": identity.profile,
        "keys_dir": str(identity.private_key_path.parent),
    }

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print("Identity created.")
        print(f"  Node ID:      {identity.node_id}")
        print(f"  Owner:        {identity.owner}")
        print(f"  Display Name: {identity.display_name}")
        print(f"  Profile:      {identity.profile}")
        print(f"  Keys:         {identity.private_key_path.parent}")

    return 0


def _prompt_owner() -> str:
    """Prompt for owner when not supplied via --owner."""
    try:
        owner = input("Owner identifier (e.g., email): ").strip()
    except (EOFError, KeyboardInterrupt):
        owner = ""
    if not owner:
        import getpass

        owner = getpass.getuser()
    return owner


def _cmd_join(args) -> int:
    if not getattr(args, "confirm", False):
        print("Joining a federation shares your node identity with remote peers.")
        print("\nRun with --confirm to proceed:")
        print("  axi federation join <url> --confirm")
        return 1

    identity = _get_identity()
    if identity is None:
        print("No identity found. Run `axi federation init` first.")
        return 1

    url = args.url

    data = {
        "action": "join_requested",
        "target": url,
        "node_id": identity.node_id,
        "message": "Join request submitted (handshake not yet implemented).",
    }

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print(f"Join request submitted to {url}")
        print("  Handshake protocol not yet implemented.")
        print(f"  Node ID: {identity.node_id}")

    return 0


def _cmd_leave(args) -> int:
    if not getattr(args, "confirm", False):
        print("Leaving a federation is irreversible. You will need to re-establish")
        print("bilateral trust with all peers.")
        print("\nRun with --confirm to proceed:")
        print("  axi federation leave --confirm")
        return 1

    identity = _get_identity()
    if identity is None:
        print("Not in a federation (no identity).")
        return 1

    data = {
        "action": "leave_requested",
        "node_id": identity.node_id,
        "message": "Leave request processed (no active federation to leave).",
    }

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print("Leave request processed.")
        print("  No active federation membership to revoke.")

    return 0


def _cmd_invite(args) -> int:
    identity = _get_identity()
    if identity is None:
        print("No identity found. Run `axi federation init` first.")
        return 1

    ttl_hours = getattr(args, "ttl", 24)
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(hours=ttl_hours)

    data = {
        "token": token,
        "issued_by": identity.node_id,
        "expires": expires.isoformat(),
        "ttl_hours": ttl_hours,
    }

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print("Invitation Token")
        print(f"  Token:   {token}")
        print(f"  Issued:  {identity.node_id}")
        print(f"  Expires: {expires:%Y-%m-%d %H:%M UTC} ({ttl_hours}h)")

    return 0


def _cmd_resources(args) -> int:
    registry = _get_registry()
    peers = registry.list_all()

    # Placeholder: no shared resources yet
    data = {
        "peers": len(peers),
        "resources": [],
        "message": "Resource sharing not yet implemented.",
    }

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        print("Shared Resources")
        if not peers:
            print("  No peers connected — no shared resources.")
        else:
            print(f"  {len(peers)} peers connected.")
            print("  Resource sharing not yet implemented.")

    return 0


def _cmd_peers(args) -> int:
    registry = _get_registry()
    peers = registry.list_all()

    if getattr(args, "json", False):
        data = [
            {
                "node_id": p.node_id,
                "display_name": p.display_name,
                "transport": p.transport,
                "state": p.state.value,
                "last_seen": p.last_seen,
                "public_key": p.public_key,
                "owner": p.owner,
                "fingerprint": p.fingerprint,
                "identity_verified": p.has_verified_identity,
            }
            for p in peers
        ]
        print(json.dumps(data, indent=2))
        return 0

    if not peers:
        print("No federated peers.")
        print("  Use `axi nodes add` to register nodes, then `axi federation join`.")
        return 0

    print(f"{'Node ID':<18} {'Name':<20} {'State':<12} {'Identity':<10} {'Last Seen'}")
    print("-" * 80)
    for p in peers:
        last = p.last_seen[:19] if p.last_seen else "never"
        ident = "verified" if p.has_verified_identity else "pending"
        print(f"{p.node_id:<18} {p.display_name:<20} {p.state.value:<12} {ident:<10} {last}")

    unverified = [p for p in peers if not p.has_verified_identity]
    print(f"\n{len(peers)} peers ({len(unverified)} without verified identity)")
    if unverified:
        print("  Re-run `axi nodes add <name> <ssh_target>` to bind identity.")
    return 0


def _cmd_inference(args) -> int:
    """ADR-030 Phase 1 — read-only capability catalog.

    Reads ``federated_provider`` advertisements from the local artifact
    registry (populated by local publish + peer gossip) and prints them.
    No routing, no policy enforcement — those arrive in Phase 2.
    """
    action = getattr(args, "inf_action", None)
    if action is None:
        print(
            "usage: axi federation inference ls [--node ...] [--tier ...] "
            "[--tag ...] [--fresher-than-hours N] [--registry PATH] [--json]",
            file=sys.stderr,
        )
        return 1

    if action == "ls":
        return _cmd_inference_ls(args)

    print(f"Unknown inference action: {action}", file=sys.stderr)
    return 1


def _cmd_inference_ls(args) -> int:
    from axiom.artifacts.registry import ArtifactRegistry, SQLiteBackend
    from axiom.infra.paths import get_user_state_dir
    from axiom.vega.federation.inference_catalog import list_advertisements

    registry_path: Path = args.registry or (get_user_state_dir() / "artifacts.db")
    if not registry_path.exists():
        data = {
            "advertisements": [],
            "message": f"No artifact registry at {registry_path}",
        }
        if getattr(args, "json", False):
            print(json.dumps(data, indent=2))
        else:
            print("No federated inference providers advertised.")
            print(f"  (no artifact registry at {registry_path})")
        return 0

    fresher_than = None
    if args.fresher_than_hours is not None:
        cutoff = datetime.now(UTC) - timedelta(hours=args.fresher_than_hours)
        fresher_than = cutoff.isoformat()

    registry = ArtifactRegistry(backend=SQLiteBackend(registry_path))
    ads = list_advertisements(
        registry,
        node_id=args.node,
        tier=args.tier,
        tag=args.tag,
        fresher_than=fresher_than,
    )

    if getattr(args, "json", False):
        print(
            json.dumps(
                [
                    {
                        "node_id": a.node_id,
                        "provider_name": a.provider_name,
                        "provider_uri": a.provider_uri,
                        "model": a.model,
                        "routing_tier": a.routing_tier,
                        "routing_tags": list(a.routing_tags),
                        "requires_vpn": a.requires_vpn,
                        "advertised_at": a.advertised_at,
                        "fragment_id": a.fragment_id,
                        "signed": a.signature is not None,
                    }
                    for a in ads
                ],
                indent=2,
            )
        )
        return 0

    if not ads:
        print("No federated inference providers advertised.")
        return 0

    print(
        f"{'Node':<14} {'Provider':<18} {'Model':<24} {'Tier':<18} {'Tags':<24} Signed"
    )
    print("-" * 110)
    for a in ads:
        tags = ",".join(a.routing_tags) or "-"
        signed = "yes" if a.signature else "no"
        print(
            f"{a.node_id:<14} {a.provider_name:<18} {a.model:<24} "
            f"{a.routing_tier:<18} {tags:<24} {signed}"
        )

    print(
        f"\n{len(ads)} advertisement(s). Routing activates in ADR-030 Phase 2."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
