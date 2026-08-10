# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Sensitivity Router — classifies LLM queries before dispatch.

Runs entirely locally. No cloud calls are made to determine routing.
Classification result determines which provider tier handles the request:
  - "public"            → cloud LLM (Anthropic, OpenAI, etc.)
  - "export_controlled" → private-network LLM (any provider with requires_vpn=true)

Classification pipeline (in order, short-circuits on first definitive result):
  1. Session mode override  → immediate (fastest)
  2. Keyword match          → export_controlled (zero latency, definitive)
     └─ allowlist filter    → suppress facility-specific false positives
  3. Ollama SLM classifier  → semantic judgment on the full context window
     └─ uncertain response  → behavior depends on sensitivity setting
  4. Fallback               → public or export_controlled per sensitivity

Sensitivity (routing.sensitivity setting):
  strict     — fallback is export_controlled; Ollama "uncertain" → export_controlled
  balanced   — fallback is public; Ollama "uncertain" → skip to fallback (default)
  permissive — skip Ollama entirely; fallback is public

Context Window (Option A):
  classify() accepts `context`: the last N conversation turns. Classifying
  the window (not just the current message) catches cases like "what's the
  API endpoint?" after several turns about a sensitive topic — no keyword,
  but clearly in a controlled conversation.

Ollama SLM Classifier (Option C):
  A small local model (e.g., llama3.2:1b via Ollama) provides semantic
  classification for queries that pass the keyword filter. Runs in <500ms on
  Apple Silicon, fully offline, zero new Python dependencies (plain HTTP).
  Falls back silently to "public" if Ollama is not running.

Domain-specific Configuration:
  The classifier prompt is loaded from runtime/config/classifier_prompt.txt
  if present, allowing domain-specific sensitivity rules without code changes.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal

from axiom import REPO_ROOT as _REPO_ROOT

_RUNTIME_CONFIG = _REPO_ROOT / "runtime" / "config"
_MODULE_DIR = Path(__file__).parent

_MIRROR_SCRUB_FILE = _RUNTIME_CONFIG / "mirror_scrub_terms.txt"
_USER_TERMS_FILE = _RUNTIME_CONFIG / "export_control_terms.txt"
_BUILTIN_TERMS_FILE = _MODULE_DIR / "_export_control_terms_default.txt"
_ALLOWLIST_FILE = _RUNTIME_CONFIG / "routing_allowlist.txt"

# Number of prior conversation turns to include in classification window.
# Each "turn" = one user message. We look back this many user messages plus
# the current one to catch context-dependent sensitivity.
CONTEXT_WINDOW_TURNS = 5

# Ollama defaults (override via axi settings)
_OLLAMA_BASE = "http://localhost:11434"
_OLLAMA_MODEL = "llama3.2:1b"
_OLLAMA_TIMEOUT = 2.0  # seconds — must not block the chat loop

# Default classifier prompt — domain-agnostic fallback.
# Override with runtime/config/classifier_prompt.txt for domain-specific routing.
_DEFAULT_CLASSIFIER_PROMPT = """\
You are a sensitivity classifier for a data platform.
Your only job: determine if the text discusses sensitive or restricted content.

Answer with exactly one word: yes, no, or uncertain.
Use "uncertain" only if you genuinely cannot tell. Prefer yes or no."""

_CLASSIFIER_PROMPT_FILE = _RUNTIME_CONFIG / "classifier_prompt.txt"


def _load_classifier_prompt() -> str:
    """Load domain-specific classifier prompt from config, or use default."""
    if _CLASSIFIER_PROMPT_FILE.exists():
        return _CLASSIFIER_PROMPT_FILE.read_text(encoding="utf-8").strip()
    return _DEFAULT_CLASSIFIER_PROMPT


# Lazily loaded to allow config override
_OLLAMA_SYSTEM: str | None = None


def _get_classifier_prompt() -> str:
    """Get the classifier prompt, loading from config on first call."""
    global _OLLAMA_SYSTEM
    if _OLLAMA_SYSTEM is None:
        _OLLAMA_SYSTEM = _load_classifier_prompt()
    return _OLLAMA_SYSTEM

