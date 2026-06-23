# Axiom Connections PRD

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-03-16
**Last Updated:** 2026-03-16
**Tech Spec:** [Connections & Credentials Spec](../tech-specs/spec-connections.md)

---

## Executive Summary

Axiom extensions integrate with external systems — LLM providers, code
repositories, communication platforms, document stores, CLI tools, MCP
servers, and other agents. Today, each extension implements its own auth
flow, credential storage, health checking, and error handling. Users
configure seven different systems in seven different ways.

This PRD defines **Connections** — a unified abstraction for all external
integrations. A Connection is any external system Axiom talks to.
The platform provides credential resolution, health checking, configuration
UX, and authorization gating. Extension builders declare their connections;
the platform handles the rest.

### Why This Matters

- **For researchers:** One command (`axiom connect`) to set up all integrations.
  No hunting for env var names or token formats.
- **For extension builders:** Five lines of TOML to add a new integration.
  No reimplementing auth, storage, or health checks.
- **For facility operators:** Centralized visibility into what's connected,
  what's broken, and who has access to what.
- **For compliance:** OpenFGA-gated connections ensure authorized access only.

---

## Problem Statement

### Current State (7 patterns, 0 consistency)

| Extension | Auth Method | Credential Storage | Setup UX |
|-----------|-----------|-------------------|----------|
| Chat / LLM Gateway | API key env var | `ANTHROPIC_API_KEY` env | `axiom config` wizard |
| Signal / GitHub | PAT env var | `GITHUB_TOKEN` env | `axiom config --set` |
| Signal / GitLab | PAT env var | `GITLAB_TOKEN` env | `axiom config --set` |
| Signal / Teams Chat | MS Graph device code | `runtime/inbox/state/` file | Manual env vars |
| Signal / Teams Browser | Playwright cookies | `~/.axi/credentials/` file | Auto on first use |
| Publisher / OneDrive | MS Graph client creds | `MS_GRAPH_*` env vars | `axiom config` wizard |
| RAG / pgvector | Connection string | `rag.database_url` setting | `axiom settings` |

Users must know: which env var, which config file, which CLI command, and
what format. Extension builders must decide these independently.

### Five Integration Patterns

| Pattern | Direction | Credential Type |
|---------|-----------|----------------|
| **API** | Neut → service | API key, OAuth token |
| **Browser** | Neut drives browser as user | Session cookies |
| **MCP** | LLM ↔ tool server | Server-specific |
| **CLI** | Neut wraps external binary | Binary on PATH |
| **A2A** | Agent ↔ agent | mTLS, federation token |

All five need the same platform support: credential storage, health checks,
configuration UX, graceful degradation, and authorization gating.

---

## Goals & Non-Goals

### Goals

1. **Unified `axiom connect` command** for adding, testing, and managing all external integrations
2. **Declarative connection registration** via `axiom-extension.toml` — extension builders declare, platform handles
3. **Credential resolution chain** — env var → settings → keychain → file → browser → prompt
4. **Health checking** visible in `axiom status` and `axiom connect --check`
5. **Multiple auth methods per connection** (e.g., Teams: browser or Graph API)
6. **Secure credential storage** at `~/.axi/credentials/` with 0600 permissions
7. **OpenFGA authorization gating** — connections declare required relations
8. **Graceful degradation** — missing credentials disable features, never crash

### Non-Goals

