# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi connect <preset>`` framework — Item 8.

Today, configuring axi to talk to a specific endpoint (e.g., a private LLM +
RAG) requires hand-editing config files. This module ships a one-line
``axi connect <preset>`` that wires both an LLM endpoint and a RAG pack
server in one go.

Subcommands
-----------

::

    axi connect list                       # built-in + extension-discovered presets
    axi connect <preset> [--no-test] [--dry-run]
                                           # probe + write llm-providers.toml + rag-packs.toml
    axi connect status [--preset <name>]   # show wired endpoints + reachability

Anything that doesn't look like a preset subcommand or a known preset name
falls through to the legacy ``axi connect`` extension (connection-credential
setup); the new framework is additive, not a replacement.

Preset shape
------------

Built-in presets live in ``runtime/config.example/connect-presets.toml``
(excluded from default discovery; tests load explicitly). Extensions ship
their own presets via ``axiom-extension.toml`` ``[[connect.preset]]`` blocks.

::

    [[connect.preset]]
    name = "example-private-llm"
    description = "Generic private LLM + RAG over VPN"
    discovery_hint = "Reachable when on VPN"

    [[connect.preset.providers]]
    kind = "llm"
    provider_name = "private-llm"
    endpoint = "${PRIVATE_LLM_ENDPOINT}"   # env-var expanded at apply time
    api_key_env = "PRIVATE_LLM_KEY"
    routing_tier = "export_controlled"
    probe_path = "/v1/models"

    [[connect.preset.providers]]
    kind = "rag"
    endpoint = "${PRIVATE_RAG_ENDPOINT}"
    probe_path = "/v1/info"

Domain-agnostic
---------------

Per ``feedback_axiom_domain_agnostic``: built-in presets carry placeholder
names only ("axiom-internal-test"). Concrete consumer presets ship from
their own extension manifests.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from axiom.infra.toml_compat import load_toml

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ConnectCliError(Exception):
    """Base class for connect-CLI errors. Carries an exit code."""

    exit_code: int = 1


class PresetNotFound(ConnectCliError):
    exit_code = 2

    def __init__(self, name: str) -> None:
        super().__init__(f"preset not found: {name}")
        self.preset_name = name


class EndpointUnreachable(ConnectCliError):
    exit_code = 3

    def __init__(self, url: str, message: str) -> None:
        super().__init__(f"could not reach {url}: {message}")
        self.url = url
        self.detail = message


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class ProviderSpec:
    """One provider declared by a preset (LLM or RAG)."""

    kind: str  # "llm" | "rag"
    endpoint: str = ""
    provider_name: str = ""
    model: str = ""
    api_key_env: str = ""
    routing_tier: str = "any"
    routing_tags: list[str] = field(default_factory=list)
    probe_path: str = "/v1/info"
    requires_vpn: bool = False
    verify_ssl: bool = True
    extras: dict[str, object] = field(default_factory=dict)


@dataclass
class Preset:
    """A connect-preset bundles one-or-more providers (LLM + RAG)."""

    name: str
    description: str = ""
    discovery_hint: str = ""
    source: str = ""  # "builtin" | "extension:<ext-name>"
    providers: list[ProviderSpec] = field(default_factory=list)

    @property
    def llm_providers(self) -> list[ProviderSpec]:
        return [p for p in self.providers if p.kind == "llm"]

    @property
    def rag_providers(self) -> list[ProviderSpec]:
        return [p for p in self.providers if p.kind == "rag"]


@dataclass
class ProbeResult:
    ok: bool
    latency_ms: float = 0.0
    message: str = ""


Prober = Callable[[str], ProbeResult]


# ---------------------------------------------------------------------------
# Default HTTP probe (stdlib only)
# ---------------------------------------------------------------------------


