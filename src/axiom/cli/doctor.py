# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``axi doctor`` — proactive node-health diagnostic (Improvement Item 3).

The 2026-Q1 head-to-head incident surfaced a class of silent failures where the
node returns "OK" for ``axi config --status`` but is operationally
broken: the routing classifier model isn't pulled, the RAG corpus is
empty, an extension manifest fails to parse, or a runtime config file
is missing. Each existing health surface answers part of the question.
``axi doctor`` is the unified entry point that aggregates every check
into one command operators run after ``axi install`` or
``axi connect <preset>``.

Subcommands
-----------

::

    axi doctor                  Run all checks; print grouped report.
    axi doctor run              Same as the bare form.
    axi doctor --json           Emit machine-readable JSON for scripts/CI.
    axi doctor --check <name>   Run only one named check (debugging).
    axi doctor list             List all registered checks (discovery).

Architecture
------------

The CLI runs a sequence of :class:`Check` records. Each check is a
``() -> CheckResult`` callable that returns ``(status, summary,
fix_hint, detail)``. ``run_checks`` wraps each call in ``try/except``
so a single broken check never poisons the rest of the diagnostic.
Status icons surface in plain-text output; the JSON renderer emits the
exact same structured payload for CI integrations.

Per ``feedback_axiom_domain_agnostic``: no domain naming in checks or
output. Per ``feedback_rich_console_lazy_construction``: ``rich.Console``
is built inside the renderer (so capsys can capture). Per
``feedback_proactive_ux_minimize_cognitive_load``: every error result
includes a ``fix_hint`` with the exact command an operator should run.

The module is defensive about *its own* dependencies — Items 1 and 2
(typed classifier failures + classifier-health surface) are running in
parallel, so this code wraps every cross-module import in
``try/except`` and degrades to a minimal local probe rather than
failing the diagnostic.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from axiom.infra.host_hazards import HostHazardProvider

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class CheckStatus(str, Enum):
    """The four outcomes ``run_checks`` can report.

    OK       — check passed; node is healthy on this dimension.
    WARNING  — node works but is degraded (e.g. empty RAG corpus on a
               node that doesn't actively use RAG).
    ERROR    — node will fail at first request (e.g. classifier model
               not loaded; LLM endpoint unreachable).
    SKIPPED  — check not applicable (e.g. RAG check on a node without
               RAG configured at all).
    """

    OK = "ok"
    WARNING = "warning"
    ERROR = "error"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class CheckResult:
    """Result of running a single :class:`Check`.

    ``summary`` is a one-line status. ``fix_hint`` carries the exact
    command an operator should run to recover; ``None`` when no fix
    applies (success / not-applicable). ``detail`` is structured data
    surfaced verbatim by ``--json`` callers.
    """

    status: CheckStatus
    summary: str
    fix_hint: str | None = None
    detail: dict[str, Any] | None = None


@dataclass(frozen=True)
class Check:
    """A registered diagnostic check.

    ``name`` is the operator-visible label (used by ``--check <name>``).
    ``category`` groups checks in the text renderer. ``fn`` is the
    callable; ``run_checks`` will wrap it in ``try/except`` so any
    raise turns into a structured ERROR result.
    """

    name: str
    category: str
    fn: Callable[[], CheckResult]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_checks(checks: Sequence[Check]) -> tuple[CheckResult, ...]:
    """Run each check; collect results.

    Never raises — every check is wrapped so one broken check doesn't
    kill the diagnostic. A check that throws is reported as
    :attr:`CheckStatus.ERROR` with the exception message in
    ``summary`` and the exception's class name in ``detail``.
    """
    results: list[CheckResult] = []
    for check in checks:
        try:
            result = check.fn()
            if not isinstance(result, CheckResult):
                # Defensive — bad return type is treated as ERROR.
                result = CheckResult(
                    status=CheckStatus.ERROR,
                    summary=f"check '{check.name}' returned {type(result).__name__}, expected CheckResult",
                    fix_hint=None,
                    detail={"raw": repr(result)},
                )
        except Exception as exc:  # noqa: BLE001 — catch-all by design
            result = CheckResult(
                status=CheckStatus.ERROR,
                summary=f"check raised {type(exc).__name__}: {exc}",
                fix_hint=None,
                detail={"exception": type(exc).__name__, "message": str(exc)},
            )
        results.append(result)
    return tuple(results)


# ---------------------------------------------------------------------------
# Built-in check implementations
# ---------------------------------------------------------------------------


def _project_root() -> Path:
    """Resolve project root, falling back to cwd if helper unavailable."""
    try:
        from axiom.infra.paths import get_project_root

        return get_project_root()
    except Exception:
        return Path.cwd()


def _runtime_config_dir() -> Path:
    return _project_root() / "runtime" / "config"


# ---- Check 1: LLM endpoints reachable -------------------------------------


def _http_probe(url: str, *, timeout: float = 3.0) -> tuple[bool, str]:
    """Lightweight HEAD/GET reachability probe (stdlib only).

    Returns ``(reachable, message)``. Any HTTP response (including 4xx)
    counts as reachable; only connection errors / timeouts indicate
    "endpoint not there."
    """
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = getattr(resp, "status", 200)
                return True, f"HTTP {code}"
        except urllib.error.HTTPError as http_exc:
            return True, f"HTTP {http_exc.code}"
        except urllib.error.URLError as url_exc:
            if method == "HEAD":
                continue
            return False, str(url_exc.reason)
        except Exception as exc:  # noqa: BLE001 — defensive
            if method == "HEAD":
                continue
            return False, str(exc)
    return False, "no response"


def _read_llm_providers(config_dir: Path) -> list[dict[str, Any]]:
    """Return parsed [[gateway.providers]] entries; tolerant of missing file."""
    path = config_dir / "llm-providers.toml"
    if not path.exists():
        return []
    try:
        from axiom.infra.toml_compat import load_toml

        data = load_toml(path)
    except Exception:
        return []
    gateway = data.get("gateway", {})
    if not isinstance(gateway, dict):
        return []
    providers = gateway.get("providers", []) or []
    return [p for p in providers if isinstance(p, dict)]


