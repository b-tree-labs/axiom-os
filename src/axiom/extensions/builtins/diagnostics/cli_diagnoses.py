# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""TRIAGE CLI failure diagnosis catalog.

When a CLI command fails and `emit_cli_error` publishes a `cli.arg_error`
event, TRIAGE's listener (see `cli_listener.py`) calls `match_failure`
to find a pattern in this catalog. A match becomes a pending diagnosis
that the next CLI invocation surfaces to the user before dispatch.

Closes the loop surfaced 2026-05-03: when `axi chat` failed because
the local config still pointed at the deprecated Bonsai LLM, TRIAGE
had no listener and no pattern, so the user only saw the raw error.
This module's first entry encodes that exact case + remedy.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Diagnosis:
    """The matched diagnosis a pattern produces.

    `fingerprint` is intentionally derived from `pattern_id` only (not the
    underlying error text), so the same diagnosis dedupes across cosmetic
    variations of the same root cause.
    """

    pattern_id: str
    summary: str
    remedy: str
    confidence: float
    fingerprint: str = field(init=False)
    matched_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        # Frozen dataclass: bypass setattr for derived field.
        digest = hashlib.sha1(self.pattern_id.encode("utf-8")).hexdigest()[:12]
        object.__setattr__(self, "fingerprint", digest)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Pattern:
    """One entry in the catalog: id + matcher + diagnosis template.

    The matcher returns a Diagnosis on hit, None on miss. The matcher
    itself owns the confidence scoring — different patterns can match
    on different signals (error_type only, message keywords,
    traceback frame, etc.) and self-rate.
    """

    pattern_id: str
    matcher: Callable[[dict], Diagnosis | None]


# ---------------------------------------------------------------------------
# Pattern: Bonsai LLM deprecated (originating case, 2026-05-03)
# ---------------------------------------------------------------------------

_BONSAI_REMEDY = (
    "The bonsai-1.7b LLM is being phased out. Edit "
    "`runtime/config/llm-providers.toml`: replace the provider whose name "
    "starts with `bonsai-` with one named `qwen-local`, model "
    "`qwen2.5-7b-instruct`, endpoint `http://localhost:8080/v1`. Then run "
    "`axi setup model qwen` to download the GGUF if not already present. "
    "Track: https://github.com/b-tree-labs/axiom-os (Bonsai → Qwen swap)."
)


def _bonsai_matcher(event: dict) -> Diagnosis | None:
    """Match any failure that mentions the bonsai gguf or model id.

    The user's `axi chat` failure on 2026-05-03 surfaced as
    `OSError: [Errno 22] Invalid argument: 'bonsai-1.7b.gguf'` — the
    bonsai gguf name is the canonical signal. We also match on the
    bare `bonsai-1.7b` model id so a future failure path that uses a
    different errno still classifies correctly.
    """
    if not event:
        return None
    # Match only on the user-visible error signal — fingerprint is an
    # internal hash and including it would couple matching to whatever
    # `self_heal._fingerprint` happens to emit, which is the wrong layer.
    haystacks = [
        str(event.get("error_message", "")),
        str(event.get("traceback", "")),
    ]
    blob = "\n".join(haystacks).lower()
    if not blob:
        return None
    if "bonsai-1.7b.gguf" in blob or "bonsai-1.7b" in blob or "bonsai-local" in blob:
        return Diagnosis(
            pattern_id="bonsai-deprecated",
            summary=(
                "axi chat fell through to the legacy `bonsai-local` provider "
                "and the bonsai-1.7b GGUF is not present. Bonsai is being "
                "deprecated in favor of Qwen2.5-7B."
            ),
            remedy=_BONSAI_REMEDY,
            confidence=0.95,
        )
    return None


# ---------------------------------------------------------------------------
# Pattern: No LLM provider configured (fresh install / wiped config)
# ---------------------------------------------------------------------------

_NO_LLM_PROVIDER_REMEDY = (
    "No LLM provider is configured. Either: (1) run `axi config` to step "
    "through the setup wizard, or (2) edit `runtime/config/llm-providers.toml` "
    "manually — copy a `[[gateway.providers]]` block from "
    "`runtime/config.example/models.toml` and fill in the model + endpoint + "
    "api_key_env. The cheapest path is the bundled local llamafile: provider "
    "`local-llamafile`, model `qwen2.5-7b-instruct`, endpoint "
    "`http://localhost:8080/v1`. Then run `axi setup model qwen` to download "
    "the GGUF."
)