# Valid sensitivity values
SENSITIVITY_STRICT = "strict"
SENSITIVITY_BALANCED = "balanced"
SENSITIVITY_PERMISSIVE = "permissive"
_VALID_SENSITIVITIES = {SENSITIVITY_STRICT, SENSITIVITY_BALANCED, SENSITIVITY_PERMISSIVE}


class RoutingTier(str, Enum):
    PUBLIC = "public"
    EXPORT_CONTROLLED = "export_controlled"


# Reason codes for typed classifier failures (Improvement Item 1).
# Mirrors the operator-visibility story: each variant maps to a distinct,
# actionable diagnostic in audit + UX surfaces.
ClassifierFailureReason = Literal[
    "ollama_unreachable",     # daemon not running / wrong port / firewall
    "model_not_loaded",       # daemon up, model missing — needs `ollama pull`
    "timeout",                # daemon up but didn't answer in time
    "unexpected_response",    # parsed but unparseable as yes/no/uncertain
    "transport_error",        # any other unexpected exception
]


@dataclass(frozen=True)
class ClassifierFailure:
    """Typed failure result from the SLM classifier.

    Distinguishes ``ollama_unreachable`` from ``model_not_loaded`` (and other
    failure modes) so the audit log + operator UX can surface specific
    remediation rather than a generic "uncertain". Returned from
    :meth:`OllamaClassifier.classify` instead of the prior catch-all ``None``
    when the failure is *attributable* — i.e., we tried to call Ollama and
    something went wrong. The legacy ``None`` is reserved for the
    pre-availability-check path (caller decides how to interpret).
    """

    reason: ClassifierFailureReason
    detail: str                  # human-readable diagnostic
    endpoint: str                # the Ollama base URL
    model: str | None = None     # the configured model name (when known)


@dataclass
class RoutingDecision:
    tier: RoutingTier
    reason: str
    matched_terms: list[str] = field(default_factory=list)
    classifier: str = "keyword"  # "session" | "keyword" | "ollama" | "fallback"
    tags: set[str] = field(default_factory=set)
    routing_event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    keyword_term: str | None = None  # first matched term (for audit)
    classifier_failure: ClassifierFailure | None = None
    """Typed SLM failure when the classifier was attempted but errored.

    None on the success paths and on the pre-availability-check skip. When
    populated, downstream consumers can surface "your SLM is unavailable
    because: <reason> <model>" in user-facing messages."""
    """Facility-policy tags carried forward to gateway._select_provider.

    The router sets tier for legal/compliance (sensitivity rules).
    Tags express facility routing policy on top: which specific private-network
    LLM to use, workflow-level routing, etc.

    Examples:
      {"domain-sensitive"}   → route to a provider tagged "domain-sensitive"
      {"private_network"}    → any private-network provider
      {"internal_compute"}   → facility intranet provider
    Tags are populated by the caller (e.g. session mode, CLI --tags flag) or
    by future classifier extensions.  The base router sets them to empty set.
    """

    def __str__(self) -> str:
        tag_str = f" tags=[{','.join(sorted(self.tags))}]" if self.tags else ""
        if self.matched_terms:
            terms = ", ".join(self.matched_terms[:3])
            suffix = f" (+{len(self.matched_terms) - 3} more)" if len(self.matched_terms) > 3 else ""
            return f"{self.tier.value} [{self.classifier}]{tag_str} — matched: {terms}{suffix}"
        return f"{self.tier.value} [{self.classifier}]{tag_str} — {self.reason}"


