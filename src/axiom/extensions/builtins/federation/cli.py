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


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{_brand_cli()} federation",
        description="Manage federation membership and resources",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Examples:
  {_brand_cli()} federation status       # Show identity and federation state
  {_brand_cli()} federation init         # Generate Ed25519 keypair
  {_brand_cli()} federation invite       # Create invitation token
  {_brand_cli()} federation peers        # List connected peers
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
    peers_p = _add_json(sub.add_parser("peers", help="List federated peers"))
    (peers_p or sub.choices["peers"]).add_argument(
        "--probe",
        action="store_true",
        help="Actually contact each peer and record the result (live reachability).",
    )

    # --- inference (ADR-030 Phase 1: read-only capability catalog) ------
    inf_p = sub.add_parser(
        "inference",
        help="Federated inference capability catalog (ADR-030 Phase 1)",
    )
    inf_sub = inf_p.add_subparsers(dest="inf_action")

    ls_p = _add_json(inf_sub.add_parser("ls", help="List peer-advertised inference providers"))
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
    from axiom.vega.federation.identity import fingerprint as _fingerprint

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
            print(f"  Run `{_brand_cli()} federation init` to generate node identity.")
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
        "fingerprint": _fingerprint(identity.public_key),
        "peers": len(peers),
        "peers_ever_reached": sum(1 for p in peers if p.last_seen),
    }

    if getattr(args, "json", False):
        print(json.dumps(data, indent=2))
    else:
        from axiom.infra.cli_format import SQUARE, Column, table, terminal_width

        reached = [p for p in peers if p.last_seen]
        newest = max((p.last_seen for p in reached), default="")

        # Lead with the answer. "Is federation working?" is what this command
        # is run to ask, and it used to be the last row of a field dump — the
        # reader had to assemble it from a peer count and a timestamp.
        #
        # A peer count is membership, not connectivity. Saying only "1"
        # invites reading it as "1 connected", which is the reading that made
        # a peer nobody had contacted since April look healthy.
        verdict, summary = _federation_verdict(peers)
        print(f"\n  Federation  {verdict}  {summary}")

        peers_value = f"{len(peers)} known · {len(reached)} reached"
        if reached:
            peers_value += f" · newest contact {_ago(newest)}"

        # The fingerprint, not a truncated public key. Thirty-two base64
        # characters and an ellipsis is the one form of the key you can do
        # nothing with — you cannot compare it, copy it, or verify against
        # it. The fingerprint is grouped in fours precisely so it can be read
        # aloud to a peer over a side channel, which is the step that closes
        # TOFU. Its first four groups are the node id.
        rows = [
            ("this node", f"{identity.display_name}   ({identity.profile} profile)"),
            ("node id", identity.node_id),
            ("owner", identity.owner),
            ("fingerprint", _fingerprint(identity.public_key)),
            ("peers", peers_value),
        ]

        print()
        for line in table(
            rows,
            [Column("federation"), Column("value", wrap=True)],
            width=terminal_width(reserve=2),
            headers=True,
            border=SQUARE,
        ):
            print(line)

        print()
        if peers and not reached:
            print(f"  {_brand_cli()} federation peers --probe    check reachability now")
        elif peers:
            print(f"  {_brand_cli()} federation peers --probe    per-peer reachability")
        else:
            print(f"  {_brand_cli()} nodes add <name> <ssh_target>    register a peer")

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
        print(f"  {_brand_cli()} federation join <url> --confirm")
        return 1

    identity = _get_identity()
    if identity is None:
        print(f"No identity found. Run `{_brand_cli()} federation init` first.")
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
        print(f"  {_brand_cli()} federation leave --confirm")
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
        print(f"No identity found. Run `{_brand_cli()} federation init` first.")
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


def _probe_peers(registry, peers) -> dict:
    """Probe each peer for liveness and persist what came back.

    Connects the heartbeat that already existed. Nothing started it — the only
    `start()` calls are in a module docstring's usage example — so a probe has
    never run and no contact had ever been recorded. Running it on demand from
    the CLI keeps that honest without introducing a background service, which
    is a separate decision.
    """
    from axiom.vega.federation.heartbeat import check_peer

    results = {}
    for peer in peers:
        result = check_peer(
            node_id=peer.node_id,
            transport=peer.transport,
            url=peer.url,
            ssh_user=peer.ssh_user,
            ssh_host=peer.ssh_host,
        )
        results[peer.node_id] = result
        if result.get("healthy"):
            registry.record_contact(peer.node_id, at=result.get("checked_at"))
    return results


def _contact_column(peer, probe: dict | None) -> str:
    """What to show for contact: a probe result, a record, or an honest gap."""
    if probe is not None:
        if probe.get("healthy"):
            return f"up {probe.get('latency_ms', 0)}ms"
        return "unreachable"
    # Same relative form as `since`. A raw timestamp here was wide enough to
    # squeeze the name column into an ellipsis, and still had to be elided.
    return _ago(peer.last_seen) if peer.last_seen else "never probed"


