# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Install-time federation probe — first cut of spec-federation §6.6.

Discovers reachable LLM endpoints from already-registered connection
presets (via ConnectionRegistry), surfaces a decision-context prompt
to the user, and writes the accepted endpoint to llm-providers.toml.

What this MVP does NOT yet implement from §6.6:

- mDNS / DNS-SRV discovery (§6.6.3 step 1) — those mechanisms aren't
  built yet on the federation side. Discovery here is limited to the
  manifest-declared connection presets that ship with axiom and its
  extensions.
- Agent Card signature verification (§6.6.7) — federation identity
  surface isn't wired through ConnectionRegistry yet. The probe
  treats the manifest-shipped preset as trusted by construction
  (it shipped with the install).
- federation_peers.toml writeback (§6.6.5 step 2) — only writes
  llm-providers.toml; peer-registry writeback follows once the
  federation identity gating is in place.

What this MVP DOES implement:

- §6.6.2 trigger point #1 (install). On-demand + periodic come later.
- §6.6.3 probe semantics for already-registered connections.
- §6.6.4 decision-context prompt with the field set from the spec.
- §6.6.5 step 1: llm-providers.toml writeback (atomic).
- §6.6.6 decline memoization so repeat installs don't re-prompt for
  the same endpoint.
- TTY guard: non-TTY contexts get a notice + the adopt-non-
  interactively command, never auto-adopt.

See: spec-federation.md §6.6 for the full contract.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from axiom.infra.branding import get_branding
from axiom.infra.connections import (
    Connection,
    ConnectionRegistry,
    HealthStatus,
    check_health,
)

log = logging.getLogger(__name__)


@dataclass
class ProbeResult:
    """One probed endpoint + the data needed to render the §6.6.4 prompt."""

    connection: Connection
    reachable: bool
    latency_ms: int | None  # None if unreachable / timed out
    rag_corpus: str | None  # populated when a sibling RAG resource maps to this endpoint


def probe_endpoint(conn: Connection, registry: ConnectionRegistry) -> ProbeResult:
    """Health-check a connection via the existing check_health helper."""
    start = time.monotonic()
    reachable = False
    measured_ms: int | None = None
    try:
        health = check_health(conn.name, registry=registry)
        reachable = health.status == HealthStatus.HEALTHY
        # Prefer the health check's own latency_ms if it reported one
        # (TCP-connect path measures it natively).
        if health.latency_ms:
            measured_ms = int(health.latency_ms)
    except Exception as exc:
        log.debug("probe failed for %s: %s", conn.name, exc)
    if measured_ms is None and reachable:
        measured_ms = int((time.monotonic() - start) * 1000)
    return ProbeResult(
        connection=conn,
        reachable=reachable,
        latency_ms=measured_ms if reachable else None,
        rag_corpus=None,  # populated later by _attach_rag_companions
    )


def _attach_rag_companions(results: list[ProbeResult], registry: ConnectionRegistry) -> None:
    """If a probed LLM endpoint has a sibling RAG resource at the same
    host:port, attach the corpus descriptor so the prompt can surface it.
    """
    rag_conns = {c.endpoint: c for c in registry.by_category("rag")}
    for r in results:
        ep = r.connection.endpoint or ""
        # Match by host:port prefix — RAG sibling typically lives on a
        # different path of the same gateway.
        host_port = ep.split("//", 1)[-1].split("/", 1)[0]
        for rag_ep, rag_conn in rag_conns.items():
            rag_host_port = rag_ep.split("//", 1)[-1].split("/", 1)[0]
            if host_port and host_port == rag_host_port:
                r.rag_corpus = getattr(rag_conn, "display_name", rag_conn.name)
                break


def discover_llm_endpoints(registry: ConnectionRegistry) -> list[ProbeResult]:
    """Probe every registered llm-category connection; return reachable ones."""
    llm_conns: Iterable[Connection] = registry.by_category("llm")
    results = [probe_endpoint(c, registry) for c in llm_conns]
    reachable = [r for r in results if r.reachable]
    _attach_rag_companions(reachable, registry)
    return reachable


def render_prompt(result: ProbeResult) -> str:
    """Format the §6.6.4 decision-context prompt body."""
    c = result.connection
    is_ec_safe = (getattr(c, "routing_tier", "") or "") == "export_controlled"
    ec_text = (
        "EC-safe"
        if is_ec_safe
        else "NOT EC-safe (gateway cloud-routes; avoid sensitive content)"
    )
    cost_text = "free" if not getattr(c, "credential_env_var", "") else "requires API key"
    access_text = (
        f"requires {getattr(c, 'vpn_name', None) or 'VPN'}"
        if getattr(c, "requires_vpn", False) or getattr(c, "vpn_name", None)
        else "open"
    )
    lines = [
        "",
        f"📡 Detected federated LLM service: {c.display_name or c.name}",
        "",
        f"   Endpoint:    {c.endpoint or '(unknown)'}",
        f"   Latency:     {result.latency_ms}ms"
        if result.latency_ms is not None
        else "   Latency:     unknown",
        f"   Cost:        {cost_text}",
        f"   EC posture:  {ec_text}",
    ]
    if result.rag_corpus:
        lines.append(f"   RAG corpus:  {result.rag_corpus}")
    lines.append(f"   Access:      {access_text}")
    lines.append(f"   Federated:   yes — {c.extension or 'shipped preset'}")
    lines.append("")
    return "\n".join(lines)