def _load_terms(path: Path) -> list[str]:
    """Load non-empty, non-comment lines from a term file."""
    if not path.exists():
        return []
    terms = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def _term_to_pattern(term: str) -> re.Pattern[str]:
    """Compile a term into a word-boundary-matching regex.

    - Default: whole-word match. ``SCRAM`` matches "discuss SCRAM here" but
      not "scrambled". ``\\b`` boundaries respect punctuation and hyphens.
    - Anchored: terms beginning with ``^`` and ending with ``$`` are exact
      matches against the full text (no surrounding context allowed).
      Useful when an operator wants ``^FOO$`` to suppress false positives.
    - Multi-word terms (e.g., ``weapons-usable``) are treated literally;
      the boundary is around the whole phrase.

    All matches are case-insensitive.
    """
    t = term.strip()
    anchored = t.startswith("^") and t.endswith("$")
    if anchored:
        inner = t[1:-1]
        return re.compile(rf"^\s*{re.escape(inner)}\s*$", re.IGNORECASE)
    # Word-boundary match. \b doesn't quite handle hyphens the way users
    # expect (a hyphen IS a word boundary in regex terms), so phrases like
    # "weapons-usable" still match cleanly: \bweapons-usable\b works.
    return re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE)


def _keyword_match(
    text: str, terms: list[str], allowlist: set[str]
) -> list[str]:
    """Shared keyword-matching implementation.

    Free function so the :class:`QueryRouter._keyword_check` instance
    method has one source of truth for keyword matching.

    Uses whole-word matching with optional ``^...$`` anchors per
    :func:`_term_to_pattern`. Eliminates substring false positives
    (e.g., ``SCRAM`` vs ``SCRAMble``) that earlier substring matching
    produced.
    """
    normalized = re.sub(r"\s+", " ", text)
    matched: list[str] = []
    for t in terms:
        pat = _term_to_pattern(t)
        if pat.search(normalized) and t.lower() not in allowlist:
            matched.append(t)
    return matched


def _max_mtime(*paths: Path) -> float:
    """Maximum mtime across the given paths; 0.0 if none exist."""
    mt = 0.0
    for p in paths:
        try:
            m = p.stat().st_mtime
            if m > mt:
                mt = m
        except (OSError, FileNotFoundError):
            continue
    return mt