def _ago(timestamp: str) -> str:
    """Delegates to the shared formatter — mcp status wants the same thing,
    and two implementations of "how long ago" drift apart."""
    from axiom.infra.cli_format import relative_age

    return relative_age(timestamp)


def _federation_verdict(peers) -> tuple[str, str]:
    """The one-line answer, and the glyph that goes with it.

    "Is federation working?" is what this command is run to ask, and it used
    to be the last row of a field dump — the reader had to assemble it from a
    peer count and a timestamp.

    A peer count is membership, not connectivity. Reporting only "1" invites
    reading it as "1 connected", which is the reading that made a peer nobody
    had contacted since April look healthy. So a peer never reached is ⬜, not
    a tick: nothing here has been verified.
    """
    from axiom.infra.cli_format import Glyph

    reached = [p for p in peers if p.last_seen]
    plural = "s" if len(peers) != 1 else ""
    if not peers:
        return Glyph.ABSENT, "no peers registered"
    if not reached:
        return Glyph.ABSENT, f"{len(peers)} peer{plural}, never reached"
    newest = max(p.last_seen for p in reached)
    return (
        Glyph.OK,
        f"{len(reached)} of {len(peers)} peer{plural} reached, newest {_ago(newest)}",
    )


def _peer_glyph(peer, probe: dict | None) -> str:
    """The peer's state at a glance, in the platform's one vocabulary.

    A probe answers the question directly, so it wins. Without one, all we
    know is what we last decided about the peer — which is a different fact
    from having reached it, and is drawn as such rather than as a tick.
    """
    from axiom.infra.cli_format import Glyph

    if probe is not None:
        return Glyph.OK if probe.get("healthy") else Glyph.FAIL
    if not peer.has_verified_identity:
        return Glyph.WARN
    return Glyph.OK if peer.last_seen else Glyph.ABSENT


def _cmd_peers(args) -> int:
    registry = _get_registry()
    peers = registry.list_all()
    probes = _probe_peers(registry, peers) if getattr(args, "probe", False) else {}

    if getattr(args, "json", False):
        data = [
            {
                "node_id": p.node_id,
                "display_name": p.display_name,
                "transport": p.transport,
                "state": p.state.value,
                "last_seen": p.last_seen,
                "state_changed_at": p.state_changed_at,
                "probe": probes.get(p.node_id),
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
        print(f"  Use `{_brand_cli()} nodes add` to register nodes, then `{_brand_cli()} federation join`.")
        return 0

    # Was hand-padded with `ljust` under a hardcoded 100-character rule, so a
    # long display name pushed every later column out of line and the rule
    # never matched the content it underlined.
    from axiom.infra.cli_format import SQUARE, Column, table, terminal_width

    rows = []
    for p in peers:
        probe = probes.get(p.node_id)
        rows.append(
            (
                _peer_glyph(p, probe),
                # Short id, git-style. The full value stays in `--json`,
                # which is what anything scripted reads.
                p.node_id[:8],
                p.display_name,
                p.state.value,
                "verified" if p.has_verified_identity else "pending",
                _ago(p.state_changed_at),
                _contact_column(p, probe),
            )
        )

    for line in table(
        rows,
        [
            Column(""),
            Column("node id"),
            Column("name", wrap=True),
            Column("state"),
            Column("identity"),
            Column("since"),
            Column("contact", wrap=True),
        ],
        width=terminal_width(reserve=2),
        headers=True,
        border=SQUARE,
    ):
        print(line)

    unverified = [p for p in peers if not p.has_verified_identity]
    print(f"\n{len(peers)} peers ({len(unverified)} without verified identity)")
    if not probes:
        never = [p for p in peers if not p.last_seen]
        if never:
            print(
                f"  {len(never)} peer(s) never probed — 'State Since' is when the "
                "state last changed, not when the peer was last reached."
            )
        print(f"  Run `{_brand_cli()} federation peers --probe` for live reachability.")
    if unverified:
        print(f"  Re-run `{_brand_cli()} nodes add <name> <ssh_target>` to bind identity.")
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
            f"usage: {_brand_cli()} federation inference ls [--node ...] [--tier ...] "
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

    print(f"{'Node':<14} {'Provider':<18} {'Model':<24} {'Tier':<18} {'Tags':<24} Signed")
    print("-" * 110)
    for a in ads:
        tags = ",".join(a.routing_tags) or "-"
        signed = "yes" if a.signature else "no"
        print(
            f"{a.node_id:<14} {a.provider_name:<18} {a.model:<24} "
            f"{a.routing_tier:<18} {tags:<24} {signed}"
        )

    print(f"\n{len(ads)} advertisement(s). Routing activates in ADR-030 Phase 2.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