def check_llm_endpoints(
    *,
    config_dir: Path | None = None,
    probe: Callable[[str], tuple[bool, str]] | None = None,
) -> CheckResult:
    """Probe each provider in ``runtime/config/llm-providers.toml``."""
    cfg = config_dir if config_dir is not None else _runtime_config_dir()
    probe_fn = probe if probe is not None else _http_probe
    providers = _read_llm_providers(cfg)
    if not providers:
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary="no LLM providers configured (llm-providers.toml absent or empty)",
            fix_hint="axi connect <preset>  # to wire an LLM endpoint",
            detail={"providers": []},
        )

    reachable: list[dict[str, Any]] = []
    unreachable: list[dict[str, Any]] = []
    for prov in providers:
        endpoint = str(prov.get("endpoint", "")).strip()
        name = str(prov.get("name", "(unnamed)"))
        if not endpoint:
            unreachable.append({"name": name, "endpoint": "", "message": "no endpoint"})
            continue
        ok, msg = probe_fn(endpoint)
        record = {"name": name, "endpoint": endpoint, "message": msg}
        (reachable if ok else unreachable).append(record)

    detail = {"reachable": reachable, "unreachable": unreachable}
    if not unreachable:
        return CheckResult(
            status=CheckStatus.OK,
            summary=f"{len(reachable)} provider(s) reachable",
            fix_hint=None,
            detail=detail,
        )
    sample = unreachable[0]
    return CheckResult(
        status=CheckStatus.ERROR,
        summary=(
            f"{len(unreachable)} of {len(providers)} provider(s) unreachable; "
            f"first: {sample['name']} ({sample['endpoint']}) — {sample['message']}"
        ),
        fix_hint="axi connect status  # to inspect; verify VPN / endpoint URL",
        detail=detail,
    )


# ---- Check 2: Routing classifier model loaded -----------------------------


def check_classifier_model(
    *,
    endpoint: str | None = None,
    model: str | None = None,
    runner: Callable[..., dict | None] | None = None,
) -> CheckResult:
    """Verify the routing classifier's SLM is pulled + serving.

    Prefers :func:`axiom.infra.routing_health.collect_classifier_health`
    (parallel Item 2). When that module isn't present yet, falls back to
    a local Ollama ``/api/tags`` probe so this check still adds value.
    """
    # Discover endpoint + model from env / classifier config (very best
    # effort — Item 2 will provide a richer surface).
    import os

    classifier_endpoint = endpoint or os.environ.get(
        "AXIOM_CLASSIFIER_ENDPOINT", "http://localhost:11434"
    )
    classifier_model = model or os.environ.get("AXIOM_CLASSIFIER_MODEL", "llama3.2:1b")

    # Preferred path: Item 2's helper.
    try:
        from axiom.infra.routing_health import collect_classifier_health

        kwargs: dict[str, Any] = {
            "endpoint": classifier_endpoint,
            "model": classifier_model,
        }
        if runner is not None:
            kwargs["runner"] = runner
        health = collect_classifier_health(**kwargs)
        endpoint_reachable = bool(getattr(health, "endpoint_reachable", False))
        model_loaded = bool(getattr(health, "model_loaded", False))
        configured_model = getattr(health, "configured_model", classifier_model)
        detail = {
            "endpoint": classifier_endpoint,
            "configured_model": configured_model,
            "endpoint_reachable": endpoint_reachable,
            "model_loaded": model_loaded,
            "source": "axiom.infra.routing_health",
        }
        if not endpoint_reachable:
            return CheckResult(
                status=CheckStatus.ERROR,
                summary=f"classifier endpoint unreachable ({classifier_endpoint})",
                fix_hint=f"start the local SLM server  # e.g. `ollama serve` on {classifier_endpoint}",
                detail=detail,
            )
        if not model_loaded:
            return CheckResult(
                status=CheckStatus.ERROR,
                summary=(
                    f"configured classifier model not loaded: "
                    f"{configured_model} (endpoint {classifier_endpoint})"
                ),
                fix_hint=f"ollama pull {configured_model}",
                detail=detail,
            )
        return CheckResult(
            status=CheckStatus.OK,
            summary=f"{configured_model} loaded on {classifier_endpoint}",
            fix_hint=None,
            detail=detail,
        )
    except ImportError:
        # Item 2 not landed — fall back to local probe.
        pass

    # Fallback: minimal Ollama /api/tags probe.
    url = classifier_endpoint.rstrip("/") + "/api/tags"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception as exc:  # noqa: BLE001 — defensive
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"classifier endpoint unreachable ({classifier_endpoint}): {exc}",
            fix_hint=f"start the local SLM server  # e.g. `ollama serve` on {classifier_endpoint}",
            detail={
                "endpoint": classifier_endpoint,
                "configured_model": classifier_model,
                "source": "fallback-ollama-tags",
            },
        )

    models = [m.get("name", "") for m in payload.get("models", []) if isinstance(m, dict)]
    base = classifier_model.split(":", 1)[0]
    loaded = any(m == classifier_model or m.startswith(f"{base}:") for m in models)
    detail = {
        "endpoint": classifier_endpoint,
        "configured_model": classifier_model,
        "loaded_models": models,
        "source": "fallback-ollama-tags",
    }
    if not loaded:
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=(
                f"configured classifier model not loaded: {classifier_model} "
                f"(endpoint reports {len(models)} model(s))"
            ),
            fix_hint=f"ollama pull {classifier_model}",
            detail=detail,
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary=f"{classifier_model} loaded on {classifier_endpoint}",
        fix_hint=None,
        detail=detail,
    )


# ---- Check 3: RAG corpus populated ----------------------------------------


def check_rag_corpus(*, rag_root: Path | None = None) -> CheckResult:
    """Wrap :func:`axiom.rag.health.collect_rag_health`.

    Empty corpora yield WARNING (not ERROR) — some nodes don't use RAG.
    Raises during import are caught by ``run_checks``; we let them
    propagate so they're reported uniformly.
    """
    try:
        from axiom.rag.health import collect_rag_health
    except ImportError as exc:
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary=f"RAG module unavailable: {exc}",
            fix_hint="pip install 'axiom[rag]'",
            detail={"import_error": str(exc)},
        )

    root = rag_root if rag_root is not None else (_project_root() / "runtime" / "rag")
    health = collect_rag_health(rag_root=root)
    detail = {
        "rag_root": str(root),
        "total_chunks": health.total_chunks,
        "corpora": [
            {"corpus_id": c.corpus_id, "chunk_count": c.chunk_count}
            for c in health.corpora
        ],
        "healthy": health.healthy,
    }

    if not health.corpora:
        return CheckResult(
            status=CheckStatus.WARNING,
            summary="no RAG corpora detected",
            fix_hint="axi rag ingest <document>  # to populate a corpus",
            detail=detail,
        )
    if not health.healthy:
        return CheckResult(
            status=CheckStatus.WARNING,
            summary=(
                f"{len(health.corpora)} corpus/corpora declared but all empty "
                f"(0 chunks)"
            ),
            fix_hint="axi rag ingest <document>  # to populate a corpus",
            detail=detail,
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary=(
            f"{len(health.corpora)} corpus/corpora populated; "
            f"{health.total_chunks:,} chunks total"
        ),
        fix_hint=None,
        detail=detail,
    )


# ---- Check 4: Extension manifests valid -----------------------------------


_REQUIRED_MANIFEST_FIELDS = ("name", "version")


