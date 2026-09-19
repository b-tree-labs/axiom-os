# ADR-012: Provider Identity — Three-Layer Model for Stable Forensic Correlation

**Status:** Accepted
**Date:** 2026-03-19
**Owner:** Ben Booth

---

## Context

Axiom routes LLM requests across multiple providers simultaneously: public
cloud providers (Anthropic, OpenAI), private-network EC providers (Qwen on PrivateCloud
private-server), and facility-internal providers (on-premises Llama, Ollama instances).
Beyond LLM providers, the platform has many other configurable providers: log
sinks, storage providers, signal sources, publishers, issue trackers, and
embedding providers. Every routing decision, audit record, log event, signal
payload, and cost attribution record includes a provider identifier.

Prior to this ADR, `LLMProvider.name` was the only identifier — and no
equivalent identity existed for other provider types:
- User-defined string, no uniqueness enforcement
- Mutable — renaming a provider in config silently breaks historical log
  correlation without any warning
- No way to detect config drift — if an operator changes `endpoint` or `model`
  while keeping `name` the same, audit records look identical before and after
- No way to distinguish "same provider, different load event" in a forensic
  timeline (e.g., a hot-reload between two sessions)
- No shared contract — each provider type invented its own naming conventions,
  making cross-provider forensic correlation inconsistent

**Risk observed:**
- Two providers with the same `name` in `llm-providers.toml` → one silently
  dropped with no error raised (previously: both loaded, second overwrote first
  in iteration order)
- Operator renames `"qwen-private-server"` to `"qwen-ec"` for clarity → all prior
  audit records for `"qwen-private-server"` are now orphaned / unmatched
- Operator changes `endpoint` URL without changing `name` → audit log reads as
  if the same provider answered, but it was a different server

---

## Decision

All configurable provider types in Axiom use a **four-layer identity model**.
Each layer answers a different forensic question.

The shared behavior lives in two base classes in `axiom.infra.provider_base`:

- **`ProviderIdentityMixin`** — pure mixin, works with `@dataclass` via
  `__post_init__`. Adds `uid`, `config_hash`, `instance_id`, and the `identity`
  property. Subclasses declare `_log_prefix` and `_fingerprint_fields`.

- **`ProviderBase(ProviderIdentityMixin, ABC)`** — full base for non-dataclass
  providers. Adds config dict handling, required-field validation, `available()`,
  `describe()`, `_logger`, and `handles_sensitive_data`.

Every provider type in the system inherits from one of these two bases.

### Layer 0: `uid` — Stable Unique Identifier

`uid` is the **true runtime key** for a provider. It is stable across display
name changes and across restarts when stored in config.

**Persistence model:**
- If `uid` is present in the config file, it is used as-is. It survives
  renames, model upgrades, and endpoint changes as long as the operator does
  not change the `uid` field itself.
- If `uid` is absent, one is auto-generated (UUID4) at load time and a
  `WARNING` is emitted containing the generated value. The operator is
  expected to paste this value into their config to make it permanent.
  The system does not write back to config files automatically.

```
WARNING  Provider 'qwen-private' has no 'uid' in config — generated uid=a1b2c3d4-...
         Add uid = "a1b2c3d4-..." to your config to persist it across restarts.
```

All runtime correlation (routing decisions, audit records, signal payloads,
cost attribution) uses `uid` as the primary key. `display_name` is for humans.

### Layer 1: `display_name` (`name` config key) — Human-Readable Label

`name` in config maps to the provider's display name. It is shown in log
records (for human readability alongside the uid), CLI output, and UI. It:
- May change freely without breaking forensic correlation — `uid` is stable
- Does not need to be unique (displayers handle collisions with prefixes/suffixes)
- Should still be descriptive; generic names make logs harder to read

| Too generic | Better |
|---|---|
| `"qwen"` | `"Qwen @ PrivateCloud EC"` |
| `"anthropic"` | `"Anthropic Sonnet (Primary)"` |
| `"file"` | `"System Log File"` |

### Layer 3: `config_hash` — Config Drift Detector

`config_hash` is an 8-character SHA-256 fingerprint of the fields that define
the provider's effective identity. Computed by `_compute_identity()` — not set
by the user. Each provider type declares which fields are fingerprinted via the
`_fingerprint_fields` class variable.