def _http_probe(url: str, *, timeout: float = 5.0) -> ProbeResult:
    """HEAD-then-GET reachability probe using stdlib urllib.

    A 200/204/401/403 are all "the server answered" — for a probe, any HTTP
    response means the endpoint exists. Connection errors / timeouts are the
    real "unreachable" signal.
    """
    import time

    start = time.monotonic()
    try:
        # HEAD first — most cost-effective. If the server doesn't speak HEAD,
        # fall through to GET.
        for method in ("HEAD", "GET"):
            try:
                req = urllib.request.Request(url, method=method)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    code = getattr(resp, "status", 200)
                    elapsed = (time.monotonic() - start) * 1000
                    return ProbeResult(
                        ok=True,
                        latency_ms=elapsed,
                        message=f"HTTP {code}",
                    )
            except urllib.error.HTTPError as http_exc:
                # Server answered with a non-2xx — that's still "reachable".
                elapsed = (time.monotonic() - start) * 1000
                return ProbeResult(
                    ok=True,
                    latency_ms=elapsed,
                    message=f"HTTP {http_exc.code}",
                )
            except urllib.error.URLError as url_exc:
                if method == "HEAD":
                    # Try GET before giving up
                    continue
                return ProbeResult(ok=False, message=str(url_exc.reason))
            except Exception as exc:  # pragma: no cover — defensive
                if method == "HEAD":
                    continue
                return ProbeResult(ok=False, message=str(exc))
        return ProbeResult(ok=False, message="no response")
    except Exception as exc:  # pragma: no cover — defensive
        return ProbeResult(ok=False, message=str(exc))


# ---------------------------------------------------------------------------
# Dependency bundle (testing seam)
# ---------------------------------------------------------------------------


@dataclass
class ConnectCliDeps:
    """Injectable dependencies for the connect-preset CLI.

    ``prober`` lets tests stub the HTTP probe. ``builtin_presets_path`` points
    to the built-in preset TOML; ``extension_search_dirs`` is a tuple of
    directories to scan for ``axiom-extension.toml`` files containing
    ``[[connect.preset]]`` blocks.
    """

    runtime_config_dir: Path
    builtin_presets_path: Path
    extension_search_dirs: tuple[Path, ...]
    prober: Prober = field(default=_http_probe)


# ---------------------------------------------------------------------------
# Preset discovery
# ---------------------------------------------------------------------------


def _parse_provider(raw: dict) -> ProviderSpec:
    kind = str(raw.get("kind", "")).lower().strip()
    return ProviderSpec(
        kind=kind,
        endpoint=str(raw.get("endpoint", "")),
        provider_name=str(raw.get("provider_name", "")),
        model=str(raw.get("model", "")),
        api_key_env=str(raw.get("api_key_env", "")),
        routing_tier=str(raw.get("routing_tier", "any")),
        routing_tags=list(raw.get("routing_tags", []) or []),
        probe_path=str(raw.get("probe_path", "/v1/info") or "/v1/info"),
        requires_vpn=bool(raw.get("requires_vpn", False)),
        verify_ssl=bool(raw.get("verify_ssl", True)),
        extras={
            k: v
            for k, v in raw.items()
            if k
            not in (
                "kind",
                "endpoint",
                "provider_name",
                "model",
                "api_key_env",
                "routing_tier",
                "routing_tags",
                "probe_path",
                "requires_vpn",
                "verify_ssl",
            )
        },
    )


def _parse_presets_block(data: dict, source: str) -> list[Preset]:
    """Extract ``[[connect.preset]]`` blocks from a parsed TOML dict."""
    out: list[Preset] = []
    raw_list = (
        data.get("connect", {}).get("preset")
        if isinstance(data.get("connect"), dict)
        else None
    )
    if not raw_list:
        return out
    if not isinstance(raw_list, list):
        return out
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        providers_raw = entry.get("providers", []) or []
        providers = [
            _parse_provider(p) for p in providers_raw if isinstance(p, dict)
        ]
        out.append(
            Preset(
                name=name,
                description=str(entry.get("description", "")),
                discovery_hint=str(entry.get("discovery_hint", "")),
                source=source,
                providers=providers,
            )
        )
    return out


def discover_presets(deps: ConnectCliDeps) -> list[Preset]:
    """Discover all available presets — built-in + extension-shipped.

    Built-in presets come from ``deps.builtin_presets_path``. Extension
    presets are parsed from ``axiom-extension.toml`` files found by walking
    ``deps.extension_search_dirs``. Names are deduplicated; first wins.
    """
    seen: dict[str, Preset] = {}

    # Built-in presets
    if deps.builtin_presets_path.exists():
        try:
            data = load_toml(deps.builtin_presets_path)
            for preset in _parse_presets_block(data, source="builtin"):
                if preset.name not in seen:
                    seen[preset.name] = preset
        except Exception:
            pass  # Bad built-in file shouldn't break list/apply for extensions

    # Extension-shipped presets
    for ext_dir in deps.extension_search_dirs:
        if not ext_dir.is_dir():
            continue
        for manifest in sorted(ext_dir.glob("**/axiom-extension.toml")):
            try:
                data = load_toml(manifest)
            except Exception:
                continue
            ext_name = ""
            ext_section = data.get("extension", {})
            if isinstance(ext_section, dict):
                ext_name = str(ext_section.get("name", manifest.parent.name))
            for preset in _parse_presets_block(
                data, source=f"extension:{ext_name or manifest.parent.name}"
            ):
                if preset.name not in seen:
                    seen[preset.name] = preset

    return sorted(seen.values(), key=lambda p: p.name)