def check_extension_manifests() -> CheckResult:
    """Walk installed extensions; verify each manifest parses + has required fields.

    SKIPPED when the discovery helper can't be imported (very early in
    install). ERROR if any discovered manifest fails to parse or omits
    required fields. OK reports the count.
    """
    try:
        from axiom.extensions.discovery import get_extension_dirs
    except ImportError as exc:
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary=f"extension discovery unavailable: {exc}",
            fix_hint=None,
            detail={"import_error": str(exc)},
        )

    try:
        from axiom.infra.toml_compat import load_toml
    except ImportError as exc:
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary=f"toml loader unavailable: {exc}",
            fix_hint=None,
            detail={"import_error": str(exc)},
        )

    dirs = get_extension_dirs()
    valid: list[str] = []
    invalid: list[dict[str, str]] = []
    for ext_dir in dirs:
        if not ext_dir.is_dir():
            continue
        for manifest in sorted(ext_dir.glob("**/axiom-extension.toml")):
            try:
                data = load_toml(manifest)
            except Exception as exc:  # noqa: BLE001
                invalid.append({"manifest": str(manifest), "error": str(exc)})
                continue
            section = data.get("extension", {})
            if not isinstance(section, dict):
                invalid.append(
                    {"manifest": str(manifest), "error": "missing [extension] table"}
                )
                continue
            missing = [f for f in _REQUIRED_MANIFEST_FIELDS if not section.get(f)]
            if missing:
                invalid.append(
                    {
                        "manifest": str(manifest),
                        "error": f"missing required field(s): {', '.join(missing)}",
                    }
                )
                continue
            valid.append(str(section.get("name", manifest.parent.name)))

    detail = {
        "valid_count": len(valid),
        "valid": valid,
        "invalid": invalid,
    }
    if invalid:
        sample = invalid[0]
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=(
                f"{len(invalid)} extension manifest(s) invalid; "
                f"first: {sample['manifest']} — {sample['error']}"
            ),
            fix_hint="axi ext lint  # to inspect failing manifests",
            detail=detail,
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary=f"{len(valid)} extension(s) valid",
        fix_hint=None,
        detail=detail,
    )


# ---- Check 5: Required Python deps ----------------------------------------


_REQUIRED_IMPORTS = (
    "axiom",
    "axiom.memory",
    "axiom.rag",
    "axiom.infra",
    "axiom.extensions",
)


def check_python_deps(*, required: Sequence[str] | None = None) -> CheckResult:
    """Verify each critical module imports cleanly."""
    import importlib

    targets = tuple(required) if required is not None else _REQUIRED_IMPORTS
    failed: list[dict[str, str]] = []
    for mod_name in targets:
        try:
            importlib.import_module(mod_name)
        except Exception as exc:  # noqa: BLE001 — surface every kind of failure
            failed.append({"module": mod_name, "error": f"{type(exc).__name__}: {exc}"})
    detail = {
        "checked": list(targets),
        "failed": failed,
    }
    if failed:
        sample = failed[0]
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=(
                f"{len(failed)} required module(s) failed to import; "
                f"first: {sample['module']} — {sample['error']}"
            ),
            fix_hint="pip install -e .  # or `pip install --upgrade axi-platform`",
            detail=detail,
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary=f"{len(targets)} required module(s) import cleanly",
        fix_hint=None,
        detail=detail,
    )


# ---- Check 6: Runtime config files present --------------------------------


_OPTIONAL_CONFIG_FILES = ("llm-providers.toml", "routing_allowlist.txt")


def check_runtime_config(*, config_dir: Path | None = None) -> CheckResult:
    """Verify the runtime/config dir exists; warn (don't error) when bare.

    Operators can run without explicit config (some checks read defaults
    from ~/.axi/), so missing files are a WARNING with a fix hint, not
    an ERROR.
    """
    cfg = config_dir if config_dir is not None else _runtime_config_dir()
    detail: dict[str, Any] = {
        "config_dir": str(cfg),
        "config_dir_exists": cfg.exists(),
        "files": {},
    }
    if not cfg.exists():
        return CheckResult(
            status=CheckStatus.WARNING,
            summary=f"runtime config dir absent: {cfg}",
            fix_hint=f"mkdir -p {cfg} && axi connect <preset>",
            detail=detail,
        )

    missing: list[str] = []
    for name in _OPTIONAL_CONFIG_FILES:
        path = cfg / name
        present = path.is_file()
        detail["files"][name] = present
        if not present:
            missing.append(name)
    if missing:
        return CheckResult(
            status=CheckStatus.WARNING,
            summary=f"optional config file(s) absent: {', '.join(missing)}",
            fix_hint="axi config  # to walk the onboarding wizard",
            detail=detail,
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary=f"runtime config dir present ({cfg})",
        fix_hint=None,
        detail=detail,
    )


# ---- Check 7: Memory store accessible -------------------------------------


def check_memory_store(*, data_root: Path | None = None) -> CheckResult:
    """Smoke-test ``build_memory_stack`` against a tmp scope.

    Should always work — if it doesn't, the install is broken in a way
    that will cascade into every downstream feature.
    """
    import tempfile

    try:
        from axiom.memory.bootstrap import build_memory_stack
    except ImportError as exc:
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"memory bootstrap module unavailable: {exc}",
            fix_hint="pip install -e .  # or `pip install --upgrade axi-platform`",
            detail={"import_error": str(exc)},
        )

    if data_root is None:
        tmp_ctx = tempfile.TemporaryDirectory(prefix="axiom-doctor-")
        root = Path(tmp_ctx.name)
    else:
        tmp_ctx = None
        root = data_root

    try:
        stack = build_memory_stack(scope_id="doctor", data_root=root)
        # Round-trip the registry — if this fails the stack is broken.
        list(stack.artifact_registry.list(kind="fragment"))
        detail = {"data_root": str(root), "scope_id": "doctor"}
        return CheckResult(
            status=CheckStatus.OK,
            summary="memory stack bootstraps + responds to list(kind='fragment')",
            fix_hint=None,
            detail=detail,
        )
    except Exception as exc:  # noqa: BLE001 — surface everything
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"memory stack failed to bootstrap: {type(exc).__name__}: {exc}",
            fix_hint="pip install -e .  # then re-run `axi doctor`",
            detail={
                "data_root": str(root),
                "exception": type(exc).__name__,
                "message": str(exc),
            },
        )
    finally:
        if tmp_ctx is not None:
            try:
                tmp_ctx.cleanup()
            except Exception:  # pragma: no cover — defensive
                pass


# ---- Check: axiom-memory MCP registered in user-scope Claude config -------


