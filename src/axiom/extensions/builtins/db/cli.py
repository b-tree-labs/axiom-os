# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi db` — PostgreSQL + pgvector infrastructure.

This provides shared database infrastructure for all platform components.

Subcommands:
    axi db up           Start local PostgreSQL (K3D → Docker Compose → native)
    axi db down         Stop local cluster (preserves data)
    axi db delete       Delete cluster and all data
    axi db status       Show cluster and connection status
    axi db migrate      Run Alembic schema migrations
    axi db bootstrap    Full setup from scratch
"""

from __future__ import annotations

import argparse
import os
import re
import sys


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

# Default connection for local K3D cluster
DEFAULT_LOCAL_URL = "postgresql://axiom:axiom@localhost:5432/axiom_db"


def _mask_url(url: str) -> str:
    """Mask password in connection URL for display."""
    return re.sub(r":[^:@]+@", ":****@", url)


def _finish_up(success: bool) -> int:
    """Shared next-steps / failure trailer for `db up`."""
    if success:
        print("\nNext steps:")
        print(f"  {_brand_cli()} db migrate upgrade   # Apply schema migrations")
        print(f"  {_brand_cli()} db status            # Verify connection")
        return 0
    print("\nFailed to start. Check prerequisites above.")
    return 1


def cmd_up(args: argparse.Namespace) -> int:
    """Start local PostgreSQL, falling through K3D → Docker Compose → native.

    Sandbox audit 2026-09-18, gap 3: this verb was K3D-only and suggested
    `brew` inside Linux containers while the shipped compose file
    (`axiom/setup/docker-compose.yml`) and `provision_postgres_compose()`
    sat unwired. An explicitly configured backend (`AXIOM_DB_BACKEND`) is
    honored as-is via its DeploymentProvider; otherwise the best available
    path is auto-detected so a machine without K3D still gets a database.
    """
    explicit = os.environ.get("AXIOM_DB_BACKEND")
    if explicit:
        from axiom.extensions.builtins.db.providers import load_deployment_provider

        provider = load_deployment_provider(explicit)
        print(f"🚀 Starting local PostgreSQL + pgvector ({provider.name})...\n")
        return _finish_up(provider.up())

    from axiom.setup.infra import detect_infra_path

    path = detect_infra_path()

    if path == "k3d":
        from axiom.extensions.builtins.signals.pgvector_store import k3d_up

        print("🚀 Starting local PostgreSQL + pgvector (K3D)...\n")
        return _finish_up(k3d_up())

    if path == "docker-compose":
        from axiom.setup.infra import provision_postgres_compose

        print("🚀 Starting local PostgreSQL + pgvector (Docker Compose)...\n")
        success = provision_postgres_compose()
        if success:
            print("  ✓ PostgreSQL service started")
        else:
            print("  ✗ docker compose could not start the database service.")
        return _finish_up(success)

    # No usable container runtime detected. Distinguish "Docker installed
    # but stopped" (fix: start it) from "no Docker at all" (fix: native PG).
    from axiom.setup.infra import InfraStatus, check_docker, provision_postgres_native

    print("🚀 Starting local PostgreSQL...\n")
    docker = check_docker()
    if docker.status == InfraStatus.NEEDS_START:
        print(f"  ✗ {docker.message}")
        print(f"  Start Docker, then re-run: {_brand_cli()} db up")
        return 1

    pg = provision_postgres_native()
    if pg.get("running"):
        print("  ✓ PostgreSQL already running on localhost:5432")
        return _finish_up(True)

    print("  ✗ No container runtime found and PostgreSQL is not running.")
    instructions = pg.get("instructions", [])
    if instructions:
        print("  To set up PostgreSQL on this machine:")
        for line in instructions:
            print(f"    {line}")
    return 1


def cmd_down(args: argparse.Namespace) -> int:
    """Stop local K3D cluster (preserves data)."""
    from axiom.extensions.builtins.signals.pgvector_store import k3d_down

    print("⏸️  Stopping local cluster...\n")
    success = k3d_down()
    return 0 if success else 1


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete local K3D cluster and all data."""
    from axiom.extensions.builtins.signals.pgvector_store import k3d_delete

    if not args.confirm:
        print("⚠️  This will DELETE the local cluster and ALL data!")
        print("\nTo confirm, run:")
        print(f"  {_brand_cli()} db delete --confirm")
        return 1

    print("🗑️  Deleting local cluster...\n")
    success = k3d_delete()
    return 0 if success else 1


