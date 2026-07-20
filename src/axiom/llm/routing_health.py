# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Domain-agnostic routing-classifier health helper.

Surfaces health for the Stage-2 routing classifier (the local SLM the
QueryRouter delegates to via :class:`OllamaClassifier`) so operator
surfaces — notably ``axi config --status`` — can answer "is the
endpoint reachable AND is the configured model actually pulled?"

Operators today see only the LLM endpoint reachability for the cloud
gateway; they don't see whether the small local classifier model is
on disk.  That gap caused over-blocking on a real host: the endpoint was
green but the configured ``llama3.2:1b`` had never been pulled, so
the SLM stage silently degraded to fallback for every request.

Tolerance is the headline guarantee: a missing endpoint, an unreachable
daemon, or a TimeoutError MUST yield a well-formed
:class:`ClassifierHealth` rather than raise.  Operator surfaces should
always be able to call this without a ``try/except`` wrapper.

Per ``feedback_rich_console_lazy_construction``: any ``rich.Console``
is constructed inside :func:`render_classifier_health` (not at module
import) so capsys can capture the output in tests.

Per axiom-domain-agnostic rule: rendered output names only the
configured endpoint + model — no domain-consumer terminology (no domain/site/host-name leakage).
"""

from __future__ import annotations

import json
import logging
import statistics
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Built-in defaults — mirror src/axiom/infra/router.py so a missing
# SettingsStore still yields a meaningful health report.
_DEFAULT_ENDPOINT = "http://localhost:11434"
_DEFAULT_MODEL = "llama3.2:1b"
_DEFAULT_PROBE_TIMEOUT_S = 1.0


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifierHealth:
    """Health report for the routing Stage-2 classifier (Ollama-based SLM).

    Fields surface as ``None`` when the underlying datum cannot be derived
    — operator surfaces should treat ``None`` as "unknown" rather than
    "zero".
    """

    endpoint: str
    endpoint_reachable: bool
    configured_model: str
    model_loaded: bool
    model_loaded_check_error: str | None
    last_classification_at: str | None
    p50_latency_ms: int | None


# ProbeRunner signature: take a URL + timeout and return either the parsed
# JSON body, ``None`` (unreachable / non-200), or raise.  Raising is a
# normal outcome — the helper catches everything to keep the surface
# tolerant.
ProbeRunner = Callable[..., "dict[str, Any] | None"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def collect_classifier_health(
    *,
    endpoint: str | None = None,
    model: str | None = None,
    probe_timeout_s: float = _DEFAULT_PROBE_TIMEOUT_S,
    runner: ProbeRunner | None = None,
) -> ClassifierHealth:
    """Probe the Stage-2 classifier endpoint + check the configured model.

    Parameters
    ----------
    endpoint:
        Ollama base URL (e.g., ``http://localhost:11434``).  When
        ``None``, reads ``routing.ollama_base`` from settings, then falls
        back to the built-in default.
    model:
        Configured classifier model (e.g., ``llama3.2:1b``).  When
        ``None``, reads ``routing.ollama_model`` from settings, then
        falls back to the built-in default.
    probe_timeout_s:
        Per-call HTTP timeout.  Defaults to 1s — operator surfaces should
        not block on a hung daemon.
    runner:
        Injectable probe callable for tests.  Default is a urllib-based
        runner that fetches ``/api/tags`` and parses the response.

    Returns
    -------
    ClassifierHealth
        Always a well-formed value.  Never raises — anything from a
        missing daemon to a corrupt audit file yields the populated
        shape with explanatory ``model_loaded_check_error``.
    """
    settings = _safe_read_settings()
    resolved_endpoint = (
        endpoint
        if endpoint is not None
        else settings.get("routing.ollama_base", _DEFAULT_ENDPOINT)
    )
    resolved_model = (
        model
        if model is not None
        else settings.get("routing.ollama_model", _DEFAULT_MODEL)
    )
    probe = runner if runner is not None else _default_runner

    # 1. Probe the endpoint.
    tags_url = resolved_endpoint.rstrip("/") + "/api/tags"
    endpoint_reachable = False
    model_loaded = False
    model_loaded_check_error: str | None = None
    tags_payload: dict | None = None

    try:
        tags_payload = probe(tags_url, timeout=probe_timeout_s)
    except TimeoutError as exc:
        model_loaded_check_error = (
            f"endpoint timeout after {probe_timeout_s:.1f}s: {exc}"
        )
    except (urllib.error.URLError, OSError) as exc:
        model_loaded_check_error = f"endpoint unreachable: {exc}"
    except Exception as exc:  # noqa: BLE001 — tolerance is the contract
        model_loaded_check_error = f"endpoint check failed: {exc}"

    if tags_payload is not None:
        endpoint_reachable = True
        # 2. Look for the configured model in the /api/tags response.
        names = _tag_names(tags_payload)
        if _model_present(resolved_model, names):
            model_loaded = True
            model_loaded_check_error = None
        else:
            model_loaded = False
            model_loaded_check_error = (
                f"model '{resolved_model}' not found in /api/tags "
                f"({len(names)} model(s) available)"
            )
    elif model_loaded_check_error is None:
        # probe returned None without raising — endpoint declined the
        # request or is not running.
        model_loaded_check_error = (
            "endpoint unreachable: could not contact /api/tags"
        )

    # 3. Compute recent classification metrics from the audit log.
    last_at, p50_ms = _read_recent_classification_metrics()

    return ClassifierHealth(
        endpoint=resolved_endpoint,
        endpoint_reachable=endpoint_reachable,
        configured_model=resolved_model,
        model_loaded=model_loaded,
        model_loaded_check_error=model_loaded_check_error,
        last_classification_at=last_at,
        p50_latency_ms=p50_ms,
    )


def render_classifier_health(
    health: ClassifierHealth, console: Any | None = None
) -> None:
    """Print a human-friendly Routing Classifier section to stdout.

    Constructs ``rich.Console`` lazily so capsys captures the output in
    tests.  Falls back to plain ``print`` when rich isn't importable.
    The actionable ``ollama pull <model>`` hint surfaces only when the
    endpoint is reachable but the model isn't loaded — pulling a model
    is the wrong action when the daemon itself is down.
    """
    if console is not None:
        printer = console.print
    else:
        try:  # pragma: no cover — import guard
            from rich.console import Console

            printer = Console(force_terminal=False, highlight=False).print
        except Exception:  # pragma: no cover — fall back to stdlib

            def printer(msg: str = "") -> None:
                print(msg)

    check_mark = "✓"
    cross_mark = "✗"

    printer("")
    printer("Routing Classifier")
    printer(f"  endpoint:          {health.endpoint}")
    printer(
        f"  reachable:         "
        f"{check_mark if health.endpoint_reachable else cross_mark}"
    )
    printer(f"  configured model:  {health.configured_model}")

    # The actionable hint is the headline ask for this feature.  Only
    # show it when the endpoint is up but the model is missing — that's
    # the case ``ollama pull X`` actually fixes.
    if health.model_loaded:
        printer(f"  model loaded:      {check_mark}")
    elif health.endpoint_reachable:
        printer(
            f"  model loaded:      {cross_mark} "
            f"(run: ollama pull {health.configured_model})"
        )
    else:
        printer(f"  model loaded:      {cross_mark} (endpoint unreachable)")

    if health.last_classification_at:
        printer(f"  last classify:     {health.last_classification_at}")
    else:
        printer("  last classify:     never")

    if health.p50_latency_ms is not None:
        printer(f"  p50 latency:       {health.p50_latency_ms} ms")
    else:
        printer("  p50 latency:       n/a")


# ---------------------------------------------------------------------------
# Internals — settings + audit lookup (test-injectable)
# ---------------------------------------------------------------------------


def _read_settings() -> dict[str, Any]:
    """Read routing-related settings.

    Split from ``_safe_read_settings`` so tests can monkeypatch a single
    seam.  Raises if the SettingsStore can't be constructed; the caller
    handles that.
    """
    from axiom.extensions.builtins.settings.store import SettingsStore

    store = SettingsStore()
    return {
        "routing.ollama_base": store.get(
            "routing.ollama_base", _DEFAULT_ENDPOINT
        ),
        "routing.ollama_model": store.get(
            "routing.ollama_model", _DEFAULT_MODEL
        ),
    }


def _safe_read_settings() -> dict[str, Any]:
    """Read settings tolerantly — never raises.  Always returns a dict."""
    try:
        return _read_settings()
    except Exception as exc:  # noqa: BLE001 — tolerance contract
        log.debug("collect_classifier_health: settings read failed: %s", exc)
        return {}


def _audit_log_dir() -> Path:
    """Return the audit log directory.  Test-injectable seam."""
    from axiom import REPO_ROOT

    return REPO_ROOT / "runtime" / "logs" / "audit"


def _read_recent_classification_metrics() -> tuple[str | None, int | None]:
    """Read recent classification audit events, returning (last_ts, p50_ms).

    Returns ``(None, None)`` whenever the metrics aren't available — no
    audit dir, no classification_events file, or the file is empty /
    corrupt.  Tolerant: never raises.

    The audit-log shape is set by :meth:`AuditLog.write_classification`
    (jsonl with ``ts`` + ``classifier`` keys).  ``latency_ms`` is not
    written by the current schema; we surface it when present and
    ``None`` otherwise.  Item 1 of the prior-incident lessons may add it
    upstream; this module adapts when that lands without a schema
    change here.
    """
    try:
        log_dir = _audit_log_dir()
    except Exception as exc:  # noqa: BLE001
        log.debug("classification audit dir lookup failed: %s", exc)
        return (None, None)

    events_path = log_dir / "classification_events.jsonl"
    if not events_path.is_file():
        return (None, None)

    try:
        text = events_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.debug("classification audit read failed: %s", exc)
        return (None, None)

    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            # Corrupt row — skip but don't crash.
            continue
        if isinstance(row, dict):
            rows.append(row)

    if not rows:
        return (None, None)

    last_at = _last_timestamp(rows)
    p50 = _p50_latency_ms(rows)
    return (last_at, p50)


def _last_timestamp(rows: list[dict]) -> str | None:
    """Return the most recent ``ts`` across audit rows, or None."""
    timestamps = [r.get("ts") for r in rows if isinstance(r.get("ts"), str)]
    if not timestamps:
        return None
    # Lexicographic sort is correct for ISO-8601-with-offset strings of
    # the same shape (which is what AuditLog._now() emits).
    return max(timestamps)


def _p50_latency_ms(rows: list[dict]) -> int | None:
    """Return the median latency_ms across recent rows, or None.

    Considers only the most recent 100 rows so the metric reflects
    current behaviour, not historical noise.
    """
    latencies: list[float] = []
    for row in rows[-100:]:
        v = row.get("latency_ms")
        if v is None:
            continue
        try:
            latencies.append(float(v))
        except (TypeError, ValueError):
            continue
    if not latencies:
        return None
    return int(round(statistics.median(latencies)))


# ---------------------------------------------------------------------------
# Default probe runner
# ---------------------------------------------------------------------------


def _default_runner(url: str, *, timeout: float) -> dict | None:
    """Fetch ``url`` and parse the JSON body.  Returns None on non-200."""
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            body = resp.read()
    except urllib.error.HTTPError:
        return None
    return json.loads(body)


def _tag_names(payload: dict) -> list[str]:
    """Extract model names from an Ollama /api/tags payload, tolerantly."""
    if not isinstance(payload, dict):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    names: list[str] = []
    for entry in models:
        if isinstance(entry, dict):
            name = entry.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def _model_present(configured: str, names: list[str]) -> bool:
    """Return True if ``configured`` is satisfied by any name in ``names``.

    Accepts:
      - exact match (``llama3.2:1b`` == ``llama3.2:1b``)
      - prefix match on the base+tag pair (``llama3.2:1b`` matches
        ``llama3.2:1b-instruct-q4_0`` because Ollama may report tagged
        variants).
    """
    if not configured or not names:
        return False
    target = configured.strip()
    for name in names:
        if name == target:
            return True
        if name.startswith(target + "-") or name.startswith(target + "."):
            return True
    return False