def check_axiom_memory_mcp_registered() -> CheckResult:
    """Detect whether axiom-memory MCP is registered in user-scope Claude config.

    Cross-tool memory only works if every Claude Code session reaches the
    axiom-memory MCP server, which requires a user-scope entry in
    ``~/.claude.json``. Missing or stale (wrong python path) entries
    silently break cross-session memory.

    Stale = registered, but the recorded ``command`` differs from the
    python that this doctor invocation is running under. Common cause:
    venv switch / repo move; fix is the same as missing.
    """
    try:
        from axiom.extensions.builtins.memory.register_mcp import (
            is_axiom_memory_mcp_registered,
        )
    except ImportError as exc:
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"register_mcp module unavailable: {exc}",
            fix_hint="pip install -e .  # then re-run `axi doctor`",
            detail={"import_error": str(exc)},
        )

    status = is_axiom_memory_mcp_registered(expected_command=sys.executable)

    if not status.get("registered"):
        reason = status.get("reason", "missing")
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=(
                f"axiom-memory MCP not registered in user-scope Claude config "
                f"({reason})"
            ),
            fix_hint="axi memory register-mcp",
            detail={
                "config_path": status.get("config_path", ""),
                "reason": reason,
            },
        )

    if status.get("stale"):
        return CheckResult(
            status=CheckStatus.WARNING,
            summary=(
                "axiom-memory MCP registered with a stale python path "
                f"(registered={status.get('command')}, expected={sys.executable})"
            ),
            fix_hint="axi memory register-mcp",
            detail={
                "config_path": status.get("config_path", ""),
                "registered_command": status.get("command", ""),
                "expected_command": sys.executable,
            },
        )

    return CheckResult(
        status=CheckStatus.OK,
        summary=f"axiom-memory MCP registered (command={status.get('command')})",
        fix_hint=None,
        detail={
            "config_path": status.get("config_path", ""),
            "command": status.get("command", ""),
        },
    )


# ---- Check: unified MCP server registered in an IDE ----------------------


def check_unified_mcp_installed() -> CheckResult:
    """Is the unified MCP server registered in at least one detected IDE/TUI?

    ``axi mcp install`` writes the server into every detected client. If
    clients are present but none carry the entry, the user's IDE agents can't
    reach the platform's MCP tools — fix is a one-liner.
    """
    try:
        from axiom.extensions.builtins.mcp.install import detect_tools, uninstall
    except ImportError as exc:
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"mcp install module unavailable: {exc}",
            fix_hint="pip install -e .  # then re-run `axi doctor`",
            detail={"import_error": str(exc)},
        )

    # A dry-run uninstall is a read-only probe: "would-remove" == registered.
    probe = uninstall(dry_run=True)["results"]
    registered = sorted(t for t, r in probe.items() if r["action"] == "would-remove")
    detected = sorted(t for t, present in detect_tools().items() if present)

    if registered:
        return CheckResult(
            status=CheckStatus.OK,
            summary=f"MCP server registered in: {', '.join(registered)}",
            fix_hint=None,
            detail={"registered": registered, "detected": detected},
        )
    if detected:
        return CheckResult(
            status=CheckStatus.WARNING,
            summary=(
                f"IDE(s) detected ({', '.join(detected)}) but the MCP server "
                "is not registered in any"
            ),
            fix_hint="axi mcp install",
            detail={"detected": detected},
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary="no MCP-capable IDEs detected (nothing to register)",
        fix_hint=None,
        detail={},
    )


# ---- Check: axiom-memory principal-pin reconciliation ---------------------


def check_axiom_memory_principal_reconciliation() -> CheckResult:
    """Detect drift between the pinned default principal and recent writes.

    When ``memory.default_principal`` is set, every CLI/MCP read defaults
    to it. If recent writes used a *different* principal, those writes
    will silently never surface in default reads — the user thinks
    "memory empty" while their fragments are landing under a sibling
    identity. Catches the user@example.org / personal@example.com
    drift class.
    """
    try:
        from axiom.extensions.builtins.memory.cli import (
            _build_default_composition,
        )
        from axiom.extensions.builtins.settings.store import SettingsStore
    except ImportError as exc:
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"settings/memory module unavailable: {exc}",
            fix_hint="pip install -e .  # then re-run `axi doctor`",
            detail={"import_error": str(exc)},
        )

    pinned = SettingsStore().get("memory.default_principal", "")
    if not pinned:
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary="no memory.default_principal pin set; nothing to reconcile",
            fix_hint=(
                "axi settings --global set memory.default_principal "
                "<your-principal-id>"
            ),
            detail={"pinned": ""},
        )

    try:
        composition = _build_default_composition()
        artifacts = list(
            composition.artifact_registry.list(kind="fragment")
        )
    except Exception as exc:  # noqa: BLE001 — surface everything
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"reconciliation probe failed: {type(exc).__name__}: {exc}",
            fix_hint=None,
            detail={"exception": type(exc).__name__, "message": str(exc)},
        )

    # Sort by provenance timestamp; sample the most recent N.
    def _ts(art):
        data = art.data or {}
        prov = data.get("provenance") or {}
        return prov.get("timestamp", "")

    artifacts.sort(key=_ts, reverse=True)
    sample = artifacts[:25]

    if not sample:
        return CheckResult(
            status=CheckStatus.OK,
            summary="no fragments yet; pin will apply to first writes",
            fix_hint=None,
            detail={"pinned": pinned, "sample_size": 0},
        )

    seen_principals: dict[str, int] = {}
    for art in sample:
        data = art.data or {}
        prov = data.get("provenance") or {}
        pid = prov.get("principal_id", "")
        seen_principals[pid] = seen_principals.get(pid, 0) + 1

    matched = seen_principals.get(pinned, 0)
    total = sum(seen_principals.values())
    drift = total - matched

    if drift == 0:
        return CheckResult(
            status=CheckStatus.OK,
            summary=(
                f"pin matches all {total} recent writes "
                f"(principal={pinned})"
            ),
            fix_hint=None,
            detail={
                "pinned": pinned,
                "sample_size": total,
                "matched": matched,
                "drift": 0,
                "seen_principals": seen_principals,
            },
        )

    other_principals = sorted(
        (k for k in seen_principals if k != pinned and k),
        key=lambda k: seen_principals[k],
        reverse=True,
    )
    return CheckResult(
        status=CheckStatus.WARNING,
        summary=(
            f"{drift}/{total} recent writes used a principal other than "
            f"the pin '{pinned}' — silent recall drift risk"
        ),
        fix_hint=(
            f"axi settings --global set memory.default_principal "
            f"{other_principals[0] if other_principals else '<correct-id>'}"
            "  # if the wrong one is pinned, repoint it; otherwise update "
            "callers to pass --principal explicitly"
        ),
        detail={
            "pinned": pinned,
            "sample_size": total,
            "matched": matched,
            "drift": drift,
            "seen_principals": seen_principals,
        },
    )