def _extract_user_text(messages: list) -> str:
    """Pull user-role text from conversation messages (dicts or Message objects)."""
    parts = []
    for m in messages:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "role", None)
        if role != "user":
            continue
        content = m.get("content", "") if isinstance(m, dict) else getattr(m, "content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return " ".join(parts)


class OllamaClassifier:
    """Semantic export-control classifier using a local Ollama model.

    Supports three responses: yes, no, uncertain.
    Fails silently — never blocks the chat loop.
    """

    def __init__(
        self,
        base_url: str = _OLLAMA_BASE,
        model: str = _OLLAMA_MODEL,
        timeout: float = _OLLAMA_TIMEOUT,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        self._available: bool | None = None  # None = not yet checked

    def _check_available(self) -> bool:
        """Check that Ollama is running, auto-starting if needed."""
        if self._available is not None:
            return self._available
        try:
            req = urllib.request.Request(f"{self._base}/api/tags")
            with urllib.request.urlopen(req, timeout=0.5):
                pass
            self._available = True
        except Exception:
            # Auto-start via the connection's declared ensure hook
            try:
                from axiom.infra.connections import ensure_available
                if ensure_available("ollama"):
                    self._available = True
                    return True
            except Exception:
                pass
            self._available = False
        return self._available

    def classify(self, text: str) -> RoutingTier | str | ClassifierFailure | None:
        """Ask the SLM to classify text.

        Returns:
            RoutingTier.EXPORT_CONTROLLED  — confident yes
            RoutingTier.PUBLIC             — confident no
            "uncertain"                    — SLM couldn't decide; caller applies sensitivity
            ClassifierFailure              — attributable failure (unreachable, model
                                             not loaded, timeout, unexpected, transport)
            None                           — pre-availability-check failed; caller
                                             decides how to interpret (back-compat)
        """
        if not self._check_available():
            return None
        try:
            payload = json.dumps({
                "model": self._model,
                "system": _get_classifier_prompt(),
                "prompt": text[:4000],  # cap to avoid long contexts
                "stream": False,
                "options": {"num_predict": 5, "temperature": 0},
            }).encode()
            req = urllib.request.Request(
                f"{self._base}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                return ClassifierFailure(
                    reason="unexpected_response",
                    detail=f"could not decode response body: {exc}",
                    endpoint=self._base,
                    model=self._model,
                )
            answer = data.get("response", "").strip().lower().rstrip(".")
            if answer.startswith("yes"):
                return RoutingTier.EXPORT_CONTROLLED
            if answer.startswith("no"):
                return RoutingTier.PUBLIC
            if answer.startswith("uncertain"):
                return "uncertain"
            return ClassifierFailure(
                reason="unexpected_response",
                detail=f"SLM returned non-yes/no/uncertain answer: {answer!r}",
                endpoint=self._base,
                model=self._model,
            )
        except urllib.error.HTTPError as exc:
            # Ollama returns 404 when the requested model isn't loaded; some
            # versions return 500 with a "model ... not found" body. Detect
            # both and surface as model_not_loaded so the operator gets a
            # specific remediation (`ollama pull <model>`).
            body_text = ""
            try:
                if exc.fp is not None:
                    body_text = exc.fp.read().decode("utf-8", errors="replace")
            except Exception:
                body_text = ""
            looks_like_missing_model = (
                exc.code == 404
                or "not found" in body_text.lower()
                or "model" in body_text.lower() and "not" in body_text.lower()
            )
            if looks_like_missing_model:
                return ClassifierFailure(
                    reason="model_not_loaded",
                    detail=body_text.strip()
                    or f"HTTP {exc.code}: {exc.reason} (model '{self._model}' not loaded)",
                    endpoint=self._base,
                    model=self._model,
                )
            return ClassifierFailure(
                reason="transport_error",
                detail=f"HTTP {exc.code}: {body_text.strip() or exc.reason}",
                endpoint=self._base,
                model=self._model,
            )
        except TimeoutError as exc:
            return ClassifierFailure(
                reason="timeout",
                detail=f"socket timeout after {self._timeout}s: {exc}",
                endpoint=self._base,
                model=self._model,
            )
        except urllib.error.URLError as exc:
            inner = exc.reason
            # URLError can wrap TimeoutError (read timeout from urlopen).
            if isinstance(inner, (TimeoutError, socket.timeout)):
                return ClassifierFailure(
                    reason="timeout",
                    detail=f"request timed out after {self._timeout}s: {inner}",
                    endpoint=self._base,
                    model=self._model,
                )
            # Connection refused / DNS / network unreachable → ollama_unreachable.
            return ClassifierFailure(
                reason="ollama_unreachable",
                detail=f"could not reach Ollama at {self._base}: {inner}",
                endpoint=self._base,
                model=self._model,
            )
        except ConnectionError as exc:
            return ClassifierFailure(
                reason="ollama_unreachable",
                detail=f"connection error to {self._base}: {exc}",
                endpoint=self._base,
                model=self._model,
            )
        except Exception as exc:
            return ClassifierFailure(
                reason="transport_error",
                detail=str(exc) or exc.__class__.__name__,
                endpoint=self._base,
                model=self._model,
            )


class QueryRouter:
    """Classifies a query for LLM routing using a layered pipeline.

    Usage:
        router = QueryRouter()

        # Simple: current message only
        decision = router.classify("How do I access the restricted API?")

        # With conversation context (recommended):
        decision = router.classify(
            user_input,
            context=session.messages[-10:],   # last 5 turns (user+assistant pairs)
        )

        # With sensitivity override:
        decision = router.classify(user_input, sensitivity="strict")
    """

    def __init__(self, ollama: OllamaClassifier | None = None) -> None:
        self._terms: list[str] | None = None
        self._allowlist: set[str] | None = None
        self._terms_mtime: float = 0.0
        self._allowlist_mtime: float = 0.0
        self._ollama = ollama or self._default_ollama()

    @staticmethod
    def _default_ollama() -> OllamaClassifier:
        """Create OllamaClassifier with model from settings (if available)."""
        model = _OLLAMA_MODEL
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore
            model = SettingsStore().get("routing.ollama_model", _OLLAMA_MODEL)
        except Exception:
            pass
        return OllamaClassifier(model=model)

    def _get_terms(self) -> list[str]:
        """Lazy-load and cache the combined term list; reload on mtime change
        (Item 4 — runtime edits apply without process restart)."""
        current_mtime = _max_mtime(
            _BUILTIN_TERMS_FILE, _USER_TERMS_FILE, _MIRROR_SCRUB_FILE
        )
        if self._terms is None or current_mtime > self._terms_mtime:
            terms = _load_terms(_BUILTIN_TERMS_FILE)
            terms += _load_terms(_USER_TERMS_FILE)
            terms += _load_terms(_MIRROR_SCRUB_FILE)
            seen: set[str] = set()
            deduped = []
            for t in terms:
                key = t.lower()
                if key not in seen:
                    seen.add(key)
                    deduped.append(t)
            self._terms = deduped
            self._terms_mtime = current_mtime
        return self._terms

    def _get_allowlist(self) -> set[str]:
        """Lazy-load and cache the allowlist; reload on mtime change."""
        current_mtime = _max_mtime(_ALLOWLIST_FILE)
        if self._allowlist is None or current_mtime > self._allowlist_mtime:
            self._allowlist = {t.lower() for t in _load_terms(_ALLOWLIST_FILE)}
            self._allowlist_mtime = current_mtime
        return self._allowlist

    def _keyword_check(self, text: str) -> list[str]:
        """Return matched export-control terms after filtering the allowlist."""
        return _keyword_match(
            text, self._get_terms(), self._get_allowlist()
        )

    def _build_window(
        self, current_message: str, context: list | None
    ) -> str:
        """Concatenate recent conversation user turns + current message."""
        if not context:
            return current_message
        # Take last CONTEXT_WINDOW_TURNS * 2 messages (user+assistant pairs),
        # extract only the user-role text to avoid classifying our own outputs.
        recent = context[-(CONTEXT_WINDOW_TURNS * 2):]
        prior = _extract_user_text(recent)
        if prior:
            return f"{prior} {current_message}"
        return current_message

    def _resolve_sensitivity(self, sensitivity: str | None) -> str:
        """Return sensitivity, falling back to settings then default."""
        if sensitivity is not None:
            return sensitivity if sensitivity in _VALID_SENSITIVITIES else SENSITIVITY_BALANCED
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore
            val = SettingsStore().get("routing.sensitivity", SENSITIVITY_BALANCED)
            return val if val in _VALID_SENSITIVITIES else SENSITIVITY_BALANCED
        except Exception:
            return SENSITIVITY_BALANCED

    def classify(
        self,
        text: str,
        session_mode: str = "auto",
        context: list | None = None,
        sensitivity: str | None = None,
    ) -> RoutingDecision:
        """Classify a query using the full pipeline.

        Args:
            text:         The current user message.
            session_mode: "auto" | "public" | "export_controlled"
            context:      Recent conversation messages (list of role/content dicts).
                          When provided, the classifier considers the full window,
                          catching context-dependent sensitivity.
            sensitivity:  Override sensitivity for this call. When None, reads from
                          settings (routing.sensitivity). Values: strict | balanced | permissive.

        Returns:
            RoutingDecision with tier, reason, and which classifier decided.
        """
        sens = self._resolve_sensitivity(sensitivity)

        # ── 1. Session mode override (fastest) ──────────────────────────────
        decision: RoutingDecision | None = None
        if session_mode == "export_controlled":
            decision = RoutingDecision(
                tier=RoutingTier.EXPORT_CONTROLLED,
                reason="session mode override",
                classifier="session",
            )
        elif session_mode == "public":
            decision = RoutingDecision(
                tier=RoutingTier.PUBLIC,
                reason="session mode override",
                classifier="session",
            )
        else:
            # ── 2. Build classification window ─────────────────────────────
            window = self._build_window(text, context)

            # ── 3. Keyword match (zero-latency, definitive) ────────────────
            matched = self._keyword_check(window)
            if matched:
                decision = RoutingDecision(
                    tier=RoutingTier.EXPORT_CONTROLLED,
                    reason="export-control keyword match",
                    matched_terms=matched,
                    classifier="keyword",
                    keyword_term=matched[0],
                )
            else:
                ollama_result = None
                classifier_failure: ClassifierFailure | None = None
                # ── 4. Ollama SLM (semantic, local, no new deps) ───────────
                if sens != SENSITIVITY_PERMISSIVE:
                    ollama_result = self._ollama.classify(window)
                    if isinstance(ollama_result, ClassifierFailure):
                        # Capture for audit + RoutingDecision propagation,
                        # but treat as fallback for routing (behavior preserved).
                        classifier_failure = ollama_result
                        ollama_result = None
                        self._audit_classifier_failure(classifier_failure)
                    elif ollama_result == RoutingTier.EXPORT_CONTROLLED:
                        decision = RoutingDecision(
                            tier=RoutingTier.EXPORT_CONTROLLED,
                            reason="SLM: export-controlled content detected",
                            classifier="ollama",
                        )
                    elif ollama_result == RoutingTier.PUBLIC:
                        decision = RoutingDecision(
                            tier=RoutingTier.PUBLIC,
                            reason="SLM: no export-controlled content detected",
                            classifier="ollama",
                        )
                    elif ollama_result == "uncertain" and sens == SENSITIVITY_STRICT:
                        decision = RoutingDecision(
                            tier=RoutingTier.EXPORT_CONTROLLED,
                            reason="SLM: uncertain — routing conservatively (strict mode)",
                            classifier="ollama",
                        )
                    else:
                        ollama_result = None  # treat unavailable/uncertain-balanced as fallback

                if decision is None:
                    # ── 5. Fallback ────────────────────────────────────────
                    if sens == SENSITIVITY_STRICT:
                        decision = RoutingDecision(
                            tier=RoutingTier.EXPORT_CONTROLLED,
                            reason="no keyword match; routing conservatively (strict mode)",
                            classifier="fallback",
                            classifier_failure=classifier_failure,
                        )
                    else:
                        decision = RoutingDecision(
                            tier=RoutingTier.PUBLIC,
                            reason="no export-control terms detected",
                            classifier="fallback",
                            classifier_failure=classifier_failure,
                        )
                elif classifier_failure is not None:
                    # Defensive — every fallback above should have already
                    # carried it; left here so future branches don't drop it.
                    decision.classifier_failure = classifier_failure

        # ── Audit: write_classification for every decision ──────────────────
        assert decision is not None, "routing must always produce a decision"
        is_ec = decision.tier == RoutingTier.EXPORT_CONTROLLED
        try:
            import hashlib as _hashlib

            from axiom.infra.audit_log import AuditLog
            prompt_hash = _hashlib.sha256(text.encode()).hexdigest()
            AuditLog.get().write_classification(
                routing_event_id=decision.routing_event_id,
                prompt_hash=prompt_hash,
                classifier=decision.classifier,
                keyword_matched=bool(decision.matched_terms),
                keyword_term=decision.keyword_term,
                ollama_result=None,  # SLM result not stored separately yet
                sensitivity=sens,
                final_tier=decision.tier.value,
                is_ec=is_ec,
            )
        except Exception:
            pass  # audit write never blocks routing

        return decision

    def reload_terms(self) -> None:
        """Force reload of the term and allowlist caches (e.g., after user edits config)."""
        self._terms = None
        self._allowlist = None

    @staticmethod
    def _audit_classifier_failure(failure: ClassifierFailure) -> None:
        """Emit a typed audit event for a classifier failure.

        Decoupled from the classification audit so each failure mode is
        diagnosable in isolation. Swallows audit-side exceptions — a
        failed audit write never blocks routing.
        """
        try:
            from axiom.infra.audit_log import AuditLog
            AuditLog.get().write_classifier_failure(
                reason=failure.reason,
                detail=failure.detail,
                endpoint=failure.endpoint,
                model=failure.model,
            )
        except Exception:
            pass  # audit write never blocks routing