def _decline_store_path() -> Path:
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "federation_declined.json"


def has_declined(conn_name: str) -> bool:
    """True if the user previously declined this endpoint."""
    p = _decline_store_path()
    if not p.exists():
        return False
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return conn_name in data
    except Exception:
        return False


def record_decline(conn_name: str) -> None:
    """Memoize a decline so future probes don't re-prompt."""
    p = _decline_store_path()
    try:
        data: dict[str, str] = (
            json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        )
    except Exception:
        data = {}
    data[conn_name] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_provider_entry(result: ProbeResult, llm_providers_path: Path) -> None:
    """Append a [[gateway.providers]] block to llm-providers.toml.

    Atomic write via temp file + rename. Marks the new provider as
    default = true if no other [[providers]] block exists yet.
    """
    c = result.connection
    existing = llm_providers_path.read_text(encoding="utf-8") if llm_providers_path.exists() else ""
    is_first = "[[gateway.providers]]" not in existing and "[[providers]]" not in existing
    today = time.strftime("%Y-%m-%d", time.gmtime())
    block_lines = [
        "",
        f"# Auto-added by federation probe {today} from preset '{c.name}'.",
        "[[gateway.providers]]",
        f'name         = "{c.name}"',
        f'endpoint     = "{c.endpoint}"',
    ]
    if getattr(c, "model", ""):
        block_lines.append(f'model        = "{c.model}"')
    if getattr(c, "credential_env_var", ""):
        block_lines.append(f'api_key_env  = "{c.credential_env_var}"')
    routing_tier = getattr(c, "routing_tier", "") or "any"
    block_lines.append(f'routing_tier = "{routing_tier}"')
    if getattr(c, "requires_vpn", False) or getattr(c, "vpn_name", None):
        block_lines.append("requires_vpn = true")
    if is_first:
        block_lines.append("default      = true")
    block_lines.append("")
    new_text = existing.rstrip("\n") + "\n" + "\n".join(block_lines)

    llm_providers_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = llm_providers_path.with_suffix(llm_providers_path.suffix + ".tmp")
    tmp.write_text(new_text, encoding="utf-8")
    tmp.replace(llm_providers_path)


def run_install_probe(
    registry: ConnectionRegistry | None = None,
    llm_providers_path: Path | None = None,
    stdin_is_tty: bool | None = None,
) -> int:
    """Orchestrate the install-time probe + prompt + writeback.

    Returns the number of providers adopted (0 if none, including all
    decline / no-reachable paths). Never raises; install-flow callers
    should treat it as best-effort.
    """
    if registry is None:
        registry = ConnectionRegistry()
        try:
            registry.discover_from_extensions()
        except Exception as exc:
            log.warning("connection discovery failed: %s", exc)
            return 0

    if llm_providers_path is None:
        from axiom.infra.paths import get_runtime_config_dir

        llm_providers_path = get_runtime_config_dir() / "llm-providers.toml"

    if stdin_is_tty is None:
        stdin_is_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())

    brand = get_branding()
    cli = (brand.cli_name or "axi").strip()

    candidates = discover_llm_endpoints(registry)
    if not candidates:
        return 0

    candidates = [r for r in candidates if not has_declined(r.connection.name)]
    if not candidates:
        return 0

    if not stdin_is_tty:
        # §6.6.4: non-TTY contexts get a notice + the explicit command.
        print("\n  Federated LLM service(s) detected:")
        for r in candidates:
            print(f"    • {r.connection.name} → {r.connection.endpoint}")
        print(f"  Adopt non-interactively: `{cli} federation adopt <name>`")
        print()
        return 0

    adopted = 0
    for r in candidates:
        print(render_prompt(r))
        try:
            answer = input("Use this as your default LLM? [Y/n] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return adopted
        if answer in ("", "y", "yes"):
            try:
                write_provider_entry(r, llm_providers_path)
                adopted += 1
                print(
                    f"   ✓ adopted {r.connection.name} as default LLM.\n"
                    f"     Try it: {cli} chat \"hello\"\n"
                )
            except Exception as exc:
                log.warning("writeback failed for %s: %s", r.connection.name, exc)
                print(f"   ⚠ couldn't write provider entry: {exc}\n")
        else:
            record_decline(r.connection.name)
            print(
                "   ⏭ declined; will not re-prompt for this endpoint.\n"
                f"     Re-consider later: `{cli} federation discover --reconsider`\n"
            )
    return adopted