# ---- Check: axiom-memory heartbeat freshness ------------------------------


def check_axiom_memory_heartbeat_freshness() -> CheckResult:
    """Detect whether the memory ledger has a recent heartbeat fragment.

    The heartbeat is written on a fixed cadence (cron / launchd / systemd
    by default; manual `axi memory heartbeat` otherwise). Missing or
    stale heartbeats indicate a broken write path — silent dysfunction
    is the primary risk for cross-tool memory.

    Thresholds (matching `axiom.memory.session_capture` constants):
      <= 60 min     OK
      60–120 min    WARNING
      > 120 min     ERROR
      no heartbeat  ERROR
    """
    try:
        from axiom.extensions.builtins.memory.cli import (
            _build_default_composition,
        )
        from axiom.memory.session_capture import heartbeat_freshness
    except ImportError as exc:
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"memory module unavailable: {exc}",
            fix_hint="pip install -e .  # then re-run `axi doctor`",
            detail={"import_error": str(exc)},
        )

    try:
        composition = _build_default_composition()
        status = heartbeat_freshness(composition=composition)
    except ValueError as exc:
        # No principal pinned + caller didn't pass one. Surface as a
        # WARNING — heartbeat won't run on this node until the pin lands.
        return CheckResult(
            status=CheckStatus.WARNING,
            summary=f"heartbeat unscoped: {exc}",
            fix_hint=(
                "axi settings --global set memory.default_principal "
                "<your-principal-id>"
            ),
            detail={"reason": "no_default_principal"},
        )
    except Exception as exc:  # noqa: BLE001 — surface everything
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"heartbeat probe failed: {type(exc).__name__}: {exc}",
            fix_hint="axi memory heartbeat",
            detail={"exception": type(exc).__name__, "message": str(exc)},
        )

    state = status.get("state", "error")
    age = status.get("age_seconds")
    most_recent = status.get("most_recent_event_time", "")
    detail = {
        "state": state,
        "age_seconds": age,
        "most_recent_event_time": most_recent,
        "principal_id": status.get("principal_id", ""),
        "reason": status.get("reason", ""),
    }

    if state == "ok":
        return CheckResult(
            status=CheckStatus.OK,
            summary=(
                f"heartbeat fresh (age={int(age)}s)"
                if age is not None else "heartbeat ok"
            ),
            fix_hint=None,
            detail=detail,
        )
    if state == "warn":
        return CheckResult(
            status=CheckStatus.WARNING,
            summary=(
                f"heartbeat stale (age={int(age)}s, threshold=3600s)"
            ),
            fix_hint=(
                "axi memory heartbeat  # cron/launchd may have stalled; "
                "manual heartbeat covers the gap"
            ),
            detail=detail,
        )
    # error
    if status.get("reason") == "no_heartbeat":
        return CheckResult(
            status=CheckStatus.ERROR,
            summary="no heartbeat fragments found",
            fix_hint=(
                "axi memory heartbeat  # one-shot test that the write path works; "
                "then schedule via launchd/systemd for sustained coverage"
            ),
            detail=detail,
        )
    return CheckResult(
        status=CheckStatus.ERROR,
        summary=(
            f"heartbeat too stale (age={int(age) if age else 'unknown'}s, "
            "threshold=7200s)"
        ),
        fix_hint=(
            "axi memory heartbeat  # then check launchd/systemd cadence"
        ),
        detail=detail,
    )


def _probe(args: Sequence[str], *, timeout: float = 8.0) -> tuple[int, str]:
    """Run a read-only probe command; never raise.

    Returns ``(returncode, stdout)``. A missing binary, timeout, or any OS
    error maps to ``(127, "")`` so callers branch on emptiness instead of
    handling exceptions. Only ever used for *inspection* commands.
    """
    try:
        proc = subprocess.run(
            list(args), capture_output=True, text=True, timeout=timeout
        )
        return proc.returncode, proc.stdout
    except Exception:
        return 127, ""


# Scheduled Task "Last Result" codes that are NOT failures: success (0),
# currently running (0x41301=267009), and never-run-yet (0x41303=267011) —
# the last is normal for an ONLOGON task created before the next logon.
_WIN_BENIGN_LAST_RESULT = {"0", "267009", "267011"}


def _check_windows_tasks(
    *,
    task_prefix: str | None,
    lister: Callable[[], tuple[int, str]] | None,
) -> CheckResult:
    """Windows analogue of the systemd managed-service check.

    Enumerates Scheduled Tasks whose name carries the Axiom task prefix
    (``{product}_{service}`` — what ``WindowsTaskProvider`` registers) and
    flags any that are Disabled or whose last run returned a failure code.
    Task Scheduler is where a Windows-installed Axiom service lives, so this
    surfaces the same silent-outage class as the Linux path.
    """
    if task_prefix is None:
        try:
            from axiom.infra.branding import get_branding

            task_prefix = get_branding().product_name
        except Exception:
            task_prefix = "Axiom"
    if lister is not None:
        rc, out = lister()
    else:
        rc, out = _probe(["schtasks", "/Query", "/FO", "CSV", "/V"], timeout=15.0)
    if rc != 0 and not out.strip():
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary="schtasks unavailable — cannot enumerate scheduled tasks",
            detail={"returncode": rc},
        )

    idx: dict[str, int] = {}

    def _col(row: list[str], key: str) -> str:
        i = idx.get(key)
        return row[i].strip() if i is not None and i < len(row) else ""

    managed: list[str] = []
    failed: list[str] = []
    prefix_token = f"{task_prefix}_".lower()
    for row in csv.reader(out.splitlines()):
        if not row:
            continue
        stripped = [c.strip() for c in row]
        # /V emits the header row once per folder — re-latch columns each time
        # so we never depend on a fixed column order.
        if "TaskName" in stripped:
            idx = {name.lower(): i for i, name in enumerate(stripped)}
            continue
        if not idx:
            continue
        name = _col(row, "taskname")
        if not name:
            continue
        short = name.rsplit("\\", 1)[-1]  # strip the "\" root-folder prefix
        if not short.lower().startswith(prefix_token):
            continue
        managed.append(short)
        state = _col(row, "scheduled task state").lower()
        last = _col(row, "last result")
        if state == "disabled" or (last and last not in _WIN_BENIGN_LAST_RESULT):
            failed.append(short)
    if not managed:
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary=f"no managed scheduled tasks matching {task_prefix}_*",
            detail={"managed": []},
        )
    if failed:
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=(
                f"{len(failed)} of {len(managed)} managed task(s) unhealthy: "
                f"{', '.join(failed[:5])}"
            ),
            fix_hint=(
                f"schtasks /Query /TN {failed[0]} /V /FO LIST  "
                "# inspect Last Result / state before re-running"
            ),
            detail={"managed": managed, "failed": failed},
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary=f"{len(managed)} managed task(s) healthy",
        detail={"managed": managed, "failed": []},
    )