def cmd_status(args: argparse.Namespace) -> int:
    """Show backend and database status."""
    from axiom.extensions.builtins.db.providers import load_deployment_provider
    from axiom.extensions.builtins.signals.pgvector_store import VectorDB

    provider = load_deployment_provider()
    status = provider.status()
    db_url = os.environ.get("AXIOM_DB_URL", status.connection_url or DEFAULT_LOCAL_URL)
    masked_url = _mask_url(db_url) if db_url else "(not configured)"

    print("\n🗄️  Database Status\n")

    # Backend-specific status (delegates to provider)
    print(f"--- Backend: {provider.name} ---")
    if not status.available:
        reason = status.extra.get("reason") or _backend_install_hint(provider.name)
        print(f"  Available: ✗ {reason}")
    elif status.running:
        print("  Available: ✓ tooling installed")
        print("  Running:   ✓")
    else:
        print("  Available: ✓ tooling installed")
        print("  Running:   ○ Stopped")
        print(f"  Start:     {_brand_cli()} db up")

    # Print backend-specific extras for context.
    for key, value in status.extra.items():
        if key == "reason":
            continue
        print(f"  {key + ':':<11} {value}")

    print("\n--- Connection ---")
    print(f"  URL: {masked_url}")

    if os.environ.get("AXIOM_DB_URL"):
        print("  Source: AXIOM_DB_URL environment variable")
    else:
        print(f"  Source: [db.deployment] backend = \"{provider.name}\"")

    # Test connection if backend reports running or an explicit URL is set
    if status.running or os.environ.get("AXIOM_DB_URL"):
        print("\n--- Health Check ---")
        try:
            db = VectorDB()
            db.connect()
            health = db.health_check()

            if health.get("connected"):
                print(f"  Status:     ✓ {health.get('status', 'connected')}")
                pg_version = health.get("postgresql", "N/A")
                if len(pg_version) > 60:
                    pg_version = pg_version[:60] + "..."
                print(f"  PostgreSQL: {pg_version}")
                print(f"  pgvector:   {health.get('pgvector', 'N/A')}")
            else:
                print(f"  Status:     ✗ {health.get('error', 'Cannot connect')}")

            db.close()
        except Exception as e:  # noqa: BLE001 — surfaced to operator
            print(f"  Status:     ✗ Error: {e}")
            return 1

    print("\n--- Backends available ---")
    from axiom.extensions.builtins.db.providers import DB_PROVIDERS

    for name in sorted(DB_PROVIDERS):
        marker = "*" if name == provider.name else " "
        print(f"  {marker} {name}")
    print("\n  Switch with AXIOM_DB_BACKEND=<name> or edit [db.deployment] backend.")

    return 0


def _backend_install_hint(backend: str) -> str:
    """Friendly install instructions per backend.

    Platform-aware: never suggest `brew` on a machine that has no brew
    (e.g. a Linux container — sandbox audit 2026-09-18, gap 3).
    """
    if backend == "k3d":
        import shutil

        if shutil.which("brew"):
            return "k3d CLI not found. Install: brew install k3d"
        return (
            "k3d CLI not found. Install: curl -s "
            "https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash"
        )
    return {
        "docker-compose": "Docker not found or not running. Install Docker Desktop.",
        "hosted": "No connection_string configured. Set AXIOM_DB_URL or edit [db.deployment.hosted] connection_string.",
    }.get(backend, "Backend tooling not available.")


def _other_extension_status() -> list[str]:
    """One status line per extension OTHER than the one `check_migrations`
    reports on.

    This used to glob ``extensions/builtins/*/alembic.ini`` — one directory
    above where that file lives, and keyed on a file only signals ships. It
    returned an empty list under every condition, so the screen said nothing
    about four extensions that ship migrations. Discovery now lives in
    `axiom.infra.db` and keys on ``migrations/env.py``, which all of them have.
    """
    from axiom.infra.db import current_revision, extensions_with_migrations

    lines: list[str] = []
    for name, directory in extensions_with_migrations():
        if name == "signals":
            continue  # reported above by check_migrations()
        try:
            from alembic.script import ScriptDirectory

            head = ScriptDirectory(str(directory)).get_current_head() or "(none)"
        except Exception as exc:  # noqa: BLE001
            lines.append(f"   {name}:  unreadable — {type(exc).__name__}")
            continue
        current = current_revision(name)
        mark = "✅" if current == head else "⚠️ "
        lines.append(f"   {mark} {name}:  {current or '(none)'} → {head}")
    return lines