# ---------------------------------------------------------------------------
# Env-var expansion
# ---------------------------------------------------------------------------


_ENV_VAR_RE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _expand_env(text: str | None) -> str:
    """Expand ``${VAR}`` references against ``os.environ``.

    Raises :class:`ConnectCliError` if any referenced variable is unset. This
    is a hard error — silently substituting empty strings would yield a
    config file that "works" but points at nothing.
    """
    if not text:
        return ""
    missing: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        var = match.group(1)
        value = os.environ.get(var)
        if value is None or value == "":
            missing.append(var)
            return match.group(0)
        return value

    expanded = _ENV_VAR_RE.sub(_sub, text)
    if missing:
        names = ", ".join(sorted(set(missing)))
        raise ConnectCliError(
            f"required environment variable(s) not set: {names}. "
            f"Set them before running 'axi connect <preset>' "
            f"(e.g. via 'export {missing[0]}=...' or your .env file)."
        )
    return expanded


def _resolve_provider(provider: ProviderSpec) -> ProviderSpec:
    """Return a copy of *provider* with env-var-bearing fields expanded."""
    return ProviderSpec(
        kind=provider.kind,
        endpoint=_expand_env(provider.endpoint),
        provider_name=provider.provider_name,
        model=provider.model,
        api_key_env=provider.api_key_env,
        routing_tier=provider.routing_tier,
        routing_tags=list(provider.routing_tags),
        probe_path=provider.probe_path,
        requires_vpn=provider.requires_vpn,
        verify_ssl=provider.verify_ssl,
        extras=dict(provider.extras),
    )


# ---------------------------------------------------------------------------
# TOML rendering
# ---------------------------------------------------------------------------


def _toml_escape(value: str) -> str:
    """Escape a string for embedding in a TOML basic-string literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _render_llm_providers_toml(preset: Preset, providers: list[ProviderSpec]) -> str:
    """Render an llm-providers.toml file for the resolved providers.

    Format mirrors ``runtime/config.example/llm-providers.toml`` so the
    Gateway can read it without further translation.
    """
    lines: list[str] = [
        f"# Generated by `axi connect {preset.name}` — do not edit by hand.",
        "# Re-run `axi connect <preset>` to refresh.",
        "",
        "[gateway]",
        'format = "openai"',
        "",
    ]
    for idx, prov in enumerate(providers, start=1):
        lines.append("[[gateway.providers]]")
        name = prov.provider_name or f"{preset.name}-llm-{idx}"
        lines.append(f'name = "{_toml_escape(name)}"')
        lines.append(f'endpoint = "{_toml_escape(prov.endpoint)}"')
        if prov.model:
            lines.append(f'model = "{_toml_escape(prov.model)}"')
        if prov.api_key_env:
            lines.append(f'api_key_env = "{_toml_escape(prov.api_key_env)}"')
        lines.append("priority = 1")
        if prov.routing_tier:
            lines.append(f'routing_tier = "{_toml_escape(prov.routing_tier)}"')
        if prov.routing_tags:
            tags_repr = ", ".join(f'"{_toml_escape(t)}"' for t in prov.routing_tags)
            lines.append(f"routing_tags = [{tags_repr}]")
        lines.append(f"requires_vpn = {'true' if prov.requires_vpn else 'false'}")
        if not prov.verify_ssl:
            lines.append("verify_ssl = false")
        lines.append('use_for = ["extraction", "synthesis", "fallback"]')
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_rag_packs_toml(preset: Preset, providers: list[ProviderSpec]) -> str:
    """Render a rag-packs.toml file for the resolved RAG providers."""
    lines: list[str] = [
        f"# Generated by `axi connect {preset.name}` — do not edit by hand.",
        "# Re-run `axi connect <preset>` to refresh.",
        "",
    ]
    for idx, prov in enumerate(providers, start=1):
        name = prov.provider_name or f"{preset.name}-rag-{idx}"
        lines.append("[[rag.pack]]")
        lines.append(f'name = "{_toml_escape(name)}"')
        lines.append(f'endpoint = "{_toml_escape(prov.endpoint)}"')
        if prov.probe_path:
            lines.append(f'probe_path = "{_toml_escape(prov.probe_path)}"')
        if prov.api_key_env:
            lines.append(f'api_key_env = "{_toml_escape(prov.api_key_env)}"')
        if prov.routing_tier and prov.routing_tier != "any":
            lines.append(f'routing_tier = "{_toml_escape(prov.routing_tier)}"')
        if prov.routing_tags:
            tags_repr = ", ".join(f'"{_toml_escape(t)}"' for t in prov.routing_tags)
            lines.append(f"routing_tags = [{tags_repr}]")
        lines.append(f"requires_vpn = {'true' if prov.requires_vpn else 'false'}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Probing helpers
# ---------------------------------------------------------------------------


def _probe_url_for(provider: ProviderSpec) -> str:
    """Compose the URL used for the reachability probe."""
    base = provider.endpoint.rstrip("/")
    path = provider.probe_path or "/v1/info"
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def _probe_all(
    providers: list[ProviderSpec], prober: Prober
) -> list[tuple[ProviderSpec, ProbeResult]]:
    return [(p, prober(_probe_url_for(p))) for p in providers]


# ---------------------------------------------------------------------------
# Atomic file writes
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, content: str) -> None:
    """Write *content* to *path* atomically (tmp + rename).

    Ensures partial writes never land — important when an apply fails part
    way, the previous file is left intact.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Subcommand: list