def check_managed_services(
    *,
    unit_glob: str = "neut-*",
    task_prefix: str | None = None,
    lister: Callable[[], tuple[int, str]] | None = None,
) -> CheckResult:
    """Verify every Axiom-managed OS service is healthy.

    The reliability contract is that a managed service is registered and *not
    failed*. On Linux that is a systemd user unit stuck in ``failed`` — e.g.
    an ExecStart that resolves to nothing (status=203/EXEC). On Windows it is
    a Scheduled Task that is Disabled or whose last run returned a failure
    code. Both are the silent-outage class this check exists to surface.
    Hosts with neither manager SKIP.
    """
    system = platform.system()
    if system == "Windows":
        return _check_windows_tasks(task_prefix=task_prefix, lister=lister)
    if system != "Linux" or shutil.which("systemctl") is None:
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary="not a systemd/Task Scheduler host — no managed services to check",
            detail={"platform": system},
        )
    if lister is not None:
        rc, out = lister()
    else:
        rc, out = _probe(
            ["systemctl", "--user", "list-units", unit_glob,
             "--all", "--no-legend", "--plain"]
        )
    if rc != 0 and not out.strip():
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary="systemd user manager unavailable (no session bus / linger?)",
            fix_hint="loginctl enable-linger $USER  # for a durable user manager",
            detail={"returncode": rc},
        )
    managed: list[str] = []
    failed: list[str] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        unit, active, sub = parts[0], parts[2], parts[3]
        managed.append(unit)
        if active == "failed" or sub == "failed":
            failed.append(unit)
    if not managed:
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary=f"no managed services matching {unit_glob!r}",
            detail={"managed": []},
        )
    if failed:
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=(
                f"{len(failed)} of {len(managed)} managed service(s) failed: "
                f"{', '.join(failed[:5])}"
            ),
            fix_hint=(
                f"systemctl --user status {failed[0]}  "
                "# inspect ExecStart / logs before restarting"
            ),
            detail={"managed": managed, "failed": failed},
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary=f"{len(managed)} managed service(s) healthy",
        detail={"managed": managed, "failed": []},
    )


def _find_nvidia_smi() -> str | None:
    """Locate ``nvidia-smi`` across platforms, or ``None`` if absent.

    ``shutil.which`` covers Linux and the common Windows case (the driver
    drops ``nvidia-smi.exe`` in System32, which is always on PATH, and
    ``which`` honours PATHEXT). The legacy NVSMI directory under Program
    Files is *not* on PATH, so probe known Windows locations as a fallback.
    """
    found = shutil.which("nvidia-smi")
    if found:
        return found
    if platform.system() == "Windows":
        # Windows env-var lookups are case-insensitive, so the canonical
        # mixed-case names resolve via these upper-case keys just the same.
        env = os.environ
        candidates = (
            Path(env.get("SYSTEMROOT", r"C:\Windows"))
            / "System32" / "nvidia-smi.exe",
            Path(env.get("PROGRAMFILES", r"C:\Program Files"))
            / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe",
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return None


def check_gpu_contention(
    *,
    query: Callable[[], tuple[int, str]] | None = None,
    apps: Callable[[], tuple[int, str]] | None = None,
) -> CheckResult:
    """Flag accelerator-memory pressure that can starve co-located workloads.

    A single accelerator shared by (say) a benchmark engine and a
    production model is a classic silent outage: one grabs the memory, the
    other cannot load, and every request times out. No NVIDIA GPU -> SKIP.
    """
    smi = _find_nvidia_smi()
    if smi is None:
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary="no NVIDIA GPU detected (nvidia-smi absent)",
        )
    if query is not None:
        _, mem_out = query()
    else:
        _, mem_out = _probe(
            [smi, "--query-gpu=memory.used,memory.total",
             "--format=csv,noheader,nounits"]
        )
    first = mem_out.strip().splitlines()[0] if mem_out.strip() else ""
    try:
        used_s, total_s = (x.strip() for x in first.split(","))
        used, total = int(float(used_s)), int(float(total_s))
    except Exception:
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary="nvidia-smi present but returned no parseable memory line",
            detail={"raw": first},
        )
    if apps is not None:
        _, apps_out = apps()
    else:
        _, apps_out = _probe(
            [smi, "--query-compute-apps=used_memory,process_name",
             "--format=csv,noheader,nounits"]
        )
    procs = [ln.strip() for ln in apps_out.splitlines() if ln.strip()]
    frac = used / total if total else 0.0
    detail = {
        "used_mib": used, "total_mib": total,
        "used_fraction": round(frac, 3), "compute_procs": procs,
    }
    if frac >= 0.9:
        return CheckResult(
            status=CheckStatus.WARNING,
            summary=(
                f"GPU memory {used}/{total} MiB ({frac:.0%}) near exhaustion; "
                f"{len(procs)} compute process(es) contending"
            ),
            fix_hint=(
                "nvidia-smi  # is a benchmark/experiment service holding memory "
                "a production workload needs? disable it if so"
            ),
            detail=detail,
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary=(
            f"GPU memory {used}/{total} MiB ({frac:.0%}); "
            f"{len(procs)} compute process(es)"
        ),
        detail=detail,
    )