def cmd_migrate(args: argparse.Namespace) -> int:
    """Run Alembic database schema migrations."""
    from axiom.extensions.builtins.signals.migrations import (
        check_migrations,
        ensure_pgvector_extension,
        run_migrations,
        verify_schema,
    )

    cmd = getattr(args, 'migrate_command', 'check') or 'check'
    revision = getattr(args, 'revision', 'head') or 'head'
    message = getattr(args, 'message', '')
    autogenerate = getattr(args, 'autogenerate', False)

    if cmd == "check":
        print("\n🔍 Migration Status\n")

        status = check_migrations()

        if not status.get("connected"):
            print("❌ Cannot connect to database")
            print(f"   Is the database running? Try: {_brand_cli()} db up")
            return 1

        print(f"Current revision: {status.get('current') or '(none)'}")
        print(f"Head revision:    {status.get('head') or '(none)'}")
        print(f"Pending:          {status.get('pending', 0)} migration(s)")

        if status.get("up_to_date"):
            # Scoped, deliberately. This verb runs one extension's revisions,
            # and saying "the database" is up to date while another
            # extension's tables do not exist is how `neut experiment list`
            # came to answer with a SQL error on a machine this screen called
            # healthy.
            print("\n✅ Up to date (signals migrations)")
        else:
            print(f"\n⚠️  {status['pending']} pending migration(s):")
            for rev in status.get("pending_revisions", []):
                print(f"   - {rev}")
            print(f"\nRun: {_brand_cli()} db migrate upgrade head")

        others = _other_extension_status()
        if others:
            print("\nOther extensions (each has its own revision history):")
            for line in others:
                print(line)

        others = _other_extension_status()
        if others:
            print("\nOther extensions (each has its own revision history):")
            for line in others:
                print(line)

        # Also verify schema
        schema = verify_schema()
        if schema.get("valid"):
            print("\n✅ Schema verified")
        else:
            if schema.get("missing_tables"):
                print(f"\n⚠️  Missing tables: {', '.join(schema['missing_tables'])}")
            if not schema.get("has_pgvector"):
                print("⚠️  pgvector extension not installed")

        return 0

    elif cmd == "upgrade":
        print(f"\n🚀 Upgrading database to revision: {revision}\n")

        # signals needs pgvector, which lives in the `signal` extra. An install
        # that omits that extra has no signals code to migrate, and treating
        # that as a failure means a deploy can never complete on a node which
        # deliberately does not ship every extension. Skipped, not failed —
        # "not installed" and "broken" want different reactions.
        ok = True
        try:
            ensure_pgvector_extension()
            ok = run_migrations("upgrade", revision)
            if ok:
                status = check_migrations()
                print(f"   signals:  {status.get('current')}")
            else:
                print("   signals:  ❌ upgrade failed")
        except ModuleNotFoundError as exc:
            missing = getattr(exc, "name", None) or str(exc)
            print(
                f"   signals:  skipped — {missing!r} is not installed "
                "(pip install 'axiom-os-lm[signal]' to provision it here)"
            )

        # Every other extension too. `axi db migrate` reporting "up to date"
        # while another extension's tables did not exist is issue #826; a verb
        # named for the whole database has to move the whole database.
        # `head` only — a specific revision means nothing across separate
        # revision histories.
        if revision == "head":
            from axiom.infra.db import extensions_with_migrations, provision_extension

            for name, _ in extensions_with_migrations():
                if name == "signals":
                    continue
                result = provision_extension(name)
                print(f"   {result.summary}")
                ok = ok and result.ok

        if ok:
            print("\n✅ Upgrade complete")
            return 0
        print("\n❌ Some extensions did not reach head")
        return 1

    elif cmd == "downgrade":
        print(f"\n⬇️  Downgrading database to revision: {revision}\n")

        if run_migrations("downgrade", revision):
            print("\n✅ Downgrade complete")

            status = check_migrations()
            print(f"Current revision: {status.get('current')}")
            return 0
        else:
            print("\n❌ Downgrade failed")
            return 1

    elif cmd == "current":
        run_migrations("current")
        return 0

    elif cmd == "history":
        print("\n📜 Migration History\n")
        run_migrations("history")
        return 0

    elif cmd == "revision":
        if not message:
            print("Error: --message/-m is required for 'revision' command")
            print(f"Example: {_brand_cli()} db migrate revision -m 'add user table'")
            return 1

        print(f"\n📝 Creating new migration: {message}\n")

        if run_migrations("revision", message=message, autogenerate=autogenerate):
            print("\n✅ Migration created")
            if autogenerate:
                print("   Review the generated migration before applying.")
            return 0
        else:
            print("\n❌ Failed to create migration")
            return 1

    else:
        _print_migrate_help()
        return 0


