# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CLI handler for `axi status` — system health dashboard.

Usage:
    axi status              Show all service health
    axi status --db         Check database only
    axi status --api        Check API server only
    axi status --services   Check all background services
    axi status --watch      Continuously monitor (refresh every 5s)
    axi status --json       Output as JSON (for automation)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from axiom.infra.cli_format import section_header, separator


class HealthStatus(Enum):
    """Health status levels."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class ServiceHealth:
    """Health status of a single service."""

    name: str
    status: HealthStatus
    message: str
    latency_ms: float | None = None
    details: dict = field(default_factory=dict)
    checked_at: str = ""

    def __post_init__(self):
        if not self.checked_at:
            self.checked_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "latency_ms": self.latency_ms,
            "details": self.details,
            "checked_at": self.checked_at,
        }

    @property
    def icon(self) -> str:
        icons = {
            HealthStatus.HEALTHY: "✓",
            HealthStatus.DEGRADED: "⚠",
            HealthStatus.UNHEALTHY: "✗",
            HealthStatus.UNKNOWN: "?",
        }
        return icons.get(self.status, "?")

    @property
    def color_code(self) -> str:
        """ANSI color codes."""
        colors = {
            HealthStatus.HEALTHY: "\033[92m",  # Green
            HealthStatus.DEGRADED: "\033[93m",  # Yellow
            HealthStatus.UNHEALTHY: "\033[91m",  # Red
            HealthStatus.UNKNOWN: "\033[90m",  # Gray
        }
        return colors.get(self.status, "")


@dataclass
class SystemHealth:
    """Aggregated system health."""

    services: list[ServiceHealth] = field(default_factory=list)
    overall: HealthStatus = HealthStatus.UNKNOWN
    checked_at: str = ""

    def __post_init__(self):
        if not self.checked_at:
            self.checked_at = datetime.now(UTC).isoformat()

    def compute_overall(self) -> None:
        """Compute overall health from services."""
        if not self.services:
            self.overall = HealthStatus.UNKNOWN
            return

        statuses = [s.status for s in self.services]

        if all(s == HealthStatus.HEALTHY for s in statuses):
            self.overall = HealthStatus.HEALTHY
        elif any(s == HealthStatus.UNHEALTHY for s in statuses):
            self.overall = HealthStatus.UNHEALTHY
        elif any(s == HealthStatus.DEGRADED for s in statuses):
            self.overall = HealthStatus.DEGRADED
        else:
            self.overall = HealthStatus.UNKNOWN

    def to_dict(self) -> dict:
        return {
            "overall": self.overall.value,
            "services": [s.to_dict() for s in self.services],
            "checked_at": self.checked_at,
        }


class HealthChecker:
    """Checks health of platform services."""

    def __init__(self, repo_root: Path | None = None):
        from axiom import REPO_ROOT

        self.repo_root = repo_root or REPO_ROOT

    def check_all(self) -> SystemHealth:
        """Check all services."""
        health = SystemHealth()

        # LLM and routing — most important for new users
        health.services.append(self.check_llm_providers())
        health.services.append(self.check_ollama())
        health.services.append(self.check_routing())

        # Infrastructure
        health.services.append(self.check_database())
        health.services.append(self.check_api_server())
        health.services.append(self.check_sense_server())
        health.services.append(self.check_mcp_server())
        health.services.append(self.check_mo())

        health.compute_overall()
        return health

    def check_llm_providers(self) -> ServiceHealth:
        """Check if any LLM providers are configured and reachable."""
        try:
            from axiom.infra.gateway import Gateway

            gw = Gateway()

            if not gw.providers:
                return ServiceHealth(
                    name="LLM Providers",
                    status=HealthStatus.UNHEALTHY,
                    message="No providers configured",
                    details={
                        "fix": "Set ANTHROPIC_API_KEY or OPENAI_API_KEY, then run `axi config`",
                    },
                )

            available = [p for p in gw.providers if p.api_key]
            unavailable = [p for p in gw.providers if not p.api_key]

            if not available:
                return ServiceHealth(
                    name="LLM Providers",
                    status=HealthStatus.UNHEALTHY,
                    message=f"{len(unavailable)} configured but no API keys set",
                    details={
                        "providers": [p.name for p in unavailable],
                        "fix": "Set the API key env var for at least one provider",
                    },
                )

            names = [p.name for p in available]
            vpn_providers = [p for p in available if getattr(p, "requires_vpn", False)]
            vpn_reachable = []
            vpn_unreachable = []
            for p in vpn_providers:
                if gw._check_vpn(p):
                    vpn_reachable.append(p.name)
                else:
                    vpn_unreachable.append(p.name)

            details: dict = {"providers": names}
            if vpn_reachable:
                details["vpn_reachable"] = vpn_reachable
            if vpn_unreachable:
                details["vpn_unreachable"] = vpn_unreachable

            status = HealthStatus.HEALTHY
            msg = f"{len(available)} provider{'s' if len(available) != 1 else ''} ready"
            if vpn_unreachable:
                status = HealthStatus.DEGRADED
                msg += f" ({', '.join(vpn_unreachable)} unreachable)"

            return ServiceHealth(
                name="LLM Providers",
                status=status,
                message=msg,
                details=details,
            )

        except Exception as e:
            return ServiceHealth(
                name="LLM Providers",
                status=HealthStatus.UNKNOWN,
                message=str(e)[:50],
            )

    def check_ollama(self) -> ServiceHealth:
        """Check if Ollama is running for local routing classification."""
        import urllib.request

        start = time.time()
        try:
            req = urllib.request.Request("http://localhost:11434/api/tags")
            with urllib.request.urlopen(req, timeout=1) as resp:
                data = json.loads(resp.read())
            latency = (time.time() - start) * 1000

            models = [m.get("name", "") for m in data.get("models", [])]
            # Check if the configured routing model is available
            try:
                from axiom.extensions.builtins.settings.store import SettingsStore

                wanted = SettingsStore().get("routing.ollama_model", "llama3.2:1b")
            except Exception:
                wanted = "llama3.2:1b"

            has_model = any(wanted.split(":")[0] in m for m in models)

            if has_model:
                return ServiceHealth(
                    name="Ollama (local classifier)",
                    status=HealthStatus.HEALTHY,
                    message=f"Running, {wanted} available ({latency:.0f}ms)",
                    latency_ms=latency,
                    details={"models": models[:5], "routing_model": wanted},
                )
            else:
                return ServiceHealth(
                    name="Ollama (local classifier)",
                    status=HealthStatus.DEGRADED,
                    message=f"Running but {wanted} not pulled",
                    latency_ms=latency,
                    details={
                        "models": models[:5],
                        "routing_model": wanted,
                        "fix": f"ollama pull {wanted}",
                    },
                )

        except Exception:
            return ServiceHealth(
                name="Ollama (local classifier)",
                status=HealthStatus.DEGRADED,
                message="Not running (routing uses keyword-only fallback)",
                details={
                    "note": "Optional — improves export-control classification accuracy",
                    "fix": "brew install ollama && ollama serve",
                },
            )

    def check_routing(self) -> ServiceHealth:
        """Check routing configuration and sensitivity."""
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            store = SettingsStore()
            mode = store.get("routing.default_mode", "auto")
            sensitivity = store.get("routing.sensitivity", "balanced")
            vpn_policy = store.get("routing.on_vpn_unavailable", "warn")

            return ServiceHealth(
                name="Routing",
                status=HealthStatus.HEALTHY,
                message=f"mode={mode}, sensitivity={sensitivity}",
                details={
                    "default_mode": mode,
                    "sensitivity": sensitivity,
                    "on_vpn_unavailable": vpn_policy,
                },
            )
        except Exception:
            return ServiceHealth(
                name="Routing",
                status=HealthStatus.HEALTHY,
                message="Using defaults (auto, balanced)",
            )

    def check_database(self) -> ServiceHealth:
        """Check PostgreSQL database connectivity."""
        db_url = os.environ.get("AXIOM_DB_URL", "postgresql://axiom:axiom@localhost:5432/axiom_db")

        start = time.time()

        try:
            import psycopg2  # type: ignore[import-not-found]

            conn = psycopg2.connect(db_url, connect_timeout=5)
            cursor = conn.cursor()

            # Check basic connectivity
            cursor.execute("SELECT 1")

            # Get version
            cursor.execute("SELECT version()")
            version_row = cursor.fetchone()
            pg_version = version_row[0].split()[1] if version_row else "unknown"

            # Check pgvector
            cursor.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cursor.fetchone()
            pgvector_version = row[0] if row else None

            # Get table counts
            cursor.execute("""
                SELECT
                    (SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public')
            """)
            count_row = cursor.fetchone()
            table_count = count_row[0] if count_row else 0

            conn.close()
            latency = (time.time() - start) * 1000

            return ServiceHealth(
                name="PostgreSQL",
                status=HealthStatus.HEALTHY,
                message=f"Connected ({latency:.0f}ms)",
                latency_ms=latency,
                details={
                    "version": pg_version,
                    "pgvector": pgvector_version or "not installed",
                    "tables": table_count,
                    "url": self._mask_url(db_url),
                },
            )

        except ImportError:
            return ServiceHealth(
                name="PostgreSQL",
                status=HealthStatus.UNKNOWN,
                message="psycopg2 not installed",
                details={"fix": "pip install psycopg2-binary"},
            )
        except Exception as e:
            latency = (time.time() - start) * 1000
            error_msg = str(e).split("\n", maxsplit=1)[0][:80]

            # Check if it's just not running vs connection refused
            if "could not connect" in str(e).lower() or "connection refused" in str(e).lower():
                return ServiceHealth(
                    name="PostgreSQL",
                    status=HealthStatus.UNHEALTHY,
                    message="Not running",
                    latency_ms=latency,
                    details={
                        "error": error_msg,
                        "fix": "axi db up",
                    },
                )

            return ServiceHealth(
                name="PostgreSQL",
                status=HealthStatus.UNHEALTHY,
                message=f"Error: {error_msg}",
                latency_ms=latency,
            )

    def check_api_server(self, host: str = "localhost", port: int = 8000) -> ServiceHealth:
        """Check main API server (future web/mobile backend)."""
        url = f"http://{host}:{port}"

        import urllib.error
        import urllib.request

        start = time.time()

        # First check if port is open
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2)
            result = sock.connect_ex((host, port))
            sock.close()

            if result != 0:
                return ServiceHealth(
                    name="API Server",
                    status=HealthStatus.UNKNOWN,
                    message=f"Not running (port {port})",
                    details={"port": port, "note": "API server not yet implemented"},
                )

            # Port is open, try HTTP health check
            try:
                import urllib.request

                req = urllib.request.Request(f"{url}/health", method="GET")
                req.add_header("Accept", "application/json")

                with urllib.request.urlopen(req, timeout=5) as resp:
                    latency = (time.time() - start) * 1000
                    data = json.loads(resp.read().decode())

                    return ServiceHealth(
                        name="API Server",
                        status=HealthStatus.HEALTHY,
                        message=f"Running ({latency:.0f}ms)",
                        latency_ms=latency,
                        details={"url": url, "response": data},
                    )

            except urllib.error.HTTPError as e:
                latency = (time.time() - start) * 1000
                return ServiceHealth(
                    name="API Server",
                    status=HealthStatus.DEGRADED,
                    message=f"HTTP {e.code}",
                    latency_ms=latency,
                    details={"url": url, "error": str(e)},
                )
            except Exception:
                latency = (time.time() - start) * 1000
                return ServiceHealth(
                    name="API Server",
                    status=HealthStatus.DEGRADED,
                    message="Running but no /health endpoint",
                    latency_ms=latency,
                    details={"url": url},
                )

        except Exception:
            return ServiceHealth(
                name="API Server",
                status=HealthStatus.UNKNOWN,
                message="Not running",
                details={"port": port, "note": "API server not yet implemented"},
            )

    def check_sense_server(self, host: str = "localhost", port: int = 8765) -> ServiceHealth:
        """Check Sense inbox server."""
        import urllib.error
        import urllib.request

        url = f"http://{host}:{port}"

        start = time.time()

        try:
            req = urllib.request.Request(f"{url}/status", method="GET")

            with urllib.request.urlopen(req, timeout=5) as resp:
                latency = (time.time() - start) * 1000
                resp.read().decode()

                return ServiceHealth(
                    name="Signal Server",
                    status=HealthStatus.HEALTHY,
                    message=f"Running ({latency:.0f}ms)",
                    latency_ms=latency,
                    details={"url": url, "port": port},
                )

        except urllib.error.URLError:
            return ServiceHealth(
                name="Signal Server",
                status=HealthStatus.UNKNOWN,
                message=f"Not running (port {port})",
                details={
                    "port": port,
                    "fix": "axi signal serve &",
                },
            )
        except Exception as e:
            return ServiceHealth(
                name="Signal Server",
                status=HealthStatus.UNHEALTHY,
                message=str(e)[:50],
                details={"port": port},
            )

    def check_mcp_server(self) -> ServiceHealth:
        """Check MCP server status."""
        # MCP server runs on demand via stdio, not as a daemon
        # Check if it's importable and configured
        try:
            from axiom.mcp_server import server  # type: ignore[import-not-found]  # noqa: F401

            return ServiceHealth(
                name="MCP Server",
                status=HealthStatus.HEALTHY,
                message="Available (on-demand)",
                details={"mode": "stdio", "note": "Runs when invoked by client"},
            )
        except ImportError as e:
            return ServiceHealth(
                name="MCP Server",
                status=HealthStatus.UNKNOWN,
                message="Not installed",
                details={"error": str(e), "fix": "pip install mcp"},
            )
        except Exception as e:
            return ServiceHealth(
                name="MCP Server",
                status=HealthStatus.DEGRADED,
                message=str(e)[:50],
            )

    def check_mo(self) -> ServiceHealth:
        """Check TIDY scratch space health."""
        start = time.time()
        try:
            from axiom.extensions.builtins.hygiene import manager

            mgr = manager()
            info = mgr.status()
            latency = (time.time() - start) * 1000

            if not info.get("writable"):
                return ServiceHealth(
                    name="TIDY (Scratch)",
                    status=HealthStatus.UNHEALTHY,
                    message="Scratch dir not writable",
                    latency_ms=latency,
                    details={"base_dir": info.get("base_dir", "unknown")},
                )

            disk_pct = info.get("disk_used_pct", 0)
            active = info.get("active_entries", 0)
            free = info.get("disk_free_bytes", 0)

            if disk_pct >= 95:
                status = HealthStatus.UNHEALTHY
                message = f"Disk critical ({disk_pct}% used)"
            elif disk_pct >= 80:
                status = HealthStatus.DEGRADED
                message = f"Disk pressure ({disk_pct}% used)"
            else:
                status = HealthStatus.HEALTHY
                message = f"{active} entries, {self._format_bytes(free)} free"

            return ServiceHealth(
                name="TIDY (Scratch)",
                status=status,
                message=message,
                latency_ms=latency,
                details={
                    "base_dir": info.get("base_dir"),
                    "active_entries": active,
                    "disk_used_pct": disk_pct,
                },
            )

        except ImportError:
            return ServiceHealth(
                name="TIDY (Scratch)",
                status=HealthStatus.UNKNOWN,
                message="TIDY module not installed",
            )
        except Exception as e:
            return ServiceHealth(
                name="TIDY (Scratch)",
                status=HealthStatus.UNKNOWN,
                message=str(e)[:50],
            )

    @staticmethod
    def _format_bytes(n: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if abs(n) < 1024:
                return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
            n = int(n / 1024)
        return f"{n:.1f} TB"

    def _mask_url(self, url: str) -> str:
        """Mask password in URL."""
        import re

        return re.sub(r":[^:@]+@", ":****@", url)


def format_health_table(health: SystemHealth, use_color: bool = True) -> str:
    """Format health status as a table."""
    reset = "\033[0m" if use_color else ""
    bold = "\033[1m" if use_color else ""

    lines = []
    from axiom.infra.branding import get_branding

    product = get_branding().product_name
    lines.append(section_header(f"{bold}{product} System Health{reset}", width=50))

    for svc in health.services:
        color = svc.color_code if use_color else ""
        latency_str = f" ({svc.latency_ms:.0f}ms)" if svc.latency_ms else ""
        lines.append(f"  {color}{svc.icon}{reset} {svc.name}: {svc.message}{latency_str}")

        # Show important details
        if svc.details:
            if svc.status == HealthStatus.UNHEALTHY and "fix" in svc.details:
                lines.append(f"      Fix: {svc.details['fix']}")
            elif svc.status == HealthStatus.HEALTHY:
                if "version" in svc.details:
                    lines.append(f"      Version: {svc.details['version']}")
                if "pgvector" in svc.details and svc.details["pgvector"] != "not installed":
                    lines.append(f"      pgvector: {svc.details['pgvector']}")

    # Connections section
    try:
        from axiom.infra.connections import format_status_section

        conn_output = format_status_section()
        if conn_output and "No connections" not in conn_output:
            lines.append(separator("-", width=50))
            lines.append(f"{bold}Connections{reset}")
            lines.append(conn_output)
    except Exception:
        pass

    lines.append(separator("-", width=50))

    # Overall status
    overall_icon = {
        HealthStatus.HEALTHY: "✅",
        HealthStatus.DEGRADED: "⚠️",
        HealthStatus.UNHEALTHY: "❌",
        HealthStatus.UNKNOWN: "❓",
    }.get(health.overall, "?")

    lines.append(f"Overall: {overall_icon} {health.overall.value.upper()}")
    lines.append("")  # blank line before shell prompt

    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """Build argument parser."""
    parser = argparse.ArgumentParser(
        prog="axi status",
        description="System health dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  axi status              # Check all services
  axi status --db         # Check database only
  axi status --json       # Output as JSON
  axi status --watch      # Continuous monitoring
""",
    )

    parser.add_argument(
        "--db",
        "--database",
        action="store_true",
        help="Check database only",
    )
    parser.add_argument(
        "--api",
        action="store_true",
        help="Check API server only",
    )
    parser.add_argument(
        "--services",
        action="store_true",
        help="Check all background services",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )
    parser.add_argument(
        "--watch",
        "-w",
        action="store_true",
        help="Continuously monitor (refresh every 5s)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=5,
        help="Watch interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    checker = HealthChecker()
    use_color = not args.no_color and sys.stdout.isatty()

    def run_check() -> SystemHealth:
        if args.db:
            health = SystemHealth()
            health.services.append(checker.check_database())
            health.compute_overall()
        elif args.api:
            health = SystemHealth()
            health.services.append(checker.check_api_server())
            health.compute_overall()
        else:
            health = checker.check_all()
        return health

    if args.watch:
        try:
            while True:
                # Clear screen
                print("\033[2J\033[H", end="")

                health = run_check()

                if args.json:
                    print(json.dumps(health.to_dict(), indent=2))
                else:
                    print(format_health_table(health, use_color))
                    print(f"\nRefreshing every {args.interval}s... (Ctrl+C to stop)")

                time.sleep(args.interval)

        except KeyboardInterrupt:
            print("\nStopped.")
            return 0
    else:
        health = run_check()

        if args.json:
            print(json.dumps(health.to_dict(), indent=2))
        else:
            print(format_health_table(health, use_color))

        # Exit code based on health
        if health.overall == HealthStatus.UNHEALTHY:
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