def check_package_coherence(
    *,
    auditor: Callable[[], tuple[int, str]] | None = None,
    preferences_dir: Path | None = None,
    hazards: HostHazardProvider | None = None,
) -> CheckResult:
    """Detect an incoherent apt/dpkg state + catch-all repo pins.

    Two failure modes from the postmortem:

    1. Half-installed packages (``dpkg --audit`` non-empty) — the state in
       which a *blind* ``apt -f install`` can cascade into removing
       unrelated packages. We report it; we never run the fix.
    2. A ``Package: *`` pin at priority > 500 pointing at a non-distro
       repo, which makes routine upgrades prefer that repo's build of
       *everything* — the third-party-driver-over-distro trap.

    When a ``hazards`` provider is supplied (ADR-089), a detected catch-all
    pin is enriched with the host-specific consequence + tested remediation
    recorded for *this* host — escalating the generic warning to a concrete
    one. The lookup is deterministic and fail-safe: an unavailable registry
    falls back to the generic message and says so, never a false "healthy".

    Non-apt hosts SKIP.
    """
    if shutil.which("dpkg") is None:
        return CheckResult(status=CheckStatus.SKIPPED, summary="not a dpkg/apt host")
    if auditor is not None:
        _, audit_out = auditor()
    else:
        _, audit_out = _probe(["dpkg", "--audit"])
    broken = [ln for ln in audit_out.splitlines() if ln.strip()]

    pin_hits: list[str] = []
    pref_files: list[Path] = []
    if preferences_dir is not None:
        pref_dir = preferences_dir
    else:
        pref_dir = Path("/etc/apt/preferences.d")
        root_pref = Path("/etc/apt/preferences")
        if root_pref.exists():
            pref_files.append(root_pref)
    try:
        if pref_dir.is_dir():
            pref_files.extend(sorted(pref_dir.glob("*")))
    except Exception:
        pass
    for pf in pref_files:
        try:
            text = pf.read_text(errors="replace")
        except Exception:
            continue
        pkg_is_star = False
        for raw in text.splitlines():
            line = raw.strip()
            low = line.lower()
            if low.startswith("package:"):
                pkg_is_star = line.split(":", 1)[1].strip() == "*"
            elif low.startswith("pin-priority:") and pkg_is_star:
                try:
                    prio = int(line.split(":", 1)[1].strip())
                except ValueError:
                    continue
                if prio > 500:
                    pin_hits.append(f"{pf.name}: Package:* @ {prio}")

    detail: dict[str, Any] = {"broken_packages": broken[:20], "catch_all_pins": pin_hits}
    if broken or pin_hits:
        parts = []
        if broken:
            parts.append(f"{len(broken)} half-installed package line(s)")
        if pin_hits:
            parts.append(f"{len(pin_hits)} catch-all repo pin(s) above the distro")
        summary = "; ".join(parts) + " — routine upgrades / fix-broken are unsafe here"
        fix_hint = (
            "inspect: dpkg --audit; apt-cache policy <pkg>; review "
            "/etc/apt/preferences.d/. Scope any 'Package: *' pin to the "
            "intended packages first. Do NOT run 'apt -f install' blindly — "
            "it can remove more than it fixes."
        )
        # ADR-089: enrich a catch-all pin with this host's recorded hazard.
        if pin_hits:
            from axiom.infra.host_hazards import (
                SIG_APT_CATCH_ALL_PIN,
                lookup_active_hazard,
            )

            hz, enrichment_available = lookup_active_hazard(
                hazards, SIG_APT_CATCH_ALL_PIN
            )
            detail["host_enrichment_available"] = enrichment_available
            if hz is not None:
                detail["host_hazard"] = {
                    "host_id": hz.host_id,
                    "signature": hz.signature,
                    "consequence": hz.consequence,
                    "remediation": hz.remediation,
                    "recorded_at": hz.recorded_at,
                }
                summary = f"on this host: {hz.consequence} — {summary}"
                fix_hint = f"{hz.remediation}  [tested fix on {hz.host_id}]. " + fix_hint
        return CheckResult(
            status=CheckStatus.WARNING,
            summary=summary,
            fix_hint=fix_hint,
            detail=detail,
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary="dpkg state coherent; no catch-all repo pins above the distro",
        detail=detail,
    )


def check_herald_channels(*, env: dict[str, str] | None = None) -> CheckResult:
    """Declared HERALD channels are actually registered.

    Channel rehydration is env-only and fail-closed
    (``notifications.channel_config.rehydrate_from_env``): a channel whose
    ``AXIOM_HERALD_*`` vars are unset simply does not register, and every
    send silently falls back to the inbox. A webhook exported in a shell
    session therefore evaporates with the session and the alert path goes
    dark with no error anywhere. Sites declare their intended channels in
    ``AXIOM_HERALD_EXPECTED_CHANNELS`` (comma-separated, e.g.
    ``inbox,teams``); this check turns a silently-missing channel into a
    loud doctor failure.
    """
    from axiom.extensions.builtins.notifications.channel_config import (
        resolved_config,
    )

    environ: dict[str, str] = resolved_config(env)
    declared_raw = (environ.get("AXIOM_HERALD_EXPECTED_CHANNELS") or "").strip()
    if not declared_raw:
        return CheckResult(
            status=CheckStatus.SKIPPED,
            summary=(
                "no channels declared — run `axi notifications setup <channel>` "
                "or set AXIOM_HERALD_EXPECTED_CHANNELS to assert the alert path"
            ),
        )
    declared = sorted({c.strip() for c in declared_raw.split(",") if c.strip()})

    try:
        from axiom.extensions.builtins.notifications.send import SendContext

        ctx = SendContext.default(env=environ)
        registered = sorted(p.name for p in ctx.registry.all())
    except Exception as exc:  # noqa: BLE001 — a broken import is itself the finding
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=f"could not build the notification context: {exc}",
            fix_hint="pip install axiom-os-lm[herald]  # or check the notifications extension",
        )

    missing = sorted(set(declared) - set(registered))
    detail = {"declared": declared, "registered": registered, "missing": missing}
    if missing:
        vars_hint = ", ".join(
            f"AXIOM_HERALD_{name.upper()}_WEBHOOK_URL" for name in missing
        )
        return CheckResult(
            status=CheckStatus.ERROR,
            summary=(
                f"declared channel(s) not registered: {', '.join(missing)} — "
                "sends are silently falling back to the inbox"
            ),
            fix_hint=(
                f"set {vars_hint} in the node's durable env "
                "(e.g. ~/.config/environment.d/50-axiom-herald.conf), run "
                "`systemctl --user daemon-reexec`, verify with "
                "`axi notifications channels`"
            ),
            detail=detail,
        )
    return CheckResult(
        status=CheckStatus.OK,
        summary=f"all declared channels registered: {', '.join(declared)}",
        detail=detail,
    )


# ---------------------------------------------------------------------------
# Default check set
# ---------------------------------------------------------------------------