def _no_llm_provider_matcher(event: dict) -> Diagnosis | None:
    if not event:
        return None
    blob = "\n".join(
        str(event.get(k, "")) for k in ("error_message", "traceback")
    ).lower()
    triggers = (
        "no llm providers available",
        "no providers configured",
        "no llm provider",
        "providers list is empty",
    )
    if any(t in blob for t in triggers):
        return Diagnosis(
            pattern_id="no-llm-provider-configured",
            summary=(
                "Axiom couldn't pick an LLM provider — none are configured "
                "(or all are gated by VPN / missing keys)."
            ),
            remedy=_NO_LLM_PROVIDER_REMEDY,
            confidence=0.9,
        )
    return None


# ---------------------------------------------------------------------------
# Pattern: Missing API-key environment variable
# ---------------------------------------------------------------------------


def _missing_api_key_matcher(event: dict) -> Diagnosis | None:
    """Match when the failure mentions a `*_API_KEY` env var being absent.

    Common shapes:
      `KeyError: 'ANTHROPIC_API_KEY'`
      `os.environ['OPENAI_API_KEY']` (in traceback) + KeyError
      `PRIVATE_LLM_API_KEY is not set`
    """
    if not event:
        return None
    blob = "\n".join(
        str(event.get(k, "")) for k in ("error_message", "traceback")
    )
    # Find any *_API_KEY identifier in the failure text.
    import re as _re
    match = _re.search(r"\b([A-Z][A-Z0-9_]*_API_KEY)\b", blob)
    if not match:
        return None
    var = match.group(1)
    # Discriminate from coincidental mentions (e.g., docs strings) by
    # requiring an "absence" signal alongside.
    blob_low = blob.lower()
    absence_signals = (
        "not set",
        "not found",
        "is missing",
        "keyerror",
        "environment variable",
        "missing required",
    )
    if not any(s in blob_low for s in absence_signals):
        return None
    return Diagnosis(
        pattern_id=f"missing-api-key:{var}",
        summary=(
            f"The `{var}` environment variable is not set, but a configured "
            "LLM provider requires it."
        ),
        remedy=(
            f"Set `{var}` in your shell (`export {var}=...`) or in a `.env` "
            f"file Axiom auto-loads. To remove the requirement entirely, "
            f"comment out the corresponding `[[gateway.providers]]` block in "
            f"`runtime/config/llm-providers.toml`."
        ),
        confidence=0.9,
    )


# ---------------------------------------------------------------------------
# Pattern: Local llamafile not running (connection refused on localhost LLM)
# ---------------------------------------------------------------------------


def _local_llamafile_down_matcher(event: dict) -> Diagnosis | None:
    """Match a connection refused on any localhost endpoint when the failing
    command is LLM-touching (chat, agent invocations, etc.).

    The discriminator is *command context*, not port number — different
    deployments use different local-LLM ports (llamafile, ollama,
    user-chosen) and hardcoding the set would false-negative most of them.
    """
    if not event:
        return None
    error_message = str(event.get("error_message", "")).lower()
    traceback = str(event.get("traceback", "")).lower()
    command = str(event.get("command", "")).lower()
    blob = error_message + "\n" + traceback

    refused_signals = (
        "connection refused",
        "connectionrefused",
        "max retries exceeded",
        "failed to establish a new connection",
    )
    if not any(s in blob for s in refused_signals):
        return None

    # Match any localhost endpoint — URL form (`localhost:NNNN`) or
    # requests-library form (`host='localhost', port=NNNN`).
    import re as _re
    url_form = r"(?:127\.0\.0\.1|localhost|::1):\d+"
    requests_form = r"host=['\"](?:127\.0\.0\.1|localhost|::1)['\"]"
    if not (_re.search(url_form, blob) or _re.search(requests_form, blob)):
        return None

    # Discriminate from non-LLM connection failures (e.g., a database or
    # bus localhost connection): require the failing command to be one that
    # routes through the LLM gateway. Extension authors who add new
    # LLM-using verbs should append to this set.
    llm_using_commands = {"chat", "ask", "explain", "classroom", "research", "review"}
    if command not in llm_using_commands and not any(
        s in blob for s in ("gateway", "llm", "completion", "/v1/chat")
    ):
        return None

    # The bonsai-deprecated pattern handles bonsai-specific localhost
    # failures with a more specific remedy (config swap).  Defer to it.
    if "bonsai" in blob:
        return None

    return Diagnosis(
        pattern_id="local-llamafile-down",
        summary=(
            "Connection refused to a localhost LLM endpoint — the local "
            "model server is not running on the configured port."
        ),
        remedy=(
            "Start your local LLM server, then retry. With Axiom's bundled "
            "Qwen llamafile: `axi setup model qwen` (downloads the GGUF if "
            "needed) and the server starts on the configured port. With "
            "Ollama: `ollama serve` (default 11434). Or point the gateway "
            "at a remote provider by editing the appropriate "
            "`[[gateway.providers]]` block in "
            "`runtime/config/llm-providers.toml`."
        ),
        confidence=0.85,
    )


