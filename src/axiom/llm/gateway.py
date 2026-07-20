# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LLM Gateway — model-agnostic routing with graceful degradation.

Reads provider configuration from config/models.toml and routes requests
to the first available provider. If no providers are configured or all
calls fail, returns a stub response preserving the raw text.

Both the signal agent and publisher agent share this gateway.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.infra.provider_base import ProviderIdentityMixin, ensure_provider_uids

if TYPE_CHECKING:
    from axiom.llm.router import RoutingDecision

log = logging.getLogger(__name__)

_RATE_LIMIT_MAX_RETRIES = 3
_RATE_LIMIT_BACKOFF_BASE = 2.0
# 5xx + network-error retry budget. Smaller than 429 max — those are
# legitimate server-side hiccups, not pacing signals, so we don't want
# to amplify backend distress with long retry chains. Three attempts
# (initial + 2 retries) with full jitter is the AWS-blessed pattern.
_TRANSIENT_MAX_RETRIES = 3
_TRANSIENT_BACKOFF_BASE = 1.0  # seconds; jittered up to this * 2**attempt
_RETRYABLE_STATUSES = frozenset({502, 503, 504})

_RUNTIME_DIR = _REPO_ROOT / "runtime"
CONFIG_DIR = _RUNTIME_DIR / "config"
CONFIG_EXAMPLE_DIR = _RUNTIME_DIR / "config.example"

# Statuses that mean "the request itself is bad" → persistent (don't retry,
# don't silently fall back: surface it). 429 is rate-limiting (transient);
# 5xx + network errors are transient. Everything else 4xx is the caller's bug.
_PERSISTENT_STATUSES = frozenset({400, 401, 403, 404, 405, 413, 415, 422})