def _print_migrate_help():
    """Print migration subcommand help."""
    print(f"Usage: {_brand_cli()} db migrate <command> [revision]")
    print()
    print("Commands:")
    print("  check       Check migration status (default)")
    print("  upgrade     Apply pending migrations (default: head)")
    print("  downgrade   Revert migrations (specify revision)")
    print("  current     Show current database revision")
    print("  history     Show migration history")
    print("  revision    Create new migration (-m message required)")
    print()
    print("Examples:")
    print(f"  {_brand_cli()} db migrate check")
    print(f"  {_brand_cli()} db migrate upgrade head")
    print(f"  {_brand_cli()} db migrate downgrade -1")
    print(f"  {_brand_cli()} db migrate revision -m 'add user preferences' --autogenerate")


def cmd_bootstrap(args: argparse.Namespace) -> int:
    """Full database setup from scratch."""
    from axiom.extensions.builtins.signals.bootstrap import (
        Bootstrap,
        BootstrapConfig,
        BootstrapStep,
    )

    config = BootstrapConfig(
        non_interactive=args.non_interactive,
        verbose=args.verbose,
    )

    bootstrap = Bootstrap(config)

    if args.check:
        results = bootstrap.check_only()
    elif args.step:
        # Parse step name to enum
        try:
            step = BootstrapStep[args.step.upper()]
            results = bootstrap.run(steps=[step])
        except KeyError:
            print(f"Unknown step: {args.step}")
            print(f"Valid steps: {', '.join(s.name.lower() for s in BootstrapStep)}")
            return 1
    else:
        results = bootstrap.run()

    # Print summary
    print("\n" + "=" * 50)
    print("Bootstrap Summary:")
    for result in results:
        print(f"  {result}")

    all_success = all(r.success for r in results)
    return 0 if all_success else 1


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser for db CLI."""
    parser = argparse.ArgumentParser(
        prog=f"{_brand_cli()} db",
        description="PostgreSQL + pgvector infrastructure for the platform",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # up
    subparsers.add_parser(
        "up",
        help="Start local PostgreSQL + pgvector (K3D, Docker Compose, or native)",
    )

    # down
    subparsers.add_parser(
        "down",
        help="Stop local K3D cluster (preserves data)",
    )

    # delete
    delete_parser = subparsers.add_parser(
        "delete",
        help="Delete local K3D cluster and all data",
    )
    delete_parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm deletion (required)",
    )

    # status
    subparsers.add_parser(
        "status",
        help="Show cluster and database status",
    )

    # migrate
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Run Alembic database schema migrations",
    )
    migrate_parser.add_argument(
        "migrate_command",
        nargs="?",
        choices=["upgrade", "downgrade", "current", "history", "revision", "check"],
        default="check",
        help="Migration command (default: check)",
    )
    migrate_parser.add_argument(
        "revision",
        nargs="?",
        default="head",
        help="Target revision (default: head)",
    )
    migrate_parser.add_argument(
        "-m", "--message",
        help="Message for new revision (required for 'revision' command)",
    )
    migrate_parser.add_argument(
        "--autogenerate",
        action="store_true",
        help="Auto-detect model changes for 'revision' command",
    )

    # bootstrap
    bootstrap_parser = subparsers.add_parser(
        "bootstrap",
        help="Full database setup from scratch",
    )
    bootstrap_parser.add_argument(
        "--check",
        action="store_true",
        help="Check prerequisites without making changes",
    )
    bootstrap_parser.add_argument(
        "--step",
        help="Run only a specific step (prerequisites, k3d, postgres, pgvector, migrate, verify)",
    )
    bootstrap_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Don't prompt for confirmation",
    )
    bootstrap_parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show detailed output",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point for axi db CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        # Show help with quick start
        parser.print_help()
        print("\nQuick Start:")
        print(f"  {_brand_cli()} db up              # Start local PostgreSQL")
        print(f"  {_brand_cli()} db migrate upgrade # Apply schema migrations")
        print(f"  {_brand_cli()} db status          # Verify everything works")
        print()
        print("Full setup:")
        print(f"  {_brand_cli()} db bootstrap       # Complete setup from scratch")
        return 0

    commands = {
        "up": cmd_up,
        "down": cmd_down,
        "delete": cmd_delete,
        "status": cmd_status,
        "migrate": cmd_migrate,
        "bootstrap": cmd_bootstrap,
    }

    handler = commands.get(args.command)
    if handler:
        return handler(args)
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