# ---------------------------------------------------------------------------
# Pattern: State directory permission denied
# ---------------------------------------------------------------------------


def _state_dir_permission_matcher(event: dict) -> Diagnosis | None:
    if not event:
        return None
    error_message = str(event.get("error_message", ""))
    error_type = str(event.get("error_type", ""))
    traceback = str(event.get("traceback", ""))
    blob = error_message + "\n" + traceback
    blob_low = blob.lower()

    is_permission = (
        "permissionerror" in error_type.lower()
        or "permission denied" in blob_low
        or "[errno 13]" in blob_low
    )
    if not is_permission:
        return None
    has_state_dir = ".axi" in blob or ".neut" in blob
    if not has_state_dir:
        return None
    return Diagnosis(
        pattern_id="state-dir-permission-denied",
        summary=(
            "Axiom's user state directory (`~/.axi/` or `~/.neut/`) is not "
            "writable by the current user."
        ),
        remedy=(
            "Most often this is leftover root-owned state from running an axi "
            "command with sudo at some point. Fix with: "
            "`sudo chown -R $(id -un):$(id -gn) ~/.axi`. If the directory "
            "lives on a read-only mount or the disk is full, run "
            "`axi dr` for a fuller diagnosis."
        ),
        confidence=0.92,
    )


# ---------------------------------------------------------------------------
# Pattern: Extension module import error
# ---------------------------------------------------------------------------


def _module_import_error_matcher(event: dict) -> Diagnosis | None:
    if not event:
        return None
    error_type = str(event.get("error_type", ""))
    error_message = str(event.get("error_message", ""))
    traceback = str(event.get("traceback", ""))
    blob = error_message + "\n" + traceback

    if error_type not in ("ModuleNotFoundError", "ImportError"):
        return None
    # Keep the catalog match conservative — only fire when the import error
    # mentions an axiom extension or an extension manifest entry. Generic
    # "no module named foo" failures can be many things.
    triggers = (
        "axiom.extensions",
        "extension manifest",
        "axiom-extension.toml",
        "[[extension.provides]]",
        "ext_info",
        "_dispatch_extension",
    )
    if not any(t in blob.lower() for t in triggers):
        return None
    # Extract the missing module name when possible.
    import re as _re
    match = _re.search(r"No module named ['\"]([\w.]+)['\"]", error_message)
    missing = match.group(1) if match else "<unknown>"
    return Diagnosis(
        pattern_id="extension-module-import-error",
        summary=(
            f"An extension's manifest declares an entry pointing at a module "
            f"that cannot be imported: `{missing}`."
        ),
        remedy=(
            "Verify the extension's `axiom-extension.toml` `entry = "
            "\"module:func\"` declarations match real Python paths. Run "
            "`axi ext lint` to surface manifest/code mismatches across all "
            "installed extensions. If the extension was installed via pip, "
            "the wheel may be missing the module — re-install with "
            "`pip install --force-reinstall <extension>`."
        ),
        confidence=0.8,
    )


