# Axiom Connections & Credentials Spec

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-03-16
**Last Updated:** 2026-03-16

---

## 1. Problem Statement

Axiom extensions integrate with external systems using seven different
auth patterns, each reinvented per extension. Extension builders must decide
independently: where to store tokens, how to configure endpoints, how to
handle expiration, how to surface health status, and what to do when
credentials are missing.

This creates inconsistency for users ("why does GitHub auth work differently
from Teams auth?") and friction for extension developers ("how do I add
OneDrive support to my extension?").

### Four Integration Patterns Converging

| Pattern | Direction | Example | Credential |
|---------|-----------|---------|------------|
| **Traditional API** | Neut → external service | GitHub, Anthropic, MS Graph | API key, OAuth token |
| **Browser automation** | Neut drives browser as user | Teams Playwright, OneDrive | Session cookies |
| **MCP** | LLM ↔ tool server | Claude Code → Neut tools; Neut → external MCP servers | Server-specific |
| **Agent-to-Agent (A2A)** | Agent ↔ agent | Signal → Chat; Facility A → Facility B | Mutual auth, federation tokens |
| **CLI tool** | Neut wraps external CLI | Ollama, Pandoc, kubectl, git-crypt | Binary on PATH, version check |

All five need: credential/binary resolution, health checks, configuration UX,
graceful degradation, and audit logging. The platform should provide these once.

---

## 2. Design: The Connection Abstraction

A **Connection** is Axiom's unit of external integration. Every external
system — API, browser session, MCP server, or federated agent — is registered
as a Connection.

```python
@dataclass
class Connection:
    """An external system that Axiom integrates with."""

    name: str                    # "github", "anthropic", "teams", "private-qwen"
    display_name: str            # "GitHub", "Anthropic Claude", "Microsoft Teams"
    kind: str                    # "api" | "browser" | "mcp" | "a2a"

    # Transport
    endpoint: str                # URL, hostname, or MCP server command
    transport: str               # "https" | "grpc" | "stdio" | "playwright"

    # Credential
    credential_type: str         # "api_key" | "oauth_token" | "browser_session" | "mtls"
    credential_env_var: str      # Primary: env var name (e.g., "GITHUB_TOKEN")
    credential_keychain: str     # Secondary: macOS Keychain / secret-service key
    credential_file: str         # Tertiary: file path (relative to ~/.axi/credentials/)

    # Behavior
    required: bool               # Is this connection required for core functionality?
    auto_refresh: bool           # Can the credential be refreshed automatically?
    health_check: str            # "http_get" | "tcp_connect" | "import_check" | "custom"
    health_endpoint: str         # URL or command for health check

    # Metadata
    category: str                # "llm" | "code" | "communication" | "storage" | "data"
    extension: str               # Which extension owns this connection
    docs_url: str                # Where to get credentials
```

### 2.1 Credential Resolution Order

When an extension needs a credential, the platform resolves it in order:

```
1. Environment variable     (GITHUB_TOKEN)        ← CI/CD, containers
2. axiom settings             (connections.github.token)  ← explicit user config
3. OS keychain              (macOS Keychain / secret-service) ← secure desktop
4. Credential file          (~/.axi/credentials/github/token) ← fallback
5. Browser session          (~/.axi/credentials/teams/state.json) ← Playwright
6. Interactive prompt       ("Enter your GitHub token: ") ← first-time setup
```

Extensions never implement this logic. They call:

```python
from axiom.infra.connections import get_credential

# Returns the credential or None (never throws)
token = get_credential("github")

# With fallback to interactive prompt
token = get_credential("github", prompt=True)

# Check if available without prompting
if has_credential("github"):
    ...
```

### 2.2 Health Check Contract

Every Connection has a health check. The platform runs these:
- On `axiom status` (all connections)
- On `axiom config` (during onboarding)
- On extension startup (lazy, first use)

```python
from axiom.infra.connections import check_health

result = check_health("github")  # → ConnectionHealth(status, latency_ms, message)
```

Health checks are lightweight:
- `http_get`: GET the health_endpoint, check for 200
- `tcp_connect`: TCP connect to endpoint:port, 1s timeout
- `import_check`: Verify a Python module is importable
- `custom`: Call a registered function

### 2.3 Configuration UX

Connections surface in `axiom settings` and `axiom status`:

```
$ axiom status

Connections
═══════════
  ✓ Anthropic Claude       API key set, connected (45ms)
  ✓ GitHub                 Token set, 3 repos accessible
  ⚠ Microsoft Teams        No credentials — run `axiom connect teams`
  ✗ Private endpoint       VPN unreachable
  ○ GitLab                 Not configured (optional)

$ axiom connect teams
  Choose auth method:
    1. Browser login (recommended — no API keys needed)
    2. MS Graph API (requires developer credentials)
    3. Manual file drop (no auth)
  > 1
  Launching browser for Microsoft login...
  ✓ Connected — session saved to ~/.axi/credentials/teams/
```

### 2.4 `axiom connect` Command

A single command for adding/managing connections:

```bash
axiom connect                    # List all connections and status
axiom connect teams              # Set up a specific connection
axiom connect teams --method browser  # Specify auth method
axiom connect teams --clear      # Remove saved credentials
axiom connect --check            # Health check all connections
```

This replaces the scattered `axiom config --set github_token` pattern.

---

## 3. How This Maps to Each Integration Pattern

### 3.1 Traditional API (GitHub, Anthropic, GitLab)

```toml
# Registered by the extension's axiom-extension.toml or Python code

[[connections]]
name = "github"
display_name = "GitHub"
kind = "api"
endpoint = "https://api.github.com"
transport = "https"
credential_type = "api_key"
credential_env_var = "GITHUB_TOKEN"
health_check = "http_get"
health_endpoint = "https://api.github.com/user"
category = "code"
extension = "signal"
required = false
docs_url = "https://github.com/settings/tokens"
```

Extension code:

```python
from axiom.infra.connections import get_credential

class GitHubExtractor(BaseExtractor):
    def __init__(self):
        self.token = get_credential("github")

    def is_available(self) -> bool:
        return self.token is not None
```

### 3.2 Browser Automation (Teams, OneDrive)

```toml
[[connections]]
name = "teams"
display_name = "Microsoft Teams"
kind = "browser"
endpoint = "https://teams.microsoft.com"
transport = "playwright"
credential_type = "browser_session"
credential_file = "teams/state.json"     # Relative to ~/.axi/credentials/
health_check = "custom"                   # Check if session cookies are valid
category = "communication"
extension = "signal"
required = false
auto_refresh = true                       # Session cookies refresh on use
```

Extension code:

```python
from axiom.infra.connections import get_browser_session, has_session

class TeamsBrowserExtractor(BaseExtractor):
    def fetch(self):
        if not has_session("teams"):
            # Platform handles: launch browser, do login, save cookies
            authenticate_browser("teams", headed=True)

        session = get_browser_session("teams")
        # session is a Playwright storage_state dict
```

### 3.3 MCP (Model Context Protocol)

Axiom is both an MCP server and an MCP client.

**As server** (Claude Code → Neut tools):
Already implemented via `src/axiom/mcp_server/`. No credential needed —
Claude Code invokes via stdio.

**As client** (Neut → external MCP servers):
An extension could consume tools from another MCP server. The connection
model handles the credential for the external server.

```toml
[[connections]]
name = "linear-mcp"
display_name = "Linear (MCP)"
kind = "mcp"
endpoint = "npx @anthropic/linear-mcp-server"
transport = "stdio"
credential_type = "api_key"
credential_env_var = "LINEAR_API_KEY"
health_check = "import_check"
category = "project_management"
extension = "signal"
```

**Key insight:** MCP servers often need their own credentials (Linear API key,
Slack token, etc.). The Connection model handles this uniformly — the MCP
server's credential is stored the same way as any other API key.

### 3.5 CLI Tools (Ollama, Pandoc, kubectl)

External CLIs that Axiom wraps or depends on:

```toml
[[connections]]
name = "ollama"
display_name = "Ollama"
kind = "cli"
endpoint = "ollama"              # Binary name on PATH
transport = "subprocess"
credential_type = "none"          # No auth, just needs to be installed
health_check = "custom"           # Check version + serving status
health_endpoint = "http://localhost:11434/api/tags"
category = "llm"
extension = "core"
required = false
docs_url = "https://ollama.com/download"
```

Extension code:

```python
from axiom.infra.connections import get_cli_tool

ollama = get_cli_tool("ollama")
if ollama is None:
    # Not installed — degrade gracefully
    return fallback_classification()

# ollama.path = "/opt/homebrew/bin/ollama"
# ollama.version = "0.1.27"
result = subprocess.run([ollama.path, "run", model, prompt], ...)
```

The platform handles: PATH resolution, version detection, install guidance
(`docs_url`), and health checking. Extension builders never parse PATH or
`--version` output themselves.

### 3.6 Agent-to-Agent (A2A)

For multi-facility or multi-agent deployments, Axiom agents need to
authenticate to each other.

**Intra-facility** (agents on the same machine): No auth needed — they share
the same filesystem and event bus.

**Inter-facility** (agents at different sites): Mutual TLS or signed tokens.

```toml
[[connections]]
name = "partner-facility"
display_name = "Partner Axiom"
kind = "a2a"
endpoint = "https://axiom.partner.example/api/v1"
transport = "https"
credential_type = "mtls"
credential_file = "a2a/peer-client.pem"
health_check = "http_get"
health_endpoint = "https://axiom.partner.example/api/v1/health"
category = "federation"
extension = "core"
required = false
```

**This is future work** — documented here to prove the abstraction scales.

### 3.7 External Ontology / Graph Systems

External ontology or graph systems are connected through the standard
connection mechanism (§3): declare a connection in the consuming extension and
implement an `OntologyProvider` (see spec-knowledge-graph §8) in the consuming
domain repo. Axiom core ships the generic seam only — no system-specific
endpoints, credentials, or provider implementations live here.

---

## 4. Credential Storage

### 4.1 Storage Hierarchy

```
Environment Variables          ← ephemeral, CI/CD, containers
  ↓ fallback
~/.axi/settings.toml          ← user preferences (can reference env vars)
  ↓ fallback
OS Keychain                    ← macOS Keychain, Linux secret-service
  ↓ fallback
~/.axi/credentials/           ← file-based, 0600 permissions
  ├── github/
  │   └── token                ← plaintext PAT (0600)
  ├── teams/
  │   └── state.json           ← Playwright session cookies (0600)
  ├── anthropic/
  │   └── api_key              ← plaintext API key (0600)
  └── a2a/
      └── peer-client.pem      ← mTLS client certificate
```

### 4.2 Security Properties

| Storage | Encryption at rest | Access control | Survives reboot | Portable |
|---------|-------------------|---------------|----------------|----------|
| Env var | No (process memory) | Process-level | No | No |
| Settings file | FileVault/LUKS | File permissions | Yes | Yes |
| OS Keychain | Yes (hardware-backed on macOS) | User + app level | Yes | No |
| Credential file | FileVault/LUKS | 0600 file permissions | Yes | Yes |
| Browser session | FileVault/LUKS | 0600 file permissions | Yes (until expiry) | No |

### 4.3 What Extension Builders Do

Extension builders don't implement credential storage. They:

1. **Declare** their connections in `axiom-extension.toml`
2. **Call** `get_credential(name)` to get the credential
3. **Handle** `None` return (credential not available)

That's it. The platform handles storage, resolution, health checks, and UI.

---

## 5. Extension Builder Guide

### 5.1 Adding a New External Integration

```toml
# my-extension/axiom-extension.toml

[extension]
name = "my-extension"
version = "0.1.0"

[[connections]]
name = "jira"
display_name = "Jira"
kind = "api"
endpoint = "https://myorg.atlassian.net"
credential_type = "api_key"
credential_env_var = "JIRA_TOKEN"
category = "project_management"
required = false
docs_url = "https://support.atlassian.com/atlassian-account/docs/manage-api-tokens-for-your-atlassian-account/"
```

```python
# my_extension/jira_client.py
from axiom.infra.connections import get_credential

def get_issues():
    token = get_credential("jira")
    if token is None:
        return []  # Graceful degradation
    # ... use token
```

### 5.2 Rules for Extension Builders

1. **Never hardcode credentials** — always use `get_credential()`
2. **Never store credentials in runtime/** — platform handles storage
3. **Always degrade gracefully** — `get_credential()` returns `None`, handle it
4. **Declare connections in the manifest** — platform discovers and health-checks them
5. **Provide `docs_url`** — tells users where to get the credential
6. **Use `required=false`** unless the extension is useless without the connection
7. **Pick a `credential_env_var`** — standard name so CI/CD can set it

### 5.3 Auth Method Selection (for browser-capable connections)

Some services support multiple auth methods. Declare them all:

```toml
[[connections]]
name = "teams"
display_name = "Microsoft Teams"

[[connections.auth_methods]]
method = "browser"
transport = "playwright"
credential_type = "browser_session"
description = "Browser login (recommended — no API keys needed)"

[[connections.auth_methods]]
method = "graph_api"
transport = "https"
credential_type = "oauth_token"
credential_env_var = "MS_GRAPH_CLIENT_ID"
description = "MS Graph API (requires developer credentials)"

[[connections.auth_methods]]
method = "manual"
transport = "filesystem"
description = "Manual file drop (no auth)"
```

The platform presents these options during `axiom connect teams`.

---

## 6. Relationship to Existing Systems

### 6.1 `axiom config` (Setup Wizard)

The setup wizard discovers registered connections and walks through them.
`axiom config` becomes a guided `axiom connect` for all declared connections.

### 6.2 `axiom settings`

Connection configuration is stored in settings:

```toml
[connections.github]
token_env = "GITHUB_TOKEN"    # Which env var to use (can override default)

[connections.teams]
auth_method = "browser"       # Which auth method to use
```

### 6.3 `axiom status`

Status checks all registered connections and reports health.

### 6.4 Model Routing (`gateway.py`)

LLM providers are a special case of connections (kind="llm"). The existing
`models.toml` provider config maps to the connection model:

```
LLMProvider.api_key_env    → Connection.credential_env_var
LLMProvider.endpoint       → Connection.endpoint
LLMProvider.requires_vpn   → Connection.health_check = "tcp_connect"
LLMProvider.routing_tier   → Connection metadata (export control)
```

Long-term, `models.toml` providers and connections unify into a single
registry. Short-term, they coexist — LLM providers are a well-tested path
that doesn't need disruption.

### 6.5 MCP Server Registry

MCP servers that Axiom consumes are connections with `kind = "mcp"`.
This aligns with Claude Code's MCP server configuration pattern:

```json
// Claude Code: .claude/settings.json
{
  "mcpServers": {
    "linear": {
      "command": "npx",
      "args": ["@anthropic/linear-mcp-server"],
      "env": {"LINEAR_API_KEY": "..."}
    }
  }
}
```

Axiom equivalent:

```toml
[[connections]]
name = "linear"
kind = "mcp"
endpoint = "npx @anthropic/linear-mcp-server"
transport = "stdio"
credential_env_var = "LINEAR_API_KEY"
```

Same semantics, Axiom's connection model.

---

## 7. Implementation Plan

### Phase 1: Credential Resolution (build now)

- `src/axiom/infra/connections.py` — `get_credential()`, `has_credential()`, `check_health()`
- Resolve from: env var → settings → credential file
- Migrate existing extractors to use `get_credential()`

### Phase 1.5: TRIAGE Self-Healing (build now)

Connections emit events on the EventBus. TRIAGE subscribes to diagnose
and remediate connection failures before they cascade to agent errors.

**Events emitted by connections module:**
- `connections.healthy` — Connection verified working
- `connections.degraded` — Connection responding but throttled or slow
- `connections.unhealthy` — Connection failed health check
- `connections.throttled` — HTTP 429 received

**TRIAGE recovery strategies** (in `self_heal.py`):

| Failure Mode | Strategy | Fallback |
|-------------|----------|----------|
| Service down | `ensure_available()` auto-start | File GitLab issue |
| Credential expired | Prompt via `axiom connect <name>` | Skip gracefully |
| Rate limited | Exponential backoff, switch provider | Wait + retry |
| Credential missing | Degrade gracefully (not an error) | — |
| Connection error in CLI | Check connections before patching code | Standard TRIAGE pipeline |

**Credential storage hierarchy:**

```
Resolution order (first match wins):
1. Environment variable     $ANTHROPIC_API_KEY          ← CI/CD, containers
2. axiom settings            connections.anthropic.token  ← explicit config
3. Credential file          ~/.axi/credentials/anthropic/token (0600)
4. OS Keychain              macOS Keychain / secret-service (Phase 2)
5. Browser session          ~/.axi/credentials/teams/state.json (Phase 2)
6. Interactive prompt       axiom connect <name> (Phase 1)
```

**Usage tracking (per-connection):**
- `requests` — Total API calls
- `errors` — Failed calls
- `throttled_count` — 429 responses
- `avg_latency_ms` — Running average
- Visible in `axiom connect --check` and `axiom status`

### Phase 2: Connection Registry (shipped in Phase 1)

- Connection declaration in `axiom-extension.toml` ✅
- `axiom connect` CLI command ✅
- `axiom status` shows connection health ✅
- Capabilities: read/write/admin/stream ✅

### Phase 3: Auth Method Negotiation

- Multiple auth methods per connection
- `axiom connect teams` presents options
- Platform handles Playwright lifecycle

### Phase 4: MCP Client + A2A

- Consume external MCP servers as connections
- Agent federation with mTLS

---

## 8. Authorization Model

Connections integrate with Axiom's authorization framework. Not every
user or agent should access every connection. Authorization answers: "is
this identity allowed to use this connection for this purpose?"

### 8.1 Authorization Framework: OpenFGA

Axiom will use [OpenFGA](https://openfga.dev/) — an open-source
implementation of Google Zanzibar — for fine-grained authorization. OpenFGA
supports all three authorization models Axiom needs:

| Model | What it answers | Axiom example |
|-------|----------------|-------------------|
| **RBAC** (role-based) | Does this user have this role? | "Ben has role `export_controlled_access`" |
| **ReBAC** (relationship-based) | Is this user related to this resource? | "Ben is a member of the facility team, which owns this wiki" |
| **ABAC** (attribute-based) | Do this user's attributes satisfy a policy? | "User is on-campus AND has completed EC training" |

### 8.2 Connections × Authorization

Each connection declares its authorization requirements:

```toml
[[connections]]
name = "private-qwen"
display_name = "PrivateCloud Private Endpoint"
kind = "api"
routing_tier = "export_controlled"

[connections.authorization]
required_relation = "can_access"   # OpenFGA relation
required_attributes = ["ec_training_complete"]
```

The gateway checks authorization before sending a request:

```
classify(query) → tier = "export_controlled"
  → select_provider() → "private-qwen"
    → check_authorization(user, "private-qwen", "can_access")
      → OpenFGA: allowed? → yes/no
```

**Classification decides WHAT the query is. Authorization decides WHO may
use the connection. Both must pass independently.**

### 8.3 OpenFGA Integration Plan

1. **Phase 1:** No OpenFGA — single-user, all connections available
2. **Phase 2:** OpenFGA for connection-level access control (who can use which LLM provider)
3. **Phase 3:** OpenFGA for document-level access (who can read which RAG corpus)
4. **Phase 4:** Federation — OpenFGA across facilities

OpenFGA runs as a sidecar service (K3D/K8S deployment) alongside PostgreSQL.
It uses PostgreSQL as its storage backend — same database Axiom already
requires.

---

## 9. Open Questions

1. Should OS keychain be a required dependency or optional?
2. How do we handle credential rotation (e.g., OAuth token refresh)?
3. Should `axiom connect` support `--test` to verify after setup?
4. How do connections interact with export-control routing? (A connection
   to a public API should not receive EC-classified data.)
5. Should browser sessions have a configurable max age before re-auth?
6. Should OpenFGA run as a sidecar in the K3D cluster or as a Go binary?
7. How do we bootstrap authorization for the first user (who has no roles yet)?
8. Should CLI tool connections auto-install (e.g., `brew install ollama`) or just guide?

---

## Related Documents

- [Connections PRD](../requirements/prd-connections.md) — User-facing requirements for `axiom connect`
- [Security & Access Control PRD](../requirements/prd-security.md) — OpenFGA authorization (FR-008), ReBAC/RBAC/ABAC
- [Model Routing Spec](spec-model-routing.md) — LLM provider credentials, routing tier authorization
- [Agent State Management Spec](spec-agent-state-management.md) — Credential storage in state registry
- [Axiom Master Tech Spec](spec-executive.md) — Extension system
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