```python
# In ProviderIdentityMixin:
def _compute_identity(self, config: dict[str, Any]) -> None:
    fingerprint = "|".join(str(config.get(f, "")) for f in self._fingerprint_fields)
    self.config_hash = hashlib.sha256(fingerprint.encode()).hexdigest()[:8]
    self.instance_id = uuid.uuid4().hex[:12]
```

Examples:
- `LLMProvider._fingerprint_fields = ("endpoint", "model", "routing_tier")`
- `FileSink._fingerprint_fields = ("path",)` (inherits from LogSinkBase)

`config_hash` is **stable** while the fingerprinted config fields are unchanged,
and **changes** when any fingerprinted field changes — even if `name` stays the
same. This makes config drift visible in audit records without requiring the
operator to bump a version number.

### Layer 4: `instance_id` — Per-Load UUID

`instance_id` is a 12-character UUID4 hex string assigned once per
`_compute_identity()` call — i.e., once per provider instantiation.

`instance_id` is **intentionally not stable** across restarts. This is the
correct behavior: it distinguishes "the Qwen provider loaded at 14:00" from
"the Qwen provider reloaded at 16:00 after a config change" within a forensic
timeline, even if `name` and `config_hash` are identical.

---

## Provider Field Names in Log and Audit Records

The `identity` property returns a dict keyed by `_log_prefix`. This prefix is
specific to each provider type — never use the bare `"provider"` field.

```python
@property
def identity(self) -> dict[str, str]:
    return {
        self._log_prefix: self.name,             # display_name — for human readability
        f"{self._log_prefix}_uid": self.uid,     # stable key — primary forensic id
        f"{self._log_prefix}_config_hash": self.config_hash,
        f"{self._log_prefix}_instance": self.instance_id,
    }
```

Registered prefixes by provider type:

| Provider type | `_log_prefix` | Display field | UID field |
|---|---|---|---|
| LLM providers | `llm_provider` | `llm_provider` | `llm_provider_uid` |
| Log sinks | `log_sink` | `log_sink` | `log_sink_uid` |
| Storage providers | `storage_provider` | `storage_provider` | `storage_provider_uid` |
| Signal sources | `signal_source` | `signal_source` | `signal_source_uid` |
| Issue trackers | `issue_provider` | `issue_provider` | `issue_provider_uid` |
| Embedding providers | `embedding_provider` | `embedding_provider` | `embedding_provider_uid` |

Never use `"provider"` as a bare log field — it is ambiguous when a record
touches multiple provider types simultaneously (e.g., an LLM request that also
writes to a log sink).

### Usage in log records

**Minimal form** (most log records — readable, includes display name + uid):
```python
logger.info("Routing to LLM", extra={
    "llm_provider": provider.name,
    "llm_provider_uid": provider.uid,
})
```

**Full form** (session start, audit records, routing decisions):
```python
logger.info("Request routed", extra=provider.identity)
```

```json
{
  "llm_provider": "Qwen @ PrivateCloud EC",
  "llm_provider_uid": "a1b2c3d4-5678-...",
  "llm_provider_config_hash": "a3f7b2c1",
  "llm_provider_instance": "9e2b4df1a3c7"
}
```

Forensic queries should **filter by `uid`**, not by display name. Display names
can change freely; the uid is the stable cross-run key. Use the display name
for human consumption in dashboards and CLI output.

---

## ProviderBase — Shared Provider Contract

`ProviderBase` (for non-dataclass providers) and `ProviderIdentityMixin` (for
`@dataclass` providers) are the single source of shared provider behavior.

```python
from axiom.infra.provider_base import ProviderBase

class MyStorageProvider(ProviderBase):
    _log_prefix = "storage_provider"
    _fingerprint_fields = ("bucket", "region")
    _required_config = ("bucket",)

    def upload(self, path): ...

p = MyStorageProvider({"name": "s3-primary", "bucket": "my-bucket", "region": "us-east-1"})
logger.info("Uploading", extra=p.identity)
# → {"storage_provider": "s3-primary", "storage_provider_config_hash": "a3f7b2c1", ...}
```

Additional concerns handled by `ProviderBase`:

| Concern | Mechanism |
|---|---|
| Required config validation | `_required_config` tuple; raises `ValueError` on missing fields |
| Health check | `available() → bool` (default `True`; override for connectivity checks) |
| Human-readable description | `describe() → str`; format: `"log_sink:system-log-file [config_hash=a3f7b2c1]"` |
| Pre-wired logger | `self._logger` using module+qualname of concrete class |
| Sensitive data flag | `handles_sensitive_data: bool = False`; override on class |

---

## Validation at Config Load

Factories that load multiple providers from config enforce name uniqueness:

```python
seen_names: set[str] = set()
for p in providers:
    pname = p.get("name", "")
    if not pname:
        log.error("Provider entry missing required 'name' — skipped")
        continue
    if pname in seen_names:
        log.error("Duplicate provider name '%s' — second entry skipped", pname)
        continue
    seen_names.add(pname)
    # ... construct provider
```

After loading, each provider's identity is logged at INFO so the session
audit record captures exactly what was loaded:

```
INFO  gateway  Provider loaded: qwen-private (config_hash=a3f7b2c1, instance=9e2b4df1a3c7)
INFO  gateway  Provider loaded: anthropic-sonnet-primary (config_hash=c2d8e4f0, instance=7a1b3e5d)
```

---

## GatewayResponse and Legacy `provider` Field

`GatewayResponse.provider` (a plain string) is kept as-is — it holds
`provider.name` from the selected LLM provider. No breaking change for existing
callers. Extension developers who need the full identity dict should access
`provider.identity` on the `LLMProvider` object directly.

---

## Consequences

**Positive:**
- `uid` is stable across display name renames and across restarts — the primary
  forensic key no longer breaks when an operator renames a provider for clarity
- Duplicate provider entries are now an explicit error, not silent data loss
- Config drift (silent endpoint/model changes) is detectable in audit records
  without manual version tracking
- Forensic timeline can distinguish provider reloads from provider selection
- Extension developers have a single `.identity` dict to include in records —
  no manual field construction
- All provider types share the same four-layer identity — forensic queries
  work the same way regardless of provider type
- Display names may collide (e.g., two sinks both named "file") without
  breaking correlation — displayers resolve collisions; `uid` disambiguates

**Negative:**
- `uid` must be stored in config to persist across restarts. Operators who
  ignore the startup warning will get a new `uid` each run, making cross-run
  correlation on `uid` alone unreliable until they persist it. Mitigation:
  the warning includes the generated value in copy-paste-ready format.
- `config_hash` changes on any edit to a fingerprinted field. This is correct
  behavior (it IS a different effective provider) but may surprise operators who
  expect the hash to persist across minor edits like changing `max_tokens_default`.
- `instance_id` is not reproducible — forensic queries that filter by
  `instance` will find nothing in old log files. Use `uid` as the stable query
  key; use `instance_id` only for within-session disambiguation.

---

## Migration

Existing config files with non-unique or missing `name` fields:
- `axiom config --check` will warn about duplicate names after this change
- No config file changes are required if all existing `name` values are already
  unique (they typically are — users naturally use different names)
- Operators are encouraged to rename generic names (`"qwen"`, `"anthropic"`)
  to descriptive ones (`"qwen-private"`, `"anthropic-sonnet-primary"`) before
  the next audit cycle, but this is advisory — the system will not refuse to
  start with generic names

---

## Extension Developer Impact

Extension code that logs a provider name:

```python
# Before (ambiguous — "provider" could mean LLM, storage, or signal source):
logger.info("Routing to LLM", extra={"provider": provider.name})

# After (specific — llm_provider is unambiguous):
logger.info("Routing to LLM", extra=provider.identity)
# → {"llm_provider": "qwen-private", "llm_provider_config_hash": "a3f7b2c1", ...}
```

Minimal form (most log records):
```python
logger.info("Routing to LLM", extra={"llm_provider": provider.name})
```

Both are acceptable. The minimal form is preferred for high-frequency log
records where field volume matters. The full `provider.identity` form is
preferred for audit records, routing decisions, and signal payloads.

Custom provider types in extensions should:
1. Inherit from `ProviderBase` (or `ProviderIdentityMixin` for dataclasses)
2. Declare a unique `_log_prefix` that does not collide with the registered prefixes above
3. Declare `_fingerprint_fields` covering the fields that define the provider's effective identity
4. Never use the bare `"provider"` field in log records
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