# ---------------------------------------------------------------------------
# Pattern: apt signing keyring missing/empty (self-hosted node, 2026-06-22)
# ---------------------------------------------------------------------------

_APT_KEYRING_REMEDY = (
    "An apt repository's signing keyring is missing or empty (0 bytes), so "
    "`apt update` can't verify it (NO_PUBKEY). This happens when the key "
    "download during install failed but left the keyring file behind — "
    "`gpg --dearmor` then refuses to overwrite it on re-runs. Fix: re-fetch "
    "the key for the offending repo and dearmor it with --yes, e.g.\n"
    "  curl -fsSL <repo>/Release.key \\\n"
    "    | sudo gpg --batch --yes --dearmor -o <signed-by-path>\n"
    "then `sudo apt-get update`. Identify <repo>/<signed-by-path> from the "
    "`deb [signed-by=...]` line in /etc/apt/sources.list.d/. For the "
    "kubernetes repo the key is https://pkgs.k8s.io/core:/stable:/<ver>/deb/"
    "Release.key. `axi hygiene` flags empty keyrings under the `apt_keyring` "
    "check."
)


def _apt_keyring_matcher(event: dict) -> Diagnosis | None:
    """Match apt update failures caused by a missing/unverifiable repo key.

    Signals: apt's own wording — `NO_PUBKEY`, "is not signed", or "couldn't
    be verified" — combined with an apt/repository context token so we don't
    fire on unrelated GPG errors.
    """
    if not event:
        return None
    blob = "\n".join(
        [str(event.get("error_message", "")), str(event.get("traceback", ""))]
    ).lower()
    if not blob:
        return None
    apt_signal = (
        "no_pubkey" in blob
        or "is not signed" in blob
        or "couldn't be verified" in blob
        or "could not be verified" in blob
    )
    apt_context = (
        "gpg error" in blob
        or "inrelease" in blob
        or "apt" in blob
        or "repository" in blob
        or "pkgs.k8s.io" in blob
    )
    if apt_signal and apt_context:
        return Diagnosis(
            pattern_id="apt-keyring-missing",
            summary=(
                "An apt repository's signing keyring is missing or empty, so "
                "`apt update` reports it as unsigned (NO_PUBKEY). The key "
                "download during install failed and left an empty keyring."
            ),
            remedy=_APT_KEYRING_REMEDY,
            confidence=0.9,
        )
    return None


PATTERN_CATALOG: tuple[Pattern, ...] = (
    # Order matters: earlier patterns win on overlap. Ordered by likelihood
    # × specificity. Qwen2.5-7B is Axiom's default local LLM; bonsai is a
    # sunset model with near-zero install base, so its diagnosis sits near
    # the bottom — present for the few legacy installs that still need it,
    # but the local-llamafile-down pattern handles the common Qwen case.
    Pattern(pattern_id="no-llm-provider-configured", matcher=_no_llm_provider_matcher),
    Pattern(pattern_id="missing-api-key", matcher=_missing_api_key_matcher),
    Pattern(pattern_id="state-dir-permission-denied", matcher=_state_dir_permission_matcher),
    Pattern(pattern_id="extension-module-import-error", matcher=_module_import_error_matcher),
    Pattern(pattern_id="local-llamafile-down", matcher=_local_llamafile_down_matcher),
    Pattern(pattern_id="apt-keyring-missing", matcher=_apt_keyring_matcher),
    Pattern(pattern_id="bonsai-deprecated", matcher=_bonsai_matcher),
)


def match_failure(event: dict[str, Any]) -> Diagnosis | None:
    """Run the catalog against `event` and return the first match.

    The catalog is ordered — earlier patterns win on overlapping matches.
    Today only one pattern exists; ordering matters when the catalog
    grows. Audit-able via `PATTERN_CATALOG` enumeration (Coverage
    Manifest meta-row).
    """
    if not event:
        return None
    for pattern in PATTERN_CATALOG:
        diagnosis = pattern.matcher(event)
        if diagnosis is not None:
            return diagnosis
    return None


# ---------------------------------------------------------------------------
# LLM-mediated fallback — when no pattern matches, ask Qwen.
#
# Per the user's 2026-05-03 directive: "if there's no matching failure
# condition in the catalog, Triage should resort to internal Qwen (unless
# happened to be the cause of the error) and present Qwen + local RAG's
# best guess." This module implements the LLM half; RAG augmentation is
# deferred to a follow-up that wires the retrieval store.
# ---------------------------------------------------------------------------