- Secrets management (Vault, SOPS, 1Password — complementary, not replaced)
- Credential rotation automation (notify, don't auto-rotate)
- Connection pooling or load balancing (infrastructure concern)
- Multi-tenant credential isolation (single-user for Phase 1)

---

## User Stories

### Researcher (new user)

**US-001**: As a new user, I want `axiom connect` to show me what integrations are available and guide me through setting up each one.

**US-002**: As a researcher, I want `axiom connect teams` to open a browser, let me log in, and then just work headlessly forever after.

**US-003**: As a researcher, I want `axiom status` to tell me which connections are working and which need attention.

### Extension Builder

**US-010**: As an extension builder, I want to declare my connection needs in TOML and have the platform handle credential resolution.

**US-011**: As an extension builder, I want `get_credential("jira")` to return a token or `None` — I should never implement storage or prompting.

**US-012**: As an extension builder, I want to declare multiple auth methods (API key, browser, manual) and let the user choose.

### Facility Operator

**US-020**: As a facility operator, I want to see all active connections and their health at a glance.

**US-021**: As an operator, I want OpenFGA to gate which users can use which connections, so I can restrict private endpoint access.

**US-022**: As an operator, I want `axiom connect --check` to verify all connections before deploying to production.

---

## Functional Requirements

### FR-001: Connection Declaration

Extensions declare connections in `axiom-extension.toml`:

```toml
[[connections]]
name = "jira"
display_name = "Jira"
kind = "api"
endpoint = "https://myorg.atlassian.net"
credential_type = "api_key"
credential_env_var = "JIRA_TOKEN"
category = "project_management"
required = false
docs_url = "https://support.atlassian.com/..."
```

The platform discovers these at extension load time and registers them.

### FR-002: Credential Resolution

Platform resolves credentials in order:

1. Environment variable (`JIRA_TOKEN`)
2. `axiom settings` (`connections.jira.token`)
3. OS keychain (macOS Keychain / Linux secret-service)
4. Credential file (`~/.axi/credentials/jira/token`, 0600)
5. Browser session (`~/.axi/credentials/teams/state.json`)
6. Interactive prompt (first-time setup)

Extension code:

```python
from axiom.infra.connections import get_credential

token = get_credential("jira")  # Returns str or None
```

### FR-003: `axiom connect` CLI

```bash
axiom connect                      # List all connections and status
axiom connect teams                # Set up Teams connection
axiom connect teams --method browser  # Specify auth method
axiom connect teams --clear        # Remove saved credentials
axiom connect --check              # Health check all connections
axiom connect --json               # Machine-readable output
```

Setup flow:

```
$ axiom connect teams
  Microsoft Teams
  ───────────────
  Choose auth method:
    1. Browser login (recommended — no API keys needed)
    2. MS Graph API (requires developer credentials)
    3. Manual file drop (no auth)
  > 1

  Launching browser for Microsoft login...
  Complete login + MFA in the browser window.

  ✓ Connected — session saved to ~/.axi/credentials/teams/
  ✓ Health check: 3 recent meetings found
```

### FR-004: Health Checks

Every connection has a health check. Platform runs these:
- On `axiom status` (all connections)
- On `axiom connect --check` (explicit)
- On first use by an extension (lazy)

Health check types:

| Type | Implementation |
|------|---------------|
| `http_get` | GET the health endpoint, check for 200 |
| `tcp_connect` | TCP connect to host:port, 1s timeout |
| `import_check` | Verify a Python module is importable |
| `cli_version` | Run `binary --version`, check output |
| `custom` | Call a registered function |

### FR-005: Multiple Auth Methods

Connections can declare multiple auth methods:

```toml
[[connections.auth_methods]]
method = "browser"
description = "Browser login (recommended)"

[[connections.auth_methods]]
method = "graph_api"
credential_env_var = "MS_GRAPH_CLIENT_ID"
description = "MS Graph API (requires developer credentials)"

[[connections.auth_methods]]
method = "manual"
description = "Manual file drop (no auth)"
```

`axiom connect <name>` presents these as options.

### FR-006: CLI Tool Connections

External binaries (Ollama, Pandoc, kubectl) are connections:

```toml
[[connections]]
name = "ollama"
kind = "cli"
endpoint = "ollama"              # Binary name
credential_type = "none"
health_check = "cli_version"
docs_url = "https://ollama.com/download"
```

Platform provides:

```python
from axiom.infra.connections import get_cli_tool

ollama = get_cli_tool("ollama")
# ollama.path = "/opt/homebrew/bin/ollama"
# ollama.version = "0.1.27"
# ollama is None if not installed
```

### FR-007: Authorization Gating (OpenFGA)

Connections declare authorization requirements:

```toml
[connections.authorization]
required_relation = "can_access"
```

Platform checks OpenFGA before allowing use:

```python
# Internally:
if not openfga.check(user, connection_name, "can_access"):
    raise AuthorizationError(f"Access denied to {connection_name}")
```

Phase 1: no OpenFGA — all connections available to all users.
Phase 2: OpenFGA gates connection access.

### FR-008: Connection Status in `axiom status`

```
$ axiom status

Connections
═══════════
  ✓ Anthropic Claude       API key set, connected (45ms)
  ✓ GitHub                 Token set, 3 repos accessible
  ⚠ Microsoft Teams        Session expired — run `axiom connect teams`
  ✗ Private endpoint       VPN unreachable
  ○ GitLab                 Not configured (optional)
  ○ Jira                   Not configured (optional)
  ✓ Ollama                 Running, llama3.2:1b available
  ✓ Pandoc                 v3.1.9
```

---

## Phased Implementation

### Phase 1: Credential Resolution + `axiom connect` ✅ SHIPPED v0.4.2

- `src/axiom/infra/connections.py` — `get_credential()`, `has_credential()`, `check_health()`, `get_cli_tool()`
- `axiom connect` CLI command (list, setup, check, clear, tab completion, JSON)
- Migrated extractors to `get_credential()` (8 files)
- Connection declarations in `axiom-extension.toml` (11 connections across 5 extensions)
- `axiom status` connection health display
- Adaptive rate limiter (learns from API response headers)
- Managed service lifecycle (launchd/systemd/Windows)
- Capabilities (read/write), usage tracking, throttle detection
- Provider preference chain (`routing.prefer_provider`)

### Phase 1.5: TRIAGE Self-Healing Integration ✅ SHIPPED v0.4.2

- Connection events on EventBus: `connections.healthy`, `connections.degraded`, `connections.unhealthy`, `connections.throttled`
- TRIAGE subscribes to connection events for proactive remediation
- Connection-aware recovery strategies in `self_heal.py`:
  - Service down → `ensure_available()` → retry
  - Credential expired → prompt via `axiom connect <name>`
  - Rate limited → backoff → retry (don't patch code)
  - Credential missing → graceful skip (not an error)
- Usage tracking: per-connection request count, error count, throttle count, avg latency
- Capabilities reporting: read/write/admin/stream per connection

### Phase 2: Auth Method Negotiation + Onboarding ✅ SHIPPED v0.4.2

- Multiple auth methods per connection (browser, graph_api, manual)
- `axiom connect <name>` presents auth method choices
- `axiom config` wizard delegates to `axiom connect`
- Playwright browser auth with session persistence
- Playwright auto-install if missing

### Phase 3: OpenFGA Authorization (deferred to Security PRD v0.5.x+)

- Requires Ory Kratos (identity) — see [Security PRD](prd-security.md) Phase 3-6
- OpenFGA sidecar deployment (K3D/K8S)
- Connection-level access control
- Document/corpus-level access control
- `axiom connect` shows authorization status per user

### Phase 4: MCP Client + A2A

- Consume external MCP servers as connections
- Agent federation (mTLS between facilities)
- Connection discovery (advertise available connections)

---

## Extension Builder Guide

### Adding a New Integration (5 lines of TOML + 3 lines of Python)

**TOML:**
```toml
[[connections]]
name = "my_service"
display_name = "My Service"
kind = "api"
credential_env_var = "MY_SERVICE_TOKEN"
docs_url = "https://myservice.com/docs/api-keys"
```

**Python:**
```python
from axiom.infra.connections import get_credential

token = get_credential("my_service")
if token is None:
    return []  # Graceful degradation
```

### Rules

1. **Never hardcode credentials**
2. **Never store credentials in runtime/** — platform handles storage
3. **Always degrade gracefully** — `get_credential()` returns `None`
4. **Declare connections in the manifest** — platform discovers them
5. **Provide `docs_url`** — tells users where to get credentials
6. **Use `required=false` by default** — let the user decide what to enable

---

## Success Metrics

| Metric | Target |
|--------|--------|
| Time from `git clone` to first `axiom chat` | < 5 minutes |
| Number of auth patterns for extension builders | 1 (`get_credential()`) |
| Connection setup steps per integration | ≤ 3 (choose method → authenticate → verify) |
| `axiom status` shows all connections | 100% of declared connections |
| Credential storage uses 0600 permissions | 100% |

---

## Open Questions

1. Should `axiom connect` auto-install CLI tools (e.g., `brew install ollama`)?
2. Should browser sessions have a configurable max age before re-auth?
3. How do we handle credential rotation notification?
4. Should `axiom connect` support `--export` for sharing connection configs (without credentials)?
5. How do MCP server connections interact with the chat agent's tool registry?

---

## Related Documents

- [Connections & Credentials Spec](../tech-specs/spec-connections.md) — Technical specification
- [Security & Access Control PRD](prd-security.md) — OpenFGA authorization, FR-008
- [Model Routing Spec](../tech-specs/spec-model-routing.md) — LLM provider connections
- [Agent State Management PRD](prd-agent-state-management.md) — Credential state in registry
- [Axiom Executive PRD](prd-executive.md)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