class LLMGatewayError(RuntimeError):
    """Base: a configured provider was tried and failed.

    Carries the real upstream ``status`` + ``body`` (or exception detail) so
    callers and humans see the actual reason instead of a guessed string.
    Mirrors ``axiom.rag.embeddings.EmbeddingError``.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        body: str | None = None,
        provider: str | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.provider = provider
        self.message = message
        detail = ""
        if provider:
            detail += f" [{provider}]"
        if status is not None:
            detail += f" HTTP {status}"
        if body:
            detail += f": {body[:300]}"
        super().__init__(message + detail)

    def client_summary(self) -> str:
        """A client-safe one-line reason — provider + status + message, but
        WITHOUT the raw upstream ``body``. The full body (which can echo an
        upstream's internal hostnames, DSNs, or auth hints) stays in logs and
        ``str(self)``; this is what may cross the external HTTP boundary."""
        parts = [self.message]
        if self.provider:
            parts.append(f"[{self.provider}]")
        if self.status is not None:
            parts.append(f"HTTP {self.status}")
        return " ".join(parts)


class TransientLLMError(LLMGatewayError):
    """Retryable failure — network drop, timeout, 429, 5xx. Safe to retry the
    same provider or fall back to another one."""


class PersistentLLMError(LLMGatewayError):
    """Non-retryable failure — the provider rejected the REQUEST (4xx other
    than 429: bad auth, bad model, malformed payload). Retrying or silently
    falling back to another provider hides a real bug; surface it."""


def _classify_http_error(exc: Exception, provider: str | None = None) -> LLMGatewayError:
    """Turn a ``requests`` exception into a typed gateway error carrying the
    real upstream status + body.

    Converts the opaque ``HTTPError`` / connection error raised by
    ``_post_with_rate_limit_retry`` into a self-describing transient/persistent
    error so callers stop guessing. Classification rule:
      * no response (connection/timeout) → transient
      * 4xx in ``_PERSISTENT_STATUSES``  → persistent
      * 429 / 5xx / anything else        → transient
    """
    if isinstance(exc, LLMGatewayError):
        if provider and exc.provider is None:
            exc.provider = provider
        return exc
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    body = None
    if response is not None:
        try:
            body = response.text
        except Exception:
            body = None
    if status is None:
        return TransientLLMError(
            str(exc) or exc.__class__.__name__, body=body, provider=provider
        )
    if status in _PERSISTENT_STATUSES:
        return PersistentLLMError(
            "provider rejected request", status=status, body=body, provider=provider
        )
    return TransientLLMError(
        "provider call failed", status=status, body=body, provider=provider
    )


def _ensure_dotenv():
    """Load .env from repo root if not already loaded."""
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


_ensure_dotenv()


def _floor_max_tokens(provider, max_tokens: int) -> int:
    """``max_tokens_default`` acts as a FLOOR (minimum output budget), never a
    cap. Reasoning models (gpt-oss, Qwen) spend tokens on ``<think>`` before the
    answer; without a floor a small caller ``max_tokens`` is consumed entirely
    by reasoning, leaving the answer empty/truncated. Never reduces a larger
    caller request. Applied on every upstream path (stream + non-stream)."""
    floor = getattr(provider, "max_tokens_default", 0) or 0
    return max(int(max_tokens), int(floor))


# Backward-compat aliases for routing_tier values in llm-providers.toml.
# Older configs used "restricted" as the private-network tier name; the
# canonical name is now "export_controlled". Aliases are normalized at
# LLMProvider construction time so all matching code sees the canonical
# value. Tags (routing_tags) are independent and preserved as-is.
_ROUTING_TIER_ALIASES = {
    "restricted": "export_controlled",
}


@dataclass
class LLMProvider(ProviderIdentityMixin):
    """LLM provider configuration. Inherits three-layer identity from ProviderIdentityMixin."""

    _log_prefix: str = field(default="llm_provider", init=False, repr=False)
    _fingerprint_fields: tuple = field(
        default=("endpoint", "model", "routing_tier"), init=False, repr=False
    )

    name: str  # Display name — human-readable label, shown in UI and logs
    endpoint: str
    model: str
    uid: str = ""  # Stable unique ID — persisted in config. Auto-generated if absent.
    api_key_env: str = ""
    priority: int = 99
    use_for: list[str] = field(default_factory=lambda: ["fallback"])
    routing_tier: str = "any"  # "public" | "export_controlled" | "any" (legacy; still respected)
    routing_tags: list[str] = field(
        default_factory=list
    )  # facility policy tags e.g. ["domain-sensitive", "private_network"]
    requires_vpn: bool = False  # if True, TCP-check endpoint before calling
    verify_ssl: bool = True  # set False for private servers with self-signed certs
    # Tool-calling strategy (axiom.llm.tool_calling): "auto" (native-first, shim
    # fallback) | "native" (server emits tool_calls) | "shim" (server can't, use
    # the JSON-action shim). Externalized so it's hot-patchable without a restart.
    tool_mode: str = "auto"
    max_tokens_default: int = (
        0  # 0 = use caller's value; set >0 for reasoning models that need headroom
    )
    # Transport: how within-tier mechanics are owned (RATIONALIZE-4).
    #   "direct"  — Axiom owns within-tier fallback/retry across its own
    #               provider list (legacy behavior).
    #   "litellm" — this provider is a whole per-tier LiteLLM router GROUP; the
    #               individual backends (vLLM, Tejas, cloud) live inside LiteLLM
    #               and are invisible to Axiom. LiteLLM owns fallback / retry /
    #               load-balance / wire-translation WITHIN the group, so Axiom
    #               must NOT fan out across its own provider list in this tier.
    #               EC isolation stays structural: the EC group's model list
    #               contains only EC-cleared backends AND routing_tier still
    #               gates selection above this seam.
    transport: str = "direct"

    # Identity fields — computed at load time, not set from config
    config_hash: str = field(default="", init=False)
    instance_id: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.routing_tier in _ROUTING_TIER_ALIASES:
            canonical = _ROUTING_TIER_ALIASES[self.routing_tier]
            log.warning(
                "LLMProvider '%s' uses deprecated routing_tier=%r — normalized to %r. "
                "Update llm-providers.toml to use %r directly.",
                self.name, self.routing_tier, canonical, canonical,
            )
            object.__setattr__(self, "routing_tier", canonical)

        uid_was_generated = self._compute_identity(
            {
                "uid": self.uid,
                "endpoint": self.endpoint,
                "model": self.model,
                "routing_tier": self.routing_tier,
            }
        )
        # _compute_identity may have generated a uid — sync it back onto the field
        if uid_was_generated:
            object.__setattr__(self, "uid", self.uid)  # uid attr already set by mixin
            log.warning(
                "LLMProvider '%s' has no 'uid' in config — generated uid=%s. "
                'Add uid = "%s" to llm-providers.toml to persist it across restarts.',
                self.name,
                self.uid,
                self.uid,
            )

    @property
    def api_key(self) -> str | None:
        # 1. Environment variable named by the provider (explicit override).
        if self.api_key_env:
            value = os.environ.get(self.api_key_env)
            if value:
                return value
        # 2. Vault: a credential stored under this provider's name. Lets the
        #    gateway resolve a key from `store_credential(<name>, ...)` with no
        #    plaintext env export. Resolved by provider name so an EC provider
        #    can never pick up a key stored for a public one.
        try:
            from axiom.infra.connections import get_credential

            return get_credential(self.name)
        except Exception:
            return None


@dataclass
class GatewayResponse:
    """Response from the LLM gateway."""

    text: str
    provider: str  # Which provider answered, or "stub"
    model: str = ""
    success: bool = True
    error: str | None = None


# --- New dataclasses for streaming + tool-use ---


@dataclass
class StreamChunk:
    """A single streaming delta from the LLM."""

    type: str  # "text", "tool_use_start", "tool_input_delta", "tool_use_end",
    #            "thinking_start", "thinking_delta", "thinking_end", "usage", "done"
    text: str = ""
    tool_name: str = ""
    tool_id: str = ""
    tool_input_json: str = ""
    # Usage fields (emitted with type="usage")
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


@dataclass
class ToolUseBlock:
    """A parsed tool-use block from the LLM response."""

    tool_id: str
    name: str
    input: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletionResponse:
    """Structured response with separated text + tool_use blocks."""

    text: str = ""
    tool_use: list[ToolUseBlock] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    success: bool = True
    error: str | None = None
    stop_reason: str = ""
    # Usage tracking
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


def _post_with_rate_limit_retry(requests_mod, url, payload, headers, timeout=60, **kwargs):
    """POST with adaptive rate limiting + transient-failure resilience.

    Retry policy:
      - 429 (rate limited): adaptive limiter + exponential backoff,
        ``_RATE_LIMIT_MAX_RETRIES`` attempts.
      - 502 / 503 / 504 (transient backend): full-jitter exponential
        backoff, ``_TRANSIENT_MAX_RETRIES`` attempts.
      - ConnectionError / Timeout (network): same as 5xx.
      - 4xx other than 429: no retry — caller bug, won't fix itself.
      - 2xx / 3xx: returned immediately.

    Full-jitter backoff (sleep = random(0, base * 2**attempt)) prevents
    thundering-herd resync after upstream returns from a brief outage —
    the AWS-blessed pattern for production resilience.
    """
    import random

    from axiom.infra.rate_limiter import get_limiter

    conn_name = _infer_connection_from_url(url)
    limiter = get_limiter(conn_name) if conn_name else None

    rl_attempt = 0
    transient_attempt = 0

    while True:
        if limiter:
            limiter.wait()

        try:
            start = time.monotonic()
            response = requests_mod.post(
                url, json=payload, headers=headers, timeout=timeout, **kwargs
            )
            elapsed = (time.monotonic() - start) * 1000
        except (
            requests_mod.exceptions.ConnectionError,
            requests_mod.exceptions.Timeout,
        ) as exc:
            transient_attempt += 1
            if transient_attempt >= _TRANSIENT_MAX_RETRIES:
                log.warning(
                    "Transient network failure from %s (attempt %d/%d): %s",
                    url, transient_attempt, _TRANSIENT_MAX_RETRIES, exc,
                )
                raise
            wait = random.uniform(0, _TRANSIENT_BACKOFF_BASE * (2 ** (transient_attempt - 1)))
            log.warning(
                "Transient network failure from %s (attempt %d/%d), retrying in %.2fs: %s",
                url, transient_attempt, _TRANSIENT_MAX_RETRIES, wait, exc,
            )
            time.sleep(wait)
            continue

        # Update limiter with response headers (learns the actual limits)
        if limiter:
            limiter.update(response)

        if response.status_code == 429:
            # Record throttle event
            try:
                from axiom.infra.connections import record_usage

                if conn_name:
                    record_usage(conn_name, elapsed, throttled=True)
            except Exception:
                pass
            rl_attempt += 1
            if rl_attempt >= _RATE_LIMIT_MAX_RETRIES:
                response.raise_for_status()
                return response
            wait = _RATE_LIMIT_BACKOFF_BASE * (2 ** (rl_attempt - 1))
            log.warning("Rate limited (429) from %s, retrying in %.1fs", url, wait)
            time.sleep(wait)
            continue

        if response.status_code in _RETRYABLE_STATUSES:
            transient_attempt += 1
            if transient_attempt >= _TRANSIENT_MAX_RETRIES:
                log.warning(
                    "Transient %d from %s after %d attempts, giving up",
                    response.status_code, url, transient_attempt,
                )
                response.raise_for_status()
                return response
            wait = random.uniform(
                0, _TRANSIENT_BACKOFF_BASE * (2 ** (transient_attempt - 1))
            )
            log.warning(
                "Transient %d from %s (attempt %d/%d), retrying in %.2fs",
                response.status_code, url, transient_attempt,
                _TRANSIENT_MAX_RETRIES, wait,
            )
            time.sleep(wait)
            continue

        # 2xx/3xx — success.  4xx other than 429 — caller bug, surface immediately.
        response.raise_for_status()
        try:
            from axiom.infra.connections import record_usage

            if conn_name:
                record_usage(conn_name, elapsed)
        except Exception:
            pass
        return response


def _infer_connection_from_url(url: str) -> str:
    """Best-effort map from URL to connection name for usage tracking."""
    if "anthropic" in url:
        return "anthropic"
    if "openai" in url:
        return "openai"
    if "github" in url:
        return "github"
    if "gitlab" in url:
        return "gitlab"
    return ""


# ---------------------------------------------------------------------------
# §14.2 auto_strategy name → factory registry. Mirrors the four built-in
# strategies in axiom.agents.strategy.builtin; keys match the names users
# write in `[gateway].auto_strategy`. Lazy-imported on demand to avoid a
# heavy import at gateway-module load time.
# ---------------------------------------------------------------------------


def _make_strategy_factories():
    from axiom.agents.strategy import builtin as _b
    return {
        "legacy-router": _b.legacy_router,
        "cost-conservative": _b.cost_conservative,
        "quality-first": _b.quality_first,
        "cohort-pinned": _b.cohort_pinned,
    }


class _LazyStrategyFactoryMap:
    """Dict-like wrapper that defers strategy imports to first use."""

    def __init__(self):
        self._cache: dict[str, Any] | None = None

    def _ensure(self) -> dict[str, Any]:
        if self._cache is None:
            self._cache = _make_strategy_factories()
        return self._cache

    def get(self, key: str, default=None):
        return self._ensure().get(key, default)


_STRATEGY_FACTORIES = _LazyStrategyFactoryMap()


class Gateway:
    """Model-agnostic LLM gateway with automatic fallback."""

    def __init__(self, config_dir: Path | None = None):
        if config_dir is None:
            config_dir = CONFIG_DIR if CONFIG_DIR.exists() else CONFIG_EXAMPLE_DIR
        self.config_dir = config_dir
        self.providers: list[LLMProvider] = []
        self._provider_override: str | None = None
        self._model_override: str | None = None
        self._ec_audit_enabled: bool = False
        # [gateway] block scalars from llm-providers.toml (excludes providers).
        # See spec-model-routing §14.2: default_routing, auto_strategy.
        self._gateway_config: dict[str, Any] = {}
        self._load_config()

    # ------------------------------------------------------------------
    # Provider / model overrides (wired from --provider / --model flags)
    # ------------------------------------------------------------------

    def set_provider_override(self, provider_name: str) -> None:
        """Pin all requests to a specific named provider."""
        self._provider_override = provider_name

    def set_model_override(self, model_name: str) -> None:
        """Override the model name on whichever provider is selected."""
        self._model_override = model_name

    # ------------------------------------------------------------------
    # §14 auto-routing dispatch (spec-model-routing)
    # ------------------------------------------------------------------

    def _resolve_via_strategy(
        self,
        task: str,
        *,
        routing_tier: str = "any",
        required_tags: set[str] | None = None,
        tier_hint: str | None = None,
    ) -> LLMProvider | None:
        """Resolve a provider via ModelStrategy per §14.2.

        Maps gateway state to a ModelContext + ProviderSpec list, dispatches
        to the configured `auto_strategy`, then looks up the chosen provider
        by name in self.providers. Returns None when no strategy is wired or
        the strategy name is unknown — caller falls back to legacy
        `_select_provider`.

        Raises ModelStrategyUnsatisfiable upward when the strategy itself
        runs but exhausts every candidate; caller handles the fallback.
        """
        from axiom.agents.strategy.types import (
            CohortModelPolicy,
            ModelContext,
            ModelRole,
            ProviderSpec,
            UserModelPolicy,
        )
        from axiom.vega.federation.policy import ClassificationStamp

        strategy_name = self._gateway_config.get("auto_strategy", "cost-conservative")
        factory = _STRATEGY_FACTORIES.get(strategy_name)
        if factory is None:
            log.warning(
                "Unknown auto_strategy %r; falling back to legacy selection",
                strategy_name,
            )
            return None

        # Build the candidate ProviderSpec list from currently-loaded
        # providers. Filter by api_key presence to mirror legacy hygiene.
        specs: list[ProviderSpec] = []
        for p in self.providers:
            if not p.api_key:
                continue
            tier = "private" if p.requires_vpn else (
                "private" if p.routing_tier == "export_controlled" else "public"
            )
            specs.append(
                ProviderSpec(
                    name=p.name,
                    tier=tier,
                    model=p.model,
                    estimated_cost_per_1k_tokens_usd=0.0,
                )
            )
        if not specs:
            return None

        # Network reachability: respect tier without doing live VPN probes
        # here (legacy path already does its own probe). Strategy survivors
        # are pruned by tier anyway.
        reachable = frozenset({"public", "private"})

        ctx = ModelContext(
            classification=ClassificationStamp.unclassified()
            if routing_tier != "export_controlled"
            else ClassificationStamp(level="cui"),
            budget_remaining_usd=10.0,
            network_reachable=reachable,
            user_policy=UserModelPolicy(),
            cohort_policy=CohortModelPolicy(),
            available_providers={},
            tier_hint=tier_hint,
        )

        strategy = factory(providers=specs)
        choice = strategy.resolve(ModelRole.EXECUTOR, ctx)
        for p in self.providers:
            if p.name == choice.provider:
                return self._apply_model_override(p)
        return None

    # ------------------------------------------------------------------
    # Routing-aware provider selection
    # ------------------------------------------------------------------

    def _select_provider(
        self,
        task: str,
        routing_tier: str = "any",
        required_tags: set[str] | None = None,
        prefer: str | None = None,
    ) -> LLMProvider | None:
        """Select the best available provider for a task + routing tier + tags.

        Priority order:
          1. --provider CLI override (if set, use that provider regardless of tier/tags)
          2. prefer_provider chain (from settings) — tries each in order, skips VPN
             providers whose endpoint is unreachable
          3. Candidates matching routing_tier AND required_tags (if any), filtered
             by task + api_key, sorted by priority
          4. Relax constraints as last resort

        Args:
            routing_tier:  "public" | "export_controlled" | "any" (legacy binary)
            required_tags: Facility-policy tags that must ALL appear in the
                           provider's routing_tags list.  Empty set = no filter.
                           Example: {"restricted"} routes only to providers tagged "restricted".
        """
        # CLI override: respect it unconditionally
        if self._provider_override:
            for p in self.providers:
                if p.name == self._provider_override and p.api_key:
                    return self._apply_model_override(p)
            print(
                f"Warning: provider '{self._provider_override}' not found or has no API key.",
                file=sys.stderr,
            )

        # Per-request explicit provider (e.g. the request's model field maps to
        # a configured provider name) WINS over the ambient settings default —
        # an explicit per-call choice should beat a standing preference. Still
        # tier-gated: never relax an EC request to a non-EC provider.
        if prefer:
            for p in self.providers:
                if p.name != prefer or not (p.api_key or not p.api_key_env):
                    continue
                if routing_tier == "export_controlled" and p.routing_tier != "export_controlled":
                    break  # EC may not be downgraded; fall through to safe selection
                return self._apply_model_override(p)

        # Check if user has a preferred provider chain
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            settings = SettingsStore()
            prefer_setting = settings.get("routing.prefer_provider", [])

            # Normalize: accept list or comma-separated string
            if isinstance(prefer_setting, str):
                chain = [n.strip() for n in prefer_setting.split(",") if n.strip()]
            else:
                chain = list(prefer_setting) if prefer_setting else []

            if chain:
                condition = settings.get("routing.prefer_when", "reachable")
                for pref_name in chain:
                    for p in self.providers:
                        if p.name != pref_name or not p.api_key:
                            continue
                        # Never route EC content to a non-EC provider via the
                        # prefer chain — tier must match exactly for EC requests.
                        if (
                            routing_tier == "export_controlled"
                            and p.routing_tier != "export_controlled"
                        ):
                            continue
                        if condition == "always":
                            return self._apply_model_override(p)
                        elif condition == "reachable":
                            if p.requires_vpn:
                                if self._check_vpn(p):
                                    return self._apply_model_override(p)
                            else:
                                return self._apply_model_override(p)
        except Exception:
            pass

        def _tier_match(p: LLMProvider) -> bool:
            if routing_tier == "any":
                return True
            if routing_tier == "export_controlled":
                # EC requests must go to an explicitly EC-cleared provider.
                # A provider tagged "any" is NOT EC-cleared — it means
                # "no restriction on my side" which only applies to public content.
                return p.routing_tier == "export_controlled"
            # public or other tiers: accept exact match or "any"
            return p.routing_tier in (routing_tier, "any")

        def _tags_match(p: LLMProvider) -> bool:
            if not required_tags:
                return True
            provider_tags = set(p.routing_tags)
            return required_tags.issubset(provider_tags)

        def _usable(p: LLMProvider) -> bool:
            """A provider is usable if it has an API key or doesn't need one."""
            return bool(p.api_key or not p.api_key_env)

        candidates = [
            p
            for p in self.providers
            if (task in p.use_for or "fallback" in p.use_for)
            and _tier_match(p)
            and _tags_match(p)
            and _usable(p)
        ]
        if not candidates:
            # Relax tag constraint, keep tier
            candidates = [p for p in self.providers if _tier_match(p) and _usable(p)]
        if not candidates and routing_tier != "export_controlled":
            # Relax tier as last resort — but NEVER for export_controlled.
            # Sending EC content to a public cloud provider is a compliance
            # violation; return None so the caller can surface a clear message.
            candidates = [p for p in self.providers if _usable(p)]

        # Tier-checked preference: honor a requested provider only if it is
        # already a tier-allowed candidate, so a flip can never escape the
        # classified tier / EC fail-closed. Unknown/disallowed -> ignored.
        if prefer:
            for c in candidates:
                if c.name == prefer:
                    return self._apply_model_override(c)

        candidates.sort(key=lambda p: p.priority)
        return self._apply_model_override(candidates[0]) if candidates else None

    def _apply_model_override(self, provider: LLMProvider) -> LLMProvider:
        """Return a copy of the provider with model_override applied, if set."""
        if self._model_override and provider.model != self._model_override:
            from dataclasses import replace

            return replace(provider, model=self._model_override)
        return provider

    def _check_vpn(self, provider: LLMProvider) -> bool:
        """Quick TCP reachability check for VPN-gated providers (1s timeout)."""
        import socket
        from urllib.parse import urlparse

        try:
            parsed = urlparse(provider.endpoint)
            host = parsed.hostname or ""
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            return False

    def _load_config(self):
        """Load LLM provider config from llm-providers.toml (previously models.toml)."""
        # Support both names during migration; prefer llm-providers.toml
        providers_path = self.config_dir / "llm-providers.toml"
        if not providers_path.exists():
            providers_path = self.config_dir / "models.toml"
        if not providers_path.exists():
            # No config file — still try local LLM discovery
            self._discover_local_llm()
            return
        models_path = providers_path

        try:
            from axiom.infra.toml_compat import tomllib

            with open(models_path, "rb") as f:
                config = tomllib.load(f)

            # Back-fill any missing uids before instantiating — writes to config file
            ensure_provider_uids(models_path, table_key="gateway.providers")

            gateway_config = config.get("gateway", {})
            providers = gateway_config.get("providers", [])
            # Stash non-provider gateway scalars for §14 auto-routing dispatch.
            self._gateway_config = {
                k: v for k, v in gateway_config.items() if k != "providers"
            }

            seen_names: set[str] = set()
            seen_uids: dict[str, str] = {}  # uid → display name of first occurrence
            for p in providers:
                pname = p.get("name", "")
                if not pname:
                    log.error(
                        "Provider entry missing required 'name' field — skipped. "
                        "Every provider must have a unique, stable 'name' in llm-providers.toml."
                    )
                    continue
                if pname in seen_names:
                    log.error(
                        "Duplicate provider name '%s' in llm-providers.toml — second entry skipped. "
                        "Provider names must be unique within a config file.",
                        pname,
                    )
                    continue
                puid = p.get("uid", "")
                if puid and puid in seen_uids:
                    log.error(
                        "Duplicate provider uid '%s' in llm-providers.toml — '%s' skipped "
                        "(uid already used by '%s'). Assign a unique uid to resolve the conflict.",
                        puid,
                        pname,
                        seen_uids[puid],
                    )
                    continue
                seen_names.add(pname)
                if puid:
                    seen_uids[puid] = pname
                self.providers.append(
                    LLMProvider(
                        name=pname,
                        uid=p.get("uid", ""),
                        endpoint=p.get("endpoint", ""),
                        model=p.get("model", ""),
                        api_key_env=p.get("api_key_env", ""),
                        priority=p.get("priority", 99),
                        use_for=p.get("use_for", ["fallback"]),
                        routing_tier=p.get("routing_tier", "any"),
                        routing_tags=p.get("routing_tags", []),
                        requires_vpn=p.get("requires_vpn", False),
                        verify_ssl=p.get("verify_ssl", True),
                        tool_mode=p.get("tool_mode", "auto"),
                        max_tokens_default=p.get("max_tokens_default", 0),
                        transport=p.get("transport", "direct"),
                    )
                )

            # Sort by priority
            self.providers.sort(key=lambda p: p.priority)

            # Log the loaded provider identities for the session audit record
            for provider in self.providers:
                log.info(
                    "Provider loaded: %s (uid=%s, config_hash=%s, instance=%s)",
                    provider.name,
                    provider.uid[:8],
                    provider.config_hash,
                    provider.instance_id,
                )

            # Activate EC audit mode if any EC providers are configured
            ec_count = sum(1 for p in self.providers if p.routing_tier == "export_controlled")
            try:
                from axiom.infra.audit_log import AuditLog

                audit = AuditLog.get()
                if ec_count > 0:
                    try:
                        audit.set_mode("ec")
                        self._ec_audit_enabled = True
                    except ValueError:
                        log.warning(
                            "EC providers configured but AXIOM_AUDIT_HMAC_KEY is not set. "
                            "EC requests will be blocked. Run 'axi setup audit-key'."
                        )
                        self._ec_audit_enabled = False
                else:
                    self._ec_audit_enabled = False
                audit.write_config_load(
                    config_file=str(models_path),
                    providers=[p.identity for p in self.providers],
                    ec_providers_count=ec_count,
                )
            except Exception as exc:
                log.warning("audit_log config_load write failed: %s", exc)

        except Exception as e:
            print(f"Warning: Could not load llm-providers.toml: {e}", file=sys.stderr)

        # Auto-discover local LLM server (bundled llamafile via K3D) if no providers loaded
        self._discover_local_llm()

    def _discover_local_llm(self) -> None:
        """Probe for a local LLM server on the default port.

        If an OpenAI-compatible server is running at localhost:8080 and no
        other providers are configured, register it as a fallback provider.
        This enables zero-config LLM access from the embedded local model
        deployed by `axi config` / `axi infra`.

        The model identifier is sourced from
        ``axiom.setup.llamafile.DEFAULT_LOCAL_MODEL_ID`` — single source of
        truth, so swapping the bundled default (qwen ↔ bonsai) doesn't
        require touching the gateway.

        TBD in feature/http-raw-bypass: a `GET /v1/info` endpoint will let
        the gateway query the running llamafile for its actual model id
        rather than relying on the build-time constant.
        """
        # Local import — keeps the gateway loadable even if setup is absent
        # in some build configurations. Falls back to the legacy literal.
        try:
            from axiom.setup.llamafile import DEFAULT_LOCAL_MODEL_ID
        except Exception:
            DEFAULT_LOCAL_MODEL_ID = "qwen2.5-7b-instruct"

        # Don't add if we already have a provider for this endpoint
        if any("localhost:8080" in (p.endpoint or "") for p in self.providers):
            return

        import socket

        try:
            with socket.create_connection(("localhost", 8080), timeout=0.5):
                reachable = True
        except OSError:
            reachable = False

        if reachable:
            self.providers.append(
                LLMProvider(
                    name="axiom-local",
                    uid=f"axiom-local-{DEFAULT_LOCAL_MODEL_ID}",
                    endpoint="http://localhost:8080",
                    model=DEFAULT_LOCAL_MODEL_ID,
                    api_key_env="",  # no key needed
                    priority=50,
                    use_for=["fallback", "diagnosis", "classification"],
                    routing_tier="public",
                    routing_tags=[],
                    requires_vpn=False,
                    verify_ssl=False,
                    max_tokens_default=2048,
                )
            )
            # Re-sort by priority
            self.providers.sort(key=lambda p: p.priority)
            log.info("Auto-discovered local LLM server at localhost:8080")

    @property
    def available(self) -> bool:
        """Whether any usable providers are configured.

        A provider is usable if it has an API key OR if it's a local
        server (no key required, identified by empty api_key_env).
        """
        return any(p.api_key or not p.api_key_env for p in self.providers)

    @property
    def active_provider(self) -> LLMProvider | None:
        """Return the first usable provider, or None."""
        for p in self.providers:
            if p.api_key or not p.api_key_env:
                return p
        return None

    # ------------------------------------------------------------------
    # Original complete() — unchanged for backward compatibility
    # ------------------------------------------------------------------

    def complete(
        self,
        prompt: str,
        system: str = "",
        task: str = "extraction",
        max_tokens: int = 2000,
        tier_hint: str | None = None,
    ) -> GatewayResponse:
        """Send a completion request to the resolved provider.

        Routing per spec-model-routing §14:
        - When `[gateway].default_routing == "auto"` and no provider
          override is set, resolve via ModelStrategy (`auto_strategy`).
        - Otherwise (or as fallback) use the legacy `_select_provider`
          path, which honors `--provider` overrides and the
          `routing.prefer_provider` chain.

        `tier_hint` (simple|standard|smart|smartest) is threaded into the
        ModelContext per §14.3 and may bias survivor selection in future
        strategies; ignored by today's built-ins.
        """
        provider = self._select_with_routing(task, tier_hint=tier_hint)
        if provider is None:
            names = ", ".join(p.name for p in self.providers) or "(none configured)"
            return GatewayResponse(
                text="LLM extraction unavailable — raw text preserved in signal.",
                provider="stub",
                success=False,
                error=f"No usable LLM provider for task={task!r}. Configured: {names}.",
            )
        candidates = self._ordered_candidates(provider)
        try:
            return self._attempt_providers(
                candidates,
                lambda p: self._call_provider(p, prompt, system, max_tokens),
            )
        except LLMGatewayError as e:
            # Real, self-describing reason (status+body, which providers). Keep
            # the graceful-degradation contract (caller relies on the stub +
            # preserved raw text) but make the failure self-explanatory.
            print(f"Warning: {e}", file=sys.stderr)
            return GatewayResponse(
                text="LLM extraction unavailable — raw text preserved in signal.",
                provider="stub",
                success=False,
                error=str(e),
            )

    def _select_with_routing(
        self,
        task: str,
        *,
        routing_tier: str = "any",
        required_tags: set[str] | None = None,
        tier_hint: str | None = None,
    ) -> LLMProvider | None:
        """Dispatch between auto-strategy and legacy `_select_provider`.

        Provider override (set_provider_override) is honored in both paths
        via `_select_provider` step 1, so it always wins.
        """
        from axiom.agents.strategy.strategy import ModelStrategyUnsatisfiable

        mode = self._gateway_config.get("default_routing", "pinned")
        if mode == "auto" and not self._provider_override:
            try:
                picked = self._resolve_via_strategy(
                    task,
                    routing_tier=routing_tier,
                    required_tags=required_tags,
                    tier_hint=tier_hint,
                )
                if picked is not None:
                    return picked
            except ModelStrategyUnsatisfiable as exc:
                log.warning(
                    "auto-strategy unsatisfiable (%s); falling back to legacy selection",
                    exc,
                )
            # Strategy returned None or raised — fall through to legacy.

        return self._select_provider(
            task, routing_tier=routing_tier, required_tags=required_tags
        )

    def _ordered_candidates(
        self,
        primary: LLMProvider,
        *,
        routing_tier: str = "any",
    ) -> list[LLMProvider]:
        """Ordered fallback list: the resolved provider first, then every other
        usable provider compatible with ``routing_tier`` in priority order.

        EC requests never widen to non-EC providers (compliance) — they fall
        back only among other export_controlled providers.

        RATIONALIZE-4: when ``primary`` is a LiteLLM router group
        (``transport == "litellm"``) the within-tier mechanics — fallback,
        retry, load-balance — belong to LiteLLM, not Axiom. We return ONLY the
        group so the gateway makes a single call to the group endpoint and lets
        LiteLLM fan out across the group's members. Axiom keeps the policy seam
        above this (tier/tag selection + EC-never-relaxes); it just stops
        double-doing the transport's job.
        """
        if primary.transport == "litellm":
            return [primary]

        ordered = [primary]
        seen = {primary.name}
        for p in self.providers:
            if p.name in seen:
                continue
            if not (p.api_key or not p.api_key_env):
                continue
            if routing_tier == "export_controlled" and p.routing_tier != "export_controlled":
                continue
            ordered.append(p)
            seen.add(p.name)
        return ordered

    def _attempt_providers(
        self,
        providers: list[LLMProvider],
        call,
    ):
        """Try each provider in order, capturing the REAL failure reason per
        provider. Returns the first success.

        Behavior (mirrors embeddings.py error semantics):
          * PersistentLLMError (4xx — bad auth/model/payload): do NOT silently
            fall back. Re-raise immediately so the real reason surfaces.
          * TransientLLMError / other (network/timeout/429/5xx): record the
            real status+body and try the next provider.
          * All providers exhausted → raise TransientLLMError naming WHICH
            providers failed and WHY (no generic "LLM unavailable").

        ``call`` is ``(provider) -> response``. Raises on total failure; the
        caller decides whether to convert to a graceful stub or propagate.
        """
        failures: list[LLMGatewayError] = []
        for provider in providers:
            try:
                return call(provider)
            except PersistentLLMError as exc:
                exc.provider = exc.provider or provider.name
                log.warning(
                    "Provider %s rejected request (persistent, HTTP %s): %s",
                    provider.name, exc.status, (exc.body or "")[:200],
                )
                raise
            except Exception as exc:  # noqa: BLE001 — classify everything else
                err = _classify_http_error(exc, provider=provider.name)
                if isinstance(err, PersistentLLMError):
                    log.warning(
                        "Provider %s rejected request (persistent, HTTP %s): %s",
                        provider.name, err.status, (err.body or "")[:200],
                    )
                    raise err from exc
                failures.append(err)
                log.warning(
                    "Provider %s failed transiently (%s) — trying next provider",
                    provider.name, err,
                )
        # Every provider failed transiently — name each one and why.
        reasons = "; ".join(str(f) for f in failures) or "no providers attempted"
        names = ", ".join(p.name for p in providers) or "(none)"
        raise TransientLLMError(
            f"all LLM providers failed (tried: {names}) — {reasons}",
            provider="all",
        )

    def _call_provider(
        self,
        provider: LLMProvider,
        prompt: str,
        system: str,
        max_tokens: int,
    ) -> GatewayResponse:
        """Call a specific provider using the OpenAI chat completions format."""
        try:
            import requests
        except ImportError:
            raise RuntimeError("requests library required for LLM calls")

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"

        # Handle Anthropic's different API format
        if "anthropic" in provider.endpoint.lower():
            headers = {
                "x-api-key": provider.api_key or "",
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            payload = {
                "model": provider.model,
                "max_tokens": _floor_max_tokens(provider, max_tokens),
                "messages": [{"role": "user", "content": prompt}],
            }
            if system:
                payload["system"] = system

            url = provider.endpoint.rstrip("/") + "/messages"
            response = _post_with_rate_limit_retry(requests, url, payload, headers)
            data = response.json()
            text = data.get("content", [{}])[0].get("text", "")
        else:
            # Standard OpenAI-compatible format
            payload = {
                "model": provider.model,
                "messages": messages,
                "max_tokens": _floor_max_tokens(provider, max_tokens),
            }

            url = provider.endpoint.rstrip("/") + "/chat/completions"
            response = _post_with_rate_limit_retry(requests, url, payload, headers)
            data = response.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")

        return GatewayResponse(
            text=text,
            provider=provider.name,
            model=provider.model,
            success=True,
        )

    # ------------------------------------------------------------------
    # New: Native tool-use (non-streaming)
    # ------------------------------------------------------------------

    def complete_with_tools(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        task: str = "chat",
        routing_tier: str = "any",
        routing_tags: set[str] | None = None,
        routing_decision: RoutingDecision | None = None,
        prefer: str | None = None,
    ) -> CompletionResponse:
        """Send a completion request with native tool-use support.

        Args:
            messages: Conversation history in API format (list of role/content dicts).
            system: System prompt.
            tools: Tool definitions in OpenAI function-calling format.
            max_tokens: Maximum tokens to generate.
            task: Task type for provider selection.
            routing_tier:  "public" | "export_controlled" | "any"
            routing_tags:  Optional set of facility-policy tags that the selected
                           provider must carry (e.g. {"restricted"}, {"internal_compute"}).
                           None = no tag filter.
            routing_decision:
                Optional ``RoutingDecision`` produced by the upstream
                router. When provided and a request is blocked, the
                user-visible error surfaces the matched keyword(s) and
                classifier stage so the user immediately knows *why*
                routing fired and *which* classifier decided —
                without needing to ``axi log routing``.

        Returns:
            CompletionResponse with text and tool_use blocks separated.
        """
        provider = self._select_provider(task, routing_tier, routing_tags, prefer=prefer)
        if provider is None:
            if routing_tier == "export_controlled":
                return CompletionResponse(
                    text=_format_ec_block_message(routing_decision),
                    provider="stub",
                    success=False,
                    error="EC_PROVIDER_NOT_CONFIGURED",
                )
            names = ", ".join(p.name for p in self.providers) or "(none configured)"
            return CompletionResponse(
                text="LLM unavailable — no providers configured.",
                provider="stub",
                success=False,
                error=(
                    f"No usable LLM provider for task={task!r}, tier={routing_tier!r}. "
                    f"Configured: {names}."
                ),
            )

        is_ec = routing_tier == "export_controlled"

        if provider.requires_vpn:
            import time as _time

            vpn_start = _time.monotonic()
            vpn_ok = self._check_vpn(provider)
            vpn_ms = int((_time.monotonic() - vpn_start) * 1000)
            try:
                from axiom.infra.audit_log import AuditLog

                AuditLog.get().write_vpn(
                    routing_event_id=str(__import__("uuid").uuid4()),
                    provider_name=provider.name,
                    vpn_reachable=vpn_ok,
                    check_duration_ms=vpn_ms,
                )
            except Exception:
                pass
            if not vpn_ok:
                return self._handle_vpn_unavailable(provider, task, routing_tier)

        try:
            import hashlib as _hashlib

            user_text = " ".join(m.get("content", "") for m in messages if m.get("role") == "user")
            prompt_hash = _hashlib.sha256(user_text.encode()).hexdigest()

            # ── System prompt hardening for EC sessions ─────────────────────
            effective_system = system
            if is_ec:
                effective_system = _harden_system_prompt(system)

            response = self._call_provider_with_tools(
                provider, messages, effective_system, tools, max_tokens
            )

            # ── Response scanning ────────────────────────────────────────────
            if response.text:
                response = _scan_response(response, routing_tier, provider.name, prompt_hash)

            response_hash = (
                _hashlib.sha256(response.text.encode()).hexdigest() if response.text else None
            )
            try:
                from axiom.infra.audit_log import AuditLog
                from axiom.infra.trace import current_session

                AuditLog.get().write_routing(
                    session_id=current_session(),
                    tier_requested=routing_tier,
                    tier_assigned=provider.routing_tier,
                    provider_name=provider.name,
                    provider_tier=provider.routing_tier,
                    blocked=False,
                    block_reason=None,
                    prompt_hash=prompt_hash,
                    response_hash=response_hash,
                    ec_violation=False,
                    is_ec=is_ec,
                )
            except Exception:
                pass
            return response
        except Exception as e:
            # Capture the REAL upstream reason (status + body) instead of a
            # generic "provider call failed". Persistent (4xx) vs transient
            # (network/429/5xx) is recorded in the error type + message so the
            # caller and logs see why, not a guess.
            err = _classify_http_error(e, provider=provider.name)
            kind = "persistent" if isinstance(err, PersistentLLMError) else "transient"
            print(f"Warning: provider {provider.name} failed ({kind}): {err}", file=sys.stderr)
            return CompletionResponse(
                text="LLM unavailable — provider call failed.",
                provider="stub",
                success=False,
                error=str(err),
            )

    # ---------------------------------------------------------------------------
    # Security helpers — system prompt hardening + response scanning
    # ---------------------------------------------------------------------------

    def _handle_vpn_unavailable(
        self, vpn_provider: LLMProvider, task: str, routing_tier: str
    ) -> CompletionResponse:
        """Handle VPN model unreachable — clear guidance on reconnecting."""
        from axiom.extensions.builtins.settings.store import SettingsStore

        try:
            policy = SettingsStore().get("routing.on_vpn_unavailable", "warn")
        except Exception:
            policy = "warn"

        # Pull VPN-specific guidance from the connection registry
        vpn_name = ""
        connect_guide = ""
        try:
            from axiom.infra.connections import get_registry

            conn = get_registry().get(vpn_provider.name)
            if conn:
                vpn_name = conn.vpn_name
                connect_guide = conn.vpn_connect_guide
        except Exception:
            pass

        # Build clear, concise message
        provider_label = vpn_name or vpn_provider.name
        lines = [
            f"Cannot reach {provider_label} — VPN not connected.",
            "",
        ]
        if connect_guide:
            lines.append(f"  To connect: {connect_guide}")
        else:
            lines.append("  Connect to your facility VPN and retry.")
        lines.append("")
        lines.append("  Your query was classified as export-controlled and requires")
        lines.append(f"  the private endpoint ({vpn_provider.name}) which is VPN-gated.")
        lines.append("")
        lines.append("  Options:")
        lines.append("    1. Connect to VPN and retry")
        lines.append("    2. Rephrase as a general (non-EC) question")
        lines.append("    3. Use --mode public to force public routing (no EC data)")

        msg = "\n".join(lines)

        if policy == "fail":
            return CompletionResponse(
                text=msg,
                provider="stub",
                success=False,
                error="VPN not connected.",
            )

        # "warn" — fall back to public tier with warning
        fallback = self._select_provider(task, "public")
        if fallback is None:
            return CompletionResponse(
                text=msg,
                provider="stub",
                success=False,
                error="VPN not connected, no public provider available.",
            )

        print(msg, file=sys.stderr)
        return CompletionResponse(
            text=msg,
            provider="stub",
            success=False,
            error="VPN not connected.",
        )

    def _call_provider_with_tools(
        self,
        provider: LLMProvider,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> CompletionResponse:
        """Call a provider with native tool-use params and normalize the response."""
        try:
            import requests  # noqa: F401
        except ImportError:
            raise RuntimeError("requests library required for LLM calls")

        is_anthropic = "anthropic" in provider.endpoint.lower()

        if is_anthropic:
            return self._call_anthropic_with_tools(provider, messages, system, tools, max_tokens)
        else:
            return self._call_openai_with_tools(provider, messages, system, tools, max_tokens)

    def _call_anthropic_with_tools(
        self,
        provider: LLMProvider,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> CompletionResponse:
        import requests

        headers = {
            "x-api-key": provider.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        # Convert messages to Anthropic format
        api_messages = _messages_to_anthropic_format(messages)

        payload: dict[str, Any] = {
            "model": provider.model,
            "max_tokens": _floor_max_tokens(provider, max_tokens),
            "messages": api_messages,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = _tools_to_anthropic_format(tools)

        url = provider.endpoint.rstrip("/") + "/messages"

        try:
            response = _post_with_rate_limit_retry(requests, url, payload, headers, timeout=120)
        except Exception as e:
            # Fall back to no-tools call if tools param rejected
            if tools:
                payload.pop("tools", None)
                try:
                    response = _post_with_rate_limit_retry(
                        requests, url, payload, headers, timeout=120
                    )
                except Exception:
                    raise e
            else:
                raise

        data = response.json()
        text_parts = []
        tool_blocks = []

        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_blocks.append(
                    ToolUseBlock(
                        tool_id=block.get("id", ""),
                        name=block.get("name", ""),
                        input=block.get("input", {}),
                    )
                )

        usage = data.get("usage", {})
        return CompletionResponse(
            text="\n".join(text_parts),
            tool_use=tool_blocks,
            provider=provider.name,
            model=provider.model,
            success=True,
            stop_reason=data.get("stop_reason", ""),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
        )

    def _call_openai_with_tools(
        self,
        provider: LLMProvider,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> CompletionResponse:
        import requests

        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"

        api_messages = list(messages)
        if system and not any(m.get("role") == "system" for m in api_messages):
            api_messages.insert(0, {"role": "system", "content": system})

        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": api_messages,
            "max_tokens": _floor_max_tokens(provider, max_tokens),
        }
        if tools:
            payload["tools"] = tools

        url = provider.endpoint.rstrip("/") + "/chat/completions"
        ssl_verify = provider.verify_ssl

        try:
            response = _post_with_rate_limit_retry(
                requests, url, payload, headers, timeout=180, verify=ssl_verify
            )
        except Exception as e:
            # Fall back to no-tools call if tools param rejected
            if tools:
                payload.pop("tools", None)
                try:
                    response = _post_with_rate_limit_retry(
                        requests, url, payload, headers, timeout=180, verify=ssl_verify
                    )
                except Exception:
                    raise e
            else:
                raise

        data = response.json()
        choice = data.get("choices", [{}])[0]
        message = choice.get("message", {})
        text = message.get("content", "") or ""

        # Qwen3 / reasoning models: answer is in `content`, chain-of-thought in
        # `reasoning_content`.  If content is empty the response was cut before
        # the model finished reasoning — surface the reasoning so the user isn't
        # left with a blank response.
        if not text.strip() and message.get("reasoning_content"):
            reasoning = message["reasoning_content"]
            finish = choice.get("finish_reason", "")
            if finish == "length":
                text = f"[Response truncated during reasoning — increase max_tokens]\n\n{reasoning}"
            else:
                text = reasoning

        tool_blocks = []

        for tc in message.get("tool_calls", []):
            func = tc.get("function", {})
            try:
                args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}
            tool_blocks.append(
                ToolUseBlock(
                    tool_id=tc.get("id", ""),
                    name=func.get("name", ""),
                    input=args,
                )
            )

        usage = data.get("usage", {})
        return CompletionResponse(
            text=text,
            tool_use=tool_blocks,
            provider=provider.name,
            model=provider.model,
            success=True,
            stop_reason=choice.get("finish_reason", ""),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
        )

    # ------------------------------------------------------------------
    # New: Streaming with tool-use
    # ------------------------------------------------------------------

    def stream_with_tools(
        self,
        messages: list[dict[str, Any]],
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        max_tokens: int = 4096,
        task: str = "chat",
        routing_tier: str = "any",
        prefer: str | None = None,
    ) -> Iterator[StreamChunk]:
        """Stream a completion with native tool-use support.

        Yields StreamChunk objects as tokens arrive via SSE.
        Falls back to non-streaming complete_with_tools() if streaming fails.
        """
        provider = self._select_provider(task, routing_tier, prefer=prefer)
        if provider is None:
            yield StreamChunk(type="text", text="LLM unavailable — no providers configured.")
            yield StreamChunk(type="done")
            return

        if provider.requires_vpn and not self._check_vpn(provider):
            result = self._handle_vpn_unavailable(provider, task, routing_tier)
            yield StreamChunk(type="text", text=result.text)
            yield StreamChunk(type="done")
            return

        try:
            yield from self._stream_provider(provider, messages, system, tools, max_tokens)
        except Exception as e:
            err = _classify_http_error(e, provider=provider.name)
            kind = "persistent" if isinstance(err, PersistentLLMError) else "transient"
            print(
                f"Warning: streaming from {provider.name} failed ({kind}): {err}",
                file=sys.stderr,
            )
            # Surface the real reason in the stream so the user isn't left with
            # a bare "unavailable" — but use the client-safe summary (provider +
            # status + message, no raw upstream body). The full body went to
            # stderr above; echoing it to the HTTP client risks leaking an
            # upstream's internal hostnames / DSNs / auth hints (SRV-033).
            yield StreamChunk(type="text", text=f"LLM stream failed: {err.client_summary()}")
            yield StreamChunk(type="done")

    def _stream_provider(
        self,
        provider: LLMProvider,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> Iterator[StreamChunk]:
        """SSE stream from a provider, yielding StreamChunk objects."""
        try:
            import requests  # noqa: F401
        except ImportError:
            raise RuntimeError("requests library required for LLM calls")

        is_anthropic = "anthropic" in provider.endpoint.lower()

        if is_anthropic:
            yield from self._stream_anthropic(provider, messages, system, tools, max_tokens)
        else:
            yield from self._stream_openai(provider, messages, system, tools, max_tokens)

    def _stream_anthropic(
        self,
        provider: LLMProvider,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> Iterator[StreamChunk]:
        import requests

        headers = {
            "x-api-key": provider.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        api_messages = _messages_to_anthropic_format(messages)
        payload: dict[str, Any] = {
            "model": provider.model,
            "max_tokens": _floor_max_tokens(provider, max_tokens),
            "messages": api_messages,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = _tools_to_anthropic_format(tools)

        url = provider.endpoint.rstrip("/") + "/messages"
        response = _post_with_rate_limit_retry(
            requests, url, payload, headers, timeout=120, stream=True
        )

        current_tool_id = ""
        current_tool_name = ""
        tool_input_buf = ""
        in_thinking = False

        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break

            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype == "content_block_start":
                block = event.get("content_block", {})
                if block.get("type") == "tool_use":
                    current_tool_id = block.get("id", "")
                    current_tool_name = block.get("name", "")
                    tool_input_buf = ""
                    yield StreamChunk(
                        type="tool_use_start",
                        tool_id=current_tool_id,
                        tool_name=current_tool_name,
                    )
                elif block.get("type") == "thinking":
                    in_thinking = True
                    yield StreamChunk(type="thinking_start")

            elif etype == "content_block_delta":
                delta = event.get("delta", {})
                if delta.get("type") == "text_delta":
                    yield StreamChunk(type="text", text=delta.get("text", ""))
                elif delta.get("type") == "input_json_delta":
                    partial = delta.get("partial_json", "")
                    tool_input_buf += partial
                    yield StreamChunk(
                        type="tool_input_delta",
                        tool_id=current_tool_id,
                        tool_input_json=partial,
                    )
                elif delta.get("type") == "thinking_delta":
                    yield StreamChunk(
                        type="thinking_delta",
                        text=delta.get("thinking", ""),
                    )

            elif etype == "content_block_stop":
                if in_thinking:
                    in_thinking = False
                    yield StreamChunk(type="thinking_end")
                elif current_tool_name:
                    yield StreamChunk(
                        type="tool_use_end",
                        tool_id=current_tool_id,
                        tool_name=current_tool_name,
                        tool_input_json=tool_input_buf,
                    )
                    current_tool_id = ""
                    current_tool_name = ""
                    tool_input_buf = ""

            elif etype == "message_delta":
                # Extract usage from message_delta (Anthropic sends it here)
                usage = event.get("usage", {})
                if usage:
                    yield StreamChunk(
                        type="usage",
                        input_tokens=usage.get("input_tokens", 0),
                        output_tokens=usage.get("output_tokens", 0),
                        cache_read_tokens=usage.get("cache_read_tokens", 0),
                    )

            elif etype == "message_start":
                # Extract input tokens from message_start
                msg = event.get("message", {})
                usage = msg.get("usage", {})
                if usage.get("input_tokens"):
                    yield StreamChunk(
                        type="usage",
                        input_tokens=usage.get("input_tokens", 0),
                        cache_read_tokens=usage.get("cache_read_tokens", 0),
                    )

            elif etype == "message_stop":
                break

        yield StreamChunk(type="done")

    def _stream_openai(
        self,
        provider: LLMProvider,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None,
        max_tokens: int,
    ) -> Iterator[StreamChunk]:
        import requests

        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"

        api_messages = list(messages)
        if system and not any(m.get("role") == "system" for m in api_messages):
            api_messages.insert(0, {"role": "system", "content": system})

        payload: dict[str, Any] = {
            "model": provider.model,
            "messages": api_messages,
            "max_tokens": _floor_max_tokens(provider, max_tokens),
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        url = provider.endpoint.rstrip("/") + "/chat/completions"
        response = _post_with_rate_limit_retry(
            requests, url, payload, headers, timeout=120, stream=True
        )

        # Track tool call state across deltas
        tool_calls_buf: dict[int, dict[str, str]] = {}  # index -> {id, name, args}
        in_thinking = False

        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break

            try:
                event = json.loads(data_str)
            except json.JSONDecodeError:
                continue

            choices = event.get("choices", [])
            if not choices:
                continue

            delta = choices[0].get("delta", {})

            # Reasoning (gpt-oss / OpenAI-compat reasoning models) -> thinking,
            # so clients show activity during the reasoning phase instead of dead
            # air through prefill + reasoning.
            if delta.get("reasoning_content"):
                if not in_thinking:
                    in_thinking = True
                    yield StreamChunk(type="thinking_start")
                yield StreamChunk(type="thinking_delta", text=delta["reasoning_content"])

            # Text content
            if delta.get("content"):
                if in_thinking:
                    in_thinking = False
                    yield StreamChunk(type="thinking_end")
                yield StreamChunk(type="text", text=delta["content"])

            # Tool calls
            for tc_delta in delta.get("tool_calls", []):
                idx = tc_delta.get("index", 0)
                if idx not in tool_calls_buf:
                    tool_calls_buf[idx] = {"id": "", "name": "", "args": ""}

                buf = tool_calls_buf[idx]

                if tc_delta.get("id"):
                    buf["id"] = tc_delta["id"]
                func = tc_delta.get("function", {})
                if func.get("name"):
                    buf["name"] = func["name"]
                    yield StreamChunk(
                        type="tool_use_start",
                        tool_id=buf["id"],
                        tool_name=buf["name"],
                    )
                if func.get("arguments"):
                    buf["args"] += func["arguments"]
                    yield StreamChunk(
                        type="tool_input_delta",
                        tool_id=buf["id"],
                        tool_input_json=func["arguments"],
                    )

            # Check for finish
            finish = choices[0].get("finish_reason")
            if finish:
                if in_thinking:
                    in_thinking = False
                    yield StreamChunk(type="thinking_end")
                for idx, buf in tool_calls_buf.items():
                    if buf["name"]:
                        yield StreamChunk(
                            type="tool_use_end",
                            tool_id=buf["id"],
                            tool_name=buf["name"],
                            tool_input_json=buf["args"],
                        )
                break

        yield StreamChunk(type="done")


# ------------------------------------------------------------------
# Security helpers — module-level, called from Gateway methods
# ------------------------------------------------------------------


# Map RoutingDecision.classifier → numbered stage label for user-visible
# messages. The bare ``classifier`` token is kept stable for audit logs +
# downstream tooling; the stage label below is purely a presentation
# affordance so users can correlate "stage-1-keyword" in the error message
# with the layered pipeline documented in router.py.
_CLASSIFIER_STAGE_LABELS = {
    "session": "stage-0-session",
    "keyword": "stage-1-keyword",
    "ollama":  "stage-2-ollama",
    "fallback": "stage-3-fallback",
}

# How many matched terms to render verbatim before truncating to
# "(+N more)". Bounds the message length and avoids leaking long
# term lists into the user surface.
_EC_BLOCK_TERMS_PREVIEW = 3


def _classifier_stage_label(classifier: str) -> str:
    """Return the user-facing stage label for a classifier token."""
    return _CLASSIFIER_STAGE_LABELS.get(classifier, classifier)


def _format_ec_block_message(decision: RoutingDecision | None = None) -> str:
    """Build the user-visible message shown when an EC routing decision
    cannot be served (no EC provider configured, etc.).

    When ``decision`` is provided, the message surfaces:

      * the matched keyword(s) (``decision.matched_terms``)
      * the classifier name as a stage label (``decision.classifier``)

    so the user immediately knows *why* the request was blocked and
    *which stage* of the pipeline made the call — no follow-up
    ``axi log routing`` round-trip required for the common case.

    The message is intentionally domain-agnostic: it mentions
    "private endpoint" / "export-controlled content" without naming
    any specific consumer, facility, or domain (per the axiom
    domain-agnostic rule).
    """
    # Header — what happened, in one line
    if decision is not None:
        stage = _classifier_stage_label(decision.classifier)
        if decision.matched_terms:
            preview = ", ".join(
                f"'{t}'" for t in decision.matched_terms[:_EC_BLOCK_TERMS_PREVIEW]
            )
            extra = len(decision.matched_terms) - _EC_BLOCK_TERMS_PREVIEW
            tail = f" (+{extra} more)" if extra > 0 else ""
            header = (
                f"[EXPORT_CONTROLLED] Routed to private endpoint: "
                f"matched {preview}{tail} via {stage} classifier."
            )
        else:
            # ollama / fallback / session — no keyword to surface, but the
            # stage label still tells the user which classifier decided.
            header = (
                f"[EXPORT_CONTROLLED] Routed to private endpoint: "
                f"classified by {stage} classifier."
            )
    else:
        header = (
            "[EXPORT_CONTROLLED] Routed to private endpoint: "
            "this query was classified as export-controlled content."
        )

    body = [
        header,
        "",
        "No export-controlled LLM is configured, so the request cannot be",
        "sent to a public cloud provider (Anthropic, OpenAI, etc.).",
        "",
        "For details:    axi log routing",
        "To allowlist:   edit runtime/config/routing_allowlist.txt",
        "",
        "If your deployment legitimately needs to handle export-controlled",
        "content, configure a private-network LLM with",
        '  routing_tier = "export_controlled"   in llm-providers.toml',
        '  (legacy alias: "restricted" — still accepted but deprecated)',
        "",
        "Contact your administrator or see:  axi connect --help",
    ]
    return "\n".join(body)


def _harden_system_prompt(original: str) -> str:
    """Prepend the non-negotiable EC security preamble to the system prompt.

    Preamble is loaded from the PromptRegistry (id: "ec_hardened_preamble").
    Falls back to a hardcoded default if the registry is unavailable.
    """
    try:
        from axiom.infra.prompt_registry import get_registry

        preamble = get_registry().resolve("ec_hardened_preamble").content
    except Exception:
        preamble = (
            "[SECURITY POLICY — NON-NEGOTIABLE]\n"
            "You are operating in an export-controlled (EC) session. "
            "Do not reproduce or transmit controlled technical data.\n"
            "[END SECURITY POLICY]\n"
        )
    return preamble + "\n\n" + original


def _scan_response(
    response: CompletionResponse,
    routing_tier: str,
    provider_name: str,
    prompt_hash: str,
) -> CompletionResponse:
    """Scan an LLM response for classified terms. Returns (possibly modified) response."""
    try:
        import hashlib as _hashlib

        from axiom.infra.security_log import SecurityLog
        from axiom.infra.trace import current_session
        from axiom.llm.router import QueryRouter

        router = QueryRouter.__new__(QueryRouter)
        router._terms = None
        router._allowlist = None
        router._ollama = None  # type: ignore[assignment]
        matched = router._keyword_check(response.text)

        if matched:
            response_hash = _hashlib.sha256(response.text.encode()).hexdigest()
            SecurityLog.get().response_scan_hit(
                session_id=current_session(),
                provider_name=provider_name,
                routing_tier=routing_tier,
                matched_terms=matched,
                prompt_hash=prompt_hash,
                response_hash=response_hash,
                warning_prepended=True,
            )
            tier_label = "public" if routing_tier != "export_controlled" else "EC"
            warning = (
                f"[SECURITY WARNING — Response scan detected {len(matched)} classified "
                f"term(s) in this {tier_label} LLM response: "
                f"{', '.join(matched[:3])}{'…' if len(matched) > 3 else ''}. "
                f"This event has been logged and flagged for review.]\n\n"
            )
            return CompletionResponse(
                text=warning + response.text,
                tool_use=response.tool_use,
                provider=response.provider,
                model=response.model,
                success=response.success,
                error=response.error,
                stop_reason=response.stop_reason,
                input_tokens=response.input_tokens,
                output_tokens=response.output_tokens,
                cache_read_tokens=response.cache_read_tokens,
            )
    except Exception:
        pass
    return response


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _messages_to_anthropic_format(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert OpenAI-style messages to Anthropic messages format.

    Handles:
    - Strips system messages (Anthropic uses top-level system param)
    - Converts assistant messages with tool_calls → content blocks with tool_use
    - Converts role:"tool" messages → role:"user" with tool_result content blocks
    - Merges consecutive same-role messages (Anthropic requires alternating roles)
    """
    result: list[dict[str, Any]] = []

    for msg in messages:
        role = msg.get("role", "")

        # Skip system messages
        if role == "system":
            continue

        # Assistant with tool_calls → Anthropic content blocks
        if role == "assistant" and msg.get("tool_calls"):
            content_blocks: list[dict[str, Any]] = []
            text = msg.get("content", "")
            if text:
                content_blocks.append({"type": "text", "text": text})
            for tc in msg["tool_calls"]:
                func = tc.get("function", {})
                try:
                    input_data = json.loads(func.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    input_data = {}
                content_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": func.get("name", ""),
                        "input": input_data,
                    }
                )
            result.append({"role": "assistant", "content": content_blocks})
            continue

        # Tool result → Anthropic user message with tool_result block
        if role == "tool":
            tool_result_block = {
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": msg.get("content", ""),
            }
            # Merge with previous user message if it's also tool results
            if (
                result
                and result[-1]["role"] == "user"
                and isinstance(result[-1].get("content"), list)
            ):
                result[-1]["content"].append(tool_result_block)
            else:
                result.append({"role": "user", "content": [tool_result_block]})
            continue

        # Regular user/assistant message
        result.append({"role": role, "content": msg.get("content", "")})

    return result


def _tools_to_anthropic_format(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convert OpenAI function-calling tool defs to Anthropic format.

    OpenAI: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
    Anthropic: {"name": ..., "description": ..., "input_schema": ...}
    """
    result = []
    for t in tools:
        func = t.get("function", t)
        result.append(
            {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return result


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """Parse a single SSE data line into a JSON dict, or None."""
    if not line or not line.startswith("data: "):
        return None
    data_str = line[6:]
    if data_str.strip() == "[DONE]":
        return None
    try:
        return json.loads(data_str)
    except json.JSONDecodeError:
        return None