LLM_RELATED_KEYWORDS = (
    "llm",
    "gateway",
    "qwen",
    "anthropic",
    "openai",
    "claude",
    "model",
    "tokenizer",
    "tiktoken",
    "embedding",
    "ollama",
    "llamafile",
    "bonsai",  # bonsai is LLM-related, but the *catalog* matches it first;
               # the keyword guard ensures we don't recursively call the LLM
               # when the LLM itself is missing.
)


def _is_llm_related_error(event: dict[str, Any]) -> bool:
    """Heuristic: would a failed LLM call recursively trigger the same
    failure mode if used to diagnose this event?"""
    blob = " ".join(
        str(event.get(k, "")) for k in ("error_message", "traceback", "command")
    ).lower()
    return any(kw in blob for kw in LLM_RELATED_KEYWORDS)


def _llm_diagnose(event: dict[str, Any], gateway=None) -> Diagnosis | None:
    """Ask the LLM gateway for a best-guess diagnosis.

    Returns None when:
    - The error is LLM-related (loop protection).
    - No gateway is reachable.
    - The LLM call fails or returns empty.

    Confidence is capped at 0.7 because the answer is a guess — catalog
    matches stay above this so they always win when both produce a
    candidate.
    """
    if _is_llm_related_error(event):
        return None
    try:
        if gateway is None:
            from axiom.infra.gateway import Gateway
            gateway = Gateway()
        prompt = (
            "You are diagnosing a CLI failure. Read the error event and "
            "respond with a brief diagnosis and a concrete remedy.\n\n"
            f"Command: {event.get('command', '?')}\n"
            f"Error type: {event.get('error_type', '?')}\n"
            f"Error message: {event.get('error_message', '')}\n"
            f"Traceback (first 2000 chars):\n{str(event.get('traceback', ''))[:2000]}\n\n"
            "Respond as JSON with two keys:\n"
            "  summary  — one sentence describing the likely root cause.\n"
            "  remedy   — concrete commands or file edits the user should try.\n"
            "Output ONLY the JSON, no fences, no commentary."
        )
        response = gateway.complete(
            prompt=prompt,
            system="You are TRIAGE, a careful diagnostic agent. Be specific.",
            task="extraction",
            max_tokens=400,
        )
        if not getattr(response, "success", False):
            return None
        text = (getattr(response, "text", "") or "").strip()
        if not text:
            return None
        # Tolerate code-fenced JSON or trailing prose.
        import json as _json
        import re as _re
        match = _re.search(r"\{.*\}", text, flags=_re.DOTALL)
        if not match:
            return None
        try:
            payload = _json.loads(match.group(0))
        except _json.JSONDecodeError:
            return None
        summary = str(payload.get("summary", "")).strip()
        remedy = str(payload.get("remedy", "")).strip()
        if not summary or not remedy:
            return None
        # Per-error pattern_id so two distinct unknown failures don't
        # collide under one LLM-fallback fingerprint. The catalog matcher
        # *intentionally* shares fingerprints across the same root cause;
        # LLM-fallback should treat each error as distinct.
        error_seed = (
            f"{event.get('command', '')}:{event.get('error_type', '')}:"
            f"{event.get('error_message', '')}"
        )
        seed_hash = hashlib.sha1(error_seed.encode("utf-8")).hexdigest()[:8]
        return Diagnosis(
            pattern_id=f"llm-fallback:{seed_hash}",
            summary=summary,
            remedy=remedy,
            confidence=0.6,
        )
    except Exception:
        return None


def diagnose(
    event: dict[str, Any],
    *,
    gateway=None,
    allow_llm: bool = True,
) -> Diagnosis | None:
    """Catalog match first; on miss, optional LLM fallback.

    `allow_llm=False` keeps tests deterministic (no network, no model).
    Production callers (the listener) pass `allow_llm=True`.
    """
    direct = match_failure(event)
    if direct is not None:
        return direct
    if not allow_llm:
        return None
    return _llm_diagnose(event, gateway=gateway)