def default_checks() -> tuple[Check, ...]:
    """The built-in check set.

    Extensions register additional checks via discovery in a future
    iteration; this function defines the core seven that ship with
    every install.
    """
    return (
        Check(
            name="LLM endpoints reachable",
            category="routing",
            fn=check_llm_endpoints,
        ),
        Check(
            name="Routing classifier model loaded",
            category="routing",
            fn=check_classifier_model,
        ),
        Check(
            name="RAG corpus populated",
            category="rag",
            fn=check_rag_corpus,
        ),
        Check(
            name="Extension manifests valid",
            category="extensions",
            fn=check_extension_manifests,
        ),
        Check(
            name="Required Python deps",
            category="environment",
            fn=check_python_deps,
        ),
        Check(
            name="Runtime config files present",
            category="environment",
            fn=check_runtime_config,
        ),
        Check(
            name="Memory store accessible",
            category="memory",
            fn=check_memory_store,
        ),
        Check(
            name="axiom-memory MCP registered (user-scope)",
            category="memory",
            fn=check_axiom_memory_mcp_registered,
        ),
        Check(
            name="MCP server registered in an IDE",
            category="mcp",
            fn=check_unified_mcp_installed,
        ),
        Check(
            name="axiom-memory heartbeat freshness",
            category="memory",
            fn=check_axiom_memory_heartbeat_freshness,
        ),
        Check(
            name="axiom-memory principal-pin reconciliation",
            category="memory",
            fn=check_axiom_memory_principal_reconciliation,
        ),
        Check(
            name="Managed services healthy",
            category="services",
            fn=check_managed_services,
        ),
        Check(
            name="GPU memory contention",
            category="resources",
            fn=check_gpu_contention,
        ),
        Check(
            name="Package/repo coherence",
            category="environment",
            fn=check_package_coherence,
        ),
        Check(
            name="HERALD declared channels registered",
            category="notifications",
            fn=check_herald_channels,
        ),
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


_STATUS_ICON = {
    CheckStatus.OK: "[ok]  ",
    CheckStatus.WARNING: "[warn]",
    CheckStatus.ERROR: "[fail]",
    CheckStatus.SKIPPED: "[skip]",
}


def render_text(
    pairs: Sequence[tuple[Check, CheckResult]],
    *,
    out=sys.stdout,
) -> None:
    """Print a grouped human-readable report to ``out``.

    Builds ``rich.Console`` lazily inside this call so capsys can
    capture the output during tests; falls back to plain ``print`` if
    rich isn't importable.
    """
    try:  # pragma: no cover — import guard
        from rich.console import Console

        console = Console(file=out, force_terminal=False, highlight=False)
        printer = console.print
    except Exception:  # pragma: no cover — fall back to stdlib

        def printer(msg: str = "") -> None:
            print(msg, file=out)

    rule = "-" * 65
    printer("Axiom Doctor — Node Health Diagnostic")
    printer(rule)

    grouped: dict[str, list[tuple[Check, CheckResult]]] = {}
    for check, result in pairs:
        grouped.setdefault(check.category, []).append((check, result))

    for category in sorted(grouped):
        printer("")
        printer(category.capitalize())
        for check, result in grouped[category]:
            icon = _STATUS_ICON.get(result.status, "[??]  ")
            printer(f"  {icon} {check.name}")
            if result.status is not CheckStatus.OK:
                printer(f"       {result.summary}")
                if result.fix_hint:
                    printer(f"       fix:   {result.fix_hint}")
            else:
                printer(f"       {result.summary}")

    counts = {s: 0 for s in CheckStatus}
    for _, result in pairs:
        counts[result.status] += 1
    printer("")
    printer(rule)
    printer(
        f"Summary: {counts[CheckStatus.ERROR]} error, "
        f"{counts[CheckStatus.WARNING]} warning, "
        f"{counts[CheckStatus.OK]} ok, "
        f"{counts[CheckStatus.SKIPPED]} skipped"
    )
    printer(f"Exit code: {counts[CheckStatus.ERROR]}")


def render_json(
    pairs: Sequence[tuple[Check, CheckResult]],
    *,
    out=sys.stdout,
) -> None:
    """Emit a machine-readable JSON document for scripts / CI."""
    payload = {
        "schema": "axiom.doctor.v1",
        "checks": [
            {
                "name": check.name,
                "category": check.category,
                "status": result.status.value,
                "summary": result.summary,
                "fix_hint": result.fix_hint,
                "detail": result.detail,
            }
            for check, result in pairs
        ],
        "summary": _summary_counts(pairs),
    }
    print(json.dumps(payload, indent=2, sort_keys=True), file=out)


def _summary_counts(pairs: Sequence[tuple[Check, CheckResult]]) -> dict[str, int]:
    counts = {s.value: 0 for s in CheckStatus}
    for _, result in pairs:
        counts[result.status.value] += 1
    counts["total"] = len(pairs)
    return counts


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


@dataclass
class DoctorCliDeps:
    """Injectable dependencies for the doctor CLI (testing seam)."""

    checks: tuple[Check, ...] = field(default_factory=default_checks)


def cmd_run(*, argv: Sequence[str], deps: DoctorCliDeps) -> int:
    """Run the full check set (or a single ``--check``); return error count."""
    parser = argparse.ArgumentParser(prog="axi doctor")
    parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Emit machine-readable JSON instead of grouped text.",
    )
    parser.add_argument(
        "--check",
        default=None,
        help="Run only the named check (use `axi doctor list` to see names).",
    )
    args = parser.parse_args(list(argv))

    selected: tuple[Check, ...]
    if args.check is not None:
        matched = tuple(c for c in deps.checks if c.name == args.check)
        if not matched:
            available = ", ".join(c.name for c in deps.checks)
            print(
                f"axi doctor: unknown check: {args.check}",
                file=sys.stderr,
            )
            print(f"available: {available}", file=sys.stderr)
            return 2
        selected = matched
    else:
        selected = deps.checks

    results = run_checks(selected)
    pairs = list(zip(selected, results, strict=True))

    if args.emit_json:
        render_json(pairs)
    else:
        render_text(pairs)

    error_count = sum(1 for r in results if r.status is CheckStatus.ERROR)
    return error_count


def cmd_list(*, argv: Sequence[str], deps: DoctorCliDeps) -> int:
    """List all registered checks (debugging / discovery)."""
    parser = argparse.ArgumentParser(prog="axi doctor list")
    parser.parse_args(list(argv))

    print("Registered checks:")
    print()
    by_cat: dict[str, list[Check]] = {}
    for check in deps.checks:
        by_cat.setdefault(check.category, []).append(check)
    for category in sorted(by_cat):
        print(f"  {category}")
        for check in by_cat[category]:
            print(f"    - {check.name}")
        print()
    print("Run a single check with:  axi doctor --check <name>")
    return 0


# ---------------------------------------------------------------------------
# Top-level dispatcher
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Top-level parser used for argcomplete + ``--help``."""
    parser = argparse.ArgumentParser(
        prog="axi doctor",
        description=(
            "Proactive node-health diagnostic — runs after install / upgrade "
            "to catch silent dependency issues before they surface as failed "
            "requests."
        ),
    )
    sub = parser.add_subparsers(dest="action")
    run_p = sub.add_parser(
        "run",
        help="Run all checks (default action when no subcommand given).",
    )
    run_p.add_argument("--json", action="store_true", dest="emit_json")
    run_p.add_argument("--check", default=None)
    sub.add_parser("list", help="List all registered checks.")
    # Top-level flags too, so `axi doctor --json` and `axi doctor --check X` work.
    parser.add_argument("--json", action="store_true", dest="emit_json")
    parser.add_argument("--check", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    argv = list(argv)

    deps = DoctorCliDeps()

    if argv and argv[0] in ("-h", "--help"):
        build_parser().print_help()
        return 0

    if argv and argv[0] == "list":
        return cmd_list(argv=argv[1:], deps=deps)

    if argv and argv[0] == "run":
        return cmd_run(argv=argv[1:], deps=deps)

    return cmd_run(argv=argv, deps=deps)


if __name__ == "__main__":
    sys.exit(main())