# ---------------------------------------------------------------------------


def cmd_list(*, argv: Sequence[str], deps: ConnectCliDeps) -> int:
    parser = argparse.ArgumentParser(prog="axi connect list")
    parser.parse_args(list(argv))

    presets = discover_presets(deps)
    if not presets:
        print("(no presets discovered)")
        print()
        print("Built-in presets live in runtime/config.example/connect-presets.toml.")
        print("Extensions ship presets via [[connect.preset]] blocks in axiom-extension.toml.")
        return 0

    print("Connect presets:")
    print()
    for preset in presets:
        kinds = sorted({p.kind for p in preset.providers if p.kind})
        kinds_str = "+".join(kinds) if kinds else "(no providers)"
        print(f"  {preset.name:<32} [{preset.source}]  ({kinds_str})")
        if preset.description:
            print(f"      {preset.description}")
        if preset.discovery_hint:
            print(f"      hint: {preset.discovery_hint}")
    print()
    print("Apply with:  axi connect <preset>")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: <preset> apply
# ---------------------------------------------------------------------------


def cmd_apply(*, argv: Sequence[str], deps: ConnectCliDeps) -> int:
    parser = argparse.ArgumentParser(prog="axi connect <preset>")
    parser.add_argument("preset", help="Preset name (see 'axi connect list').")
    parser.add_argument(
        "--no-test",
        action="store_true",
        help="Skip endpoint reachability probes before writing.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be configured + exit; write nothing.",
    )
    args = parser.parse_args(list(argv))

    presets = discover_presets(deps)
    matched = next((p for p in presets if p.name == args.preset), None)
    if matched is None:
        available = ", ".join(p.name for p in presets) or "(none discovered)"
        print(
            f"axi connect: preset not found: {args.preset}",
            file=sys.stderr,
        )
        print(f"available presets: {available}", file=sys.stderr)
        return PresetNotFound(args.preset).exit_code

    # Resolve env vars eagerly so we fail before probing or writing
    try:
        resolved = [_resolve_provider(p) for p in matched.providers]
    except ConnectCliError as exc:
        print(f"axi connect: {exc}", file=sys.stderr)
        return exc.exit_code

    llm_resolved = [p for p in resolved if p.kind == "llm"]
    rag_resolved = [p for p in resolved if p.kind == "rag"]

    # ── Dry run ────────────────────────────────────────────────────────────
    if args.dry_run:
        print(f"[dry-run] preset: {matched.name}")
        if matched.description:
            print(f"[dry-run] {matched.description}")
        print()
        if llm_resolved:
            print("[dry-run] would write runtime/config/llm-providers.toml:")
            print(_render_llm_providers_toml(matched, llm_resolved))
        if rag_resolved:
            print("[dry-run] would write runtime/config/rag-packs.toml:")
            print(_render_rag_packs_toml(matched, rag_resolved))
        return 0

    # ── Probe each endpoint ────────────────────────────────────────────────
    if not args.no_test:
        results = _probe_all(resolved, deps.prober)
        failed = [(prov, result) for prov, result in results if not result.ok]
        if failed:
            print(
                f"axi connect: preset '{matched.name}' has unreachable endpoints:",
                file=sys.stderr,
            )
            for prov, result in failed:
                print(
                    f"  - {prov.kind}  {_probe_url_for(prov)}  ({result.message})",
                    file=sys.stderr,
                )
            print(
                "No config written. Re-run with --no-test to skip the probe, "
                "or fix the endpoints.",
                file=sys.stderr,
            )
            return EndpointUnreachable(
                _probe_url_for(failed[0][0]), failed[0][1].message
            ).exit_code

    # ── Render content ─────────────────────────────────────────────────────
    llm_text = _render_llm_providers_toml(matched, llm_resolved) if llm_resolved else None
    rag_text = _render_rag_packs_toml(matched, rag_resolved) if rag_resolved else None

    # ── Write atomically ───────────────────────────────────────────────────
    written: list[Path] = []
    if llm_text is not None:
        path = deps.runtime_config_dir / "llm-providers.toml"
        _atomic_write(path, llm_text)
        written.append(path)
    if rag_text is not None:
        path = deps.runtime_config_dir / "rag-packs.toml"
        _atomic_write(path, rag_text)
        written.append(path)

    # ── Confirmation ───────────────────────────────────────────────────────
    print(f"axi connect: applied preset '{matched.name}'")
    for prov in llm_resolved:
        print(f"  llm  {prov.provider_name or '(unnamed)':<24} {prov.endpoint}")
    for prov in rag_resolved:
        print(f"  rag  {prov.provider_name or '(unnamed)':<24} {prov.endpoint}")
    for path in written:
        print(f"  wrote {path}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: status
# ---------------------------------------------------------------------------


def _read_configured_endpoints(runtime_config_dir: Path) -> list[tuple[str, str, str]]:
    """Return ``(kind, name, endpoint)`` triples from current runtime config."""
    triples: list[tuple[str, str, str]] = []
    llm_path = runtime_config_dir / "llm-providers.toml"
    if llm_path.exists():
        data = load_toml(llm_path)
        gw = data.get("gateway", {})
        if isinstance(gw, dict):
            for prov in gw.get("providers", []) or []:
                if not isinstance(prov, dict):
                    continue
                triples.append(
                    (
                        "llm",
                        str(prov.get("name", "")),
                        str(prov.get("endpoint", "")),
                    )
                )
    rag_path = runtime_config_dir / "rag-packs.toml"
    if rag_path.exists():
        data = load_toml(rag_path)
        rag = data.get("rag", {})
        if isinstance(rag, dict):
            for pack in rag.get("pack", []) or []:
                if not isinstance(pack, dict):
                    continue
                triples.append(
                    (
                        "rag",
                        str(pack.get("name", "")),
                        str(pack.get("endpoint", "")),
                    )
                )
    return triples


def cmd_status(*, argv: Sequence[str], deps: ConnectCliDeps) -> int:
    parser = argparse.ArgumentParser(prog="axi connect status")
    parser.add_argument("--preset", default=None, help="Filter to one preset's expected endpoints.")
    args = parser.parse_args(list(argv))

    if args.preset is not None:
        presets = discover_presets(deps)
        matched = next((p for p in presets if p.name == args.preset), None)
        if matched is None:
            print(f"axi connect status: preset not found: {args.preset}", file=sys.stderr)
            return 2
        try:
            resolved = [_resolve_provider(p) for p in matched.providers]
        except ConnectCliError as exc:
            print(f"axi connect status: {exc}", file=sys.stderr)
            return exc.exit_code
        print(f"Status for preset '{matched.name}':")
        for prov in resolved:
            url = _probe_url_for(prov)
            result = deps.prober(url)
            symbol = "ok" if result.ok else "fail"
            print(
                f"  [{symbol:<4}] {prov.kind:<3}  {prov.provider_name or '(unnamed)':<24} "
                f"{prov.endpoint}  ({result.message})"
            )
        return 0

    triples = _read_configured_endpoints(deps.runtime_config_dir)
    if not triples:
        print("axi connect status: no providers currently configured.")
        print(
            f"  (looked in {deps.runtime_config_dir}/llm-providers.toml + "
            f"{deps.runtime_config_dir}/rag-packs.toml)"
        )
        print("  Run 'axi connect list' to see available presets.")
        return 0

    print("Currently-configured endpoints:")
    for kind, name, endpoint in triples:
        if not endpoint:
            print(f"  [n/a ] {kind:<3}  {name or '(unnamed)':<24} (no endpoint)")
            continue
        # Probe at the bare endpoint root — we don't know the probe path here
        result = deps.prober(endpoint.rstrip("/") + "/")
        symbol = "ok" if result.ok else "fail"
        print(
            f"  [{symbol:<4}] {kind:<3}  {name or '(unnamed)':<24} {endpoint}  ({result.message})"
        )
    return 0


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


# Reserved subcommand words handled by the preset framework. Anything else
# falls through to the legacy connect extension (connection-credential setup).
_PRESET_SUBCOMMANDS = {"list", "status"}


def _legacy_connect_main(argv: Sequence[str] | None = None) -> int:
    """Dispatch to the existing connect extension (connection-credential setup).

    Lazy-import so a missing/unavailable extension doesn't break the preset
    framework. Returns the legacy main's exit code.
    """
    try:
        from axiom.extensions.builtins.connect.cli import main as _legacy_main
    except Exception as exc:
        print(f"axi connect: legacy connection setup unavailable: {exc}", file=sys.stderr)
        return 1
    rc = _legacy_main(list(argv) if argv is not None else None)
    return int(rc) if isinstance(rc, int) else 0


def _looks_like_preset_command(first_arg: str, deps: ConnectCliDeps) -> bool:
    """Decide whether *first_arg* should route to the preset framework."""
    if first_arg in _PRESET_SUBCOMMANDS:
        return True
    if first_arg.startswith("-"):
        return False
    # Match against discovered preset names (built-in + extension).
    try:
        names = {p.name for p in discover_presets(deps)}
    except Exception:
        return False
    return first_arg in names


def _build_default_deps() -> ConnectCliDeps:
    """Construct production deps from cwd / project root."""
    from axiom.infra.paths import get_project_root

    project_root = get_project_root()
    runtime_config = project_root / "runtime" / "config"
    builtin_presets = project_root / "runtime" / "config.example" / "connect-presets.toml"

    # Extension search dirs: project + user + builtin (mirrors discovery.get_extension_dirs)
    search_dirs: list[Path] = []
    try:
        from axiom.extensions.discovery import get_extension_dirs

        search_dirs = list(get_extension_dirs())
    except Exception:
        pass

    return ConnectCliDeps(
        runtime_config_dir=runtime_config,
        builtin_presets_path=builtin_presets,
        extension_search_dirs=tuple(search_dirs),
        prober=_http_probe,
    )


def build_parser() -> argparse.ArgumentParser:
    """Top-level parser used for ``--help`` and tab completion.

    The dispatcher in :func:`main` picks subcommands manually so we can fall
    through to the legacy connect extension; argparse is only used to render
    the help text.
    """
    parser = argparse.ArgumentParser(
        prog="axi connect",
        description=(
            "Wire up LLM + RAG endpoints in one shot via presets. "
            "Falls through to legacy connection-credential setup for unknown args."
        ),
    )
    sub = parser.add_subparsers(dest="action")
    sub.add_parser(
        "list",
        help="List available presets (built-in + extension-discovered).",
    )
    apply_p = sub.add_parser(
        "apply",
        help="Apply a preset (alias for `axi connect <name>`).",
    )
    apply_p.add_argument("preset")
    apply_p.add_argument("--no-test", action="store_true")
    apply_p.add_argument("--dry-run", action="store_true")
    status_p = sub.add_parser("status", help="Show currently-configured endpoints + reachability.")
    status_p.add_argument("--preset", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)

    if not argv:
        # No args — preserve legacy behavior (list connections).
        return _legacy_connect_main([])

    if argv[0] in ("-h", "--help"):
        build_parser().print_help()
        print()
        print(
            "Note: any args that aren't preset subcommands fall through to the "
            "legacy `axi connect` (connection-credential setup)."
        )
        return 0

    deps = _build_default_deps()
    first = argv[0]

    try:
        if first == "list":
            return cmd_list(argv=argv[1:], deps=deps)
        if first == "status":
            return cmd_status(argv=argv[1:], deps=deps)
        if _looks_like_preset_command(first, deps):
            # Treat as `axi connect <preset> [args...]`
            return cmd_apply(argv=argv, deps=deps)
    except ConnectCliError as exc:
        print(f"axi connect: {exc}", file=sys.stderr)
        return exc.exit_code

    # Fall through to legacy connect extension (existing behavior).
    return _legacy_connect_main(argv)


if __name__ == "__main__":
    sys.exit(main())
