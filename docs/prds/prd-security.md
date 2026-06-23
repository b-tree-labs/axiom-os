# Axiom Security PRD

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-03-15
**Last Updated:** 2026-03-18
**Tech Spec:** [Security Spec](../tech-specs/spec-security.md)

**Scope:** Identity, Authentication (AuthN), Authorization (AuthZ), RBAC,
ReBAC, ABAC, Credential & Key Management, Export Control Defense, Audit Logging
**Related:** [Federal Data Management](prd-doe-data-management.md)

---

## Executive Summary

Axiom operates in operations facility environments where export-controlled
data is regulated under 10 CFR 810, where users must be identified and
authenticated, and where external service credentials must be stored
securely. This PRD consolidates three security concerns into one design
surface:

1. **Identity & Authentication** — Who are you? Prove it.
2. **Credential Management** — Secure storage for API keys and service tokens.
3. **EC Defense & Authorization** — Layered protection for export-controlled data.

### Design Principles

- **Everything is a provider.** Auth backends, credential stores, and
  authorization engines are pluggable. Facilities choose what works for them.
- **Ship a complete solution.** Ory Kratos for identity, OS Keychain for
  credentials, OpenFGA for authorization — all work out of the box with zero
  external dependencies beyond PostgreSQL.
- **Degrade gracefully.** Air-gapped facility with no SSO? Local accounts
  work. No Vault? Keychain works. No OpenFGA? Single-user mode works.

---

## Part 1: Identity & Authentication

### Problem

Axiom has no user identity. Every CLI session is anonymous. Agent actions
are unattributed. Audit trails say "axiom" not "user@example.org". In multi-user
deployments, identity is required to enforce per-user access policies and
attribute actions. In EC-configured deployments, it is additionally required
for authorization gating (Part 3). Single-user deployments do not need
identity and can skip Phases 3–6.

### Solution: Ory Kratos + OAuth Providers

[Ory Kratos](https://www.ory.sh/kratos/) is an open-source, self-hosted
identity server that handles:
- Registration, login, password recovery
- Multi-factor authentication (TOTP, WebAuthn, email codes)
- OAuth/OIDC social sign-in (Google, Microsoft, GitHub, etc.)
- Session management with configurable lifetimes
- Account verification and recovery flows
- PostgreSQL backend (shares our existing database)

**Why Kratos, not custom code:**
- Identity is a solved problem. Rolling our own invites security bugs.
- Kratos is a single Go binary — ships via `axiom connect kratos` like Ollama.
- Managed by our `ServiceManager` (launchd/systemd/Windows Task Scheduler).
- Battle-tested: used by Raspberry Pi Foundation, Sainsbury's, Discogs.

### FR-ID-001: Authentication Provider Stack

| Provider | Type | MFA | Network | When to Use |
|----------|------|-----|---------|-------------|
| **Ory Kratos** | Local identity server | TOTP, WebAuthn | No (localhost) | Default — always available |
| **Google OAuth** | OAuth 2.0 PKCE | Delegated | Yes | University environments |
| **Microsoft OAuth** | OAuth 2.0 PKCE | Delegated | Yes | Enterprise / .edu |
| **GitHub OAuth** | OAuth 2.0 | Delegated | Yes | Developer environments |
| **GitLab OAuth** | OAuth 2.0 | Delegated | Yes | PrivateCloud / institutional GitLab |
| **LDAP / AD** | LDAP bind (via Kratos) | TOTP (axiom-managed) | LAN | Government / enterprise |
| **OIDC (generic)** | OpenID Connect | Delegated to IdP | Yes | Custom institutional SSO |
| **SAML 2.0** | SAML SP (via Kratos) | Delegated to IdP | Yes | Federal / DOE SSO |

**Key insight:** OAuth, LDAP, OIDC, and SAML are configured as upstream
identity providers *in Kratos*, not as separate auth backends in axiom.
Kratos handles the protocol complexity. Neut talks to Kratos. One integration
point, N identity sources.

### FR-ID-001a: Kratos → OpenFGA Webhook Bridge

Kratos and OpenFGA connect via post-flow webhooks. When a user registers
or logs in, Kratos fires a webhook. Axiom handles it by writing
OpenFGA relationship tuples:

```
User logs in via Google → Kratos authenticates
    ↓ post-login webhook fires
    ↓
Axiom hook-service:
    1. Read identity traits (email, groups, org)
    2. Map IdP claims to Axiom roles (via auth.toml role_mapping)
    3. Write OpenFGA tuples:
       user:user@example.org → role:export_controlled_access#member
       user:user@example.org → connection:anthropic#can_access
       user:user@example.org → rag_corpus:domain-wiki#can_query
    4. Return session to Kratos → user is logged in with roles
```

**Events that trigger tuple updates:**
- **Registration:** Create user, assign default roles
- **Login:** Refresh session, sync group memberships from IdP claims
- **Admin action:** `axiom admin grant <user> <role>` → write tuple
- **Deprovisioning:** Remove all tuples for a user

**Why OpenFGA, not Ory Keto:**
- Both implement Google Zanzibar (relationship-based access control)
- OpenFGA has better maintenance trajectory (backed by Auth0/Okta)
- OpenFGA supports multiple authorization models, contextual tuples, audit log
- OpenFGA has richer SDK ecosystem and documentation
- Ory Keto development has stagnated (minimal commits since 2024)

### FR-ID-002: Configuration (`auth.toml`)

```toml
# runtime/config/auth.toml

[auth]
# Login is required for write commands (default) or all commands
require_login = "write"        # "all" | "write" | "none"
session_timeout_hours = 168    # 7 days
session_timeout_ec_hours = 8   # Shorter for EC-tier sessions

# MFA policy
mfa_required = false           # Global MFA requirement
mfa_required_for_ec = true     # MFA required for export-controlled access
mfa_methods = ["totp"]         # "totp", "webauthn", "email"

[auth.kratos]
# Kratos runs as a managed service (like Ollama)
# install_command and service lifecycle are declared in the extension manifest
public_url = "http://localhost:4433"
admin_url = "http://localhost:4434"

# Self-registration
allow_registration = true
require_verification = true    # Email verification before login

# First user bootstrapping
bootstrap_admin = true         # First registered user gets admin role

[auth.oauth]
# Pre-shipped OAuth providers (configure client IDs to enable)
# Client secrets stored via credential provider (Keychain/Vault)

[auth.oauth.google]
enabled = true
client_id = ""                 # From Google Cloud Console
allowed_domains = []           # ["example.org"] to restrict

[auth.oauth.microsoft]
enabled = true
client_id = ""
tenant = "organizations"       # "common" | "organizations" | specific tenant

[auth.oauth.github]
enabled = true
client_id = ""
allowed_orgs = []              # ["example-org"] to restrict

[auth.oauth.gitlab]
enabled = false
client_id = ""
instance_url = "https://gitlab.example.org"

# Custom OIDC provider (any standards-compliant IdP)
[auth.oauth.custom]
enabled = false
display_name = "Facility SSO"
discovery_url = "https://sso.facility.gov/.well-known/openid-configuration"
client_id = ""
scopes = ["openid", "profile", "email", "groups"]
# Map IdP group claims to Axiom roles
role_mapping = { "facility-admin" = "admin", "ec-cleared" = "export_controlled_access" }

[auth.ldap]
enabled = false
url = "ldap://ad.facility.gov:389"
base_dn = "dc=facility,dc=gov"
user_filter = "(sAMAccountName={username})"
tls = true
```

### FR-ID-003: `axiom login` / `axiom logout` / `axiom whoami`

```bash
axiom login
# If OAuth providers configured:
#   Log in with:
#     1. Google (example.org)
#     2. Microsoft
#     3. Local account
#   > 1
#   Opening browser for Google login...
#   ✓ Logged in as user@example.org
#
# If only local:
#   Email: user@example.org
#   Password: ****
#   ✓ Logged in as user@example.org

axiom whoami
# user@example.org (Google OAuth)
# Session: 6d 23h remaining
# MFA: verified (TOTP)
# Roles: export_controlled_access, admin

axiom logout
# ✓ Session cleared
```

### FR-ID-004: Identity in Agent Context

Every agent action carries the authenticated user:

```python
session = get_current_session()
# session.user_id = "user@example.org"
# session.display_name = "Benjamin Booth"
# session.roles = ["export_controlled_access", "admin"]
# session.mfa_verified = True

# All bus events include user_id
bus.publish("cli.command", {"user_id": session.user_id, ...})
# Audit logs trace to real people, not "axiom"
```

### FR-ID-005: Identity Feeds the Signal Correlator

The Kratos user registry replaces the static `people.md` correlator
config. When SCAN extracts a signal mentioning a person, it resolves
against Kratos identities — not a hand-maintained markdown file.

**Migration path:**
- v0.4.x: Static `people.md` (bootstrap seed)
- v0.5.x: Correlator queries Kratos identity store; `people.md` deprecated
- v0.6.x: Correlator enriches with OpenFGA roles and team memberships

**Correlator integration:**
```python
# Current (static):
people = parse_markdown("runtime/config/people.md")

# Future (Kratos):
people = kratos.list_identities(traits=["email", "name", "organization"])
# Each person carries roles from OpenFGA:
# { "email": "user@example.org", "roles": ["admin", "ec_access"], "team": "PartnerLab" }
```

This ensures the people list is always current, new team members are
automatically discoverable by SCAN, and departed members are removed
when their Kratos identity is deactivated.

### FR-ID-006: Kratos as Managed Service

Kratos is deployed like Ollama — declared in an extension manifest and
managed by `ServiceManager`:

```toml
# identity extension manifest
[[connections]]
name = "kratos"
display_name = "Ory Kratos (Identity)"
kind = "cli"
endpoint = "kratos"
credential_type = "none"
health_check = "http_get"
health_endpoint = "http://localhost:4433/health/alive"
category = "identity"
ensure_module = "axiom.extensions.builtins.identity.connections"
ensure_function = "ensure_kratos_running"
post_setup_module = "axiom.extensions.builtins.identity.connections"
post_setup_function = "setup_kratos"

[connections.install_commands]
macos = "brew install ory/tap/kratos"
linux = "bash <(curl https://raw.githubusercontent.com/ory/meta/master/install.sh) -b /usr/local/bin kratos"
```

### FR-ID-006: OpenFGA as Managed Service

OpenFGA runs alongside Kratos, also managed by `ServiceManager`:

```toml
# identity extension manifest
[[connections]]
name = "openfga"
display_name = "OpenFGA (Authorization)"
kind = "cli"
endpoint = "openfga"
credential_type = "none"
health_check = "http_get"
health_endpoint = "http://localhost:8080/healthz"
category = "identity"
capabilities = ["read", "write"]
ensure_module = "axiom.extensions.builtins.identity.connections"
ensure_function = "ensure_openfga_running"
post_setup_module = "axiom.extensions.builtins.identity.connections"
post_setup_function = "setup_openfga"

[connections.install_commands]
macos = "brew install openfga/tap/openfga"
linux = "bash <(curl https://raw.githubusercontent.com/openfga/openfga/main/install.sh) -b /usr/local/bin openfga"
```

**Full identity stack (all managed services):**

```
axiom connect kratos    → Install + register service + create DB schema
axiom connect openfga   → Install + register service + load auth model
axiom login             → Kratos authenticates → webhook writes OpenFGA tuples
axiom chat              → Gateway checks OpenFGA before routing to EC tier
axiom status            → Shows Kratos + OpenFGA health
```

---

## Part 2: Credential Management

### Problem

Axiom integrates with 10+ external services, each requiring API keys or
tokens. Today credentials are stored in `.env` files (plaintext on disk).
No rotation, no expiry detection, no multi-environment support.

### Solution: Credential Provider Pattern

Pluggable backends with OS Keychain as default:

| Provider | Platform | Encrypted | Persistent | Production |
|----------|----------|-----------|-----------|-----------|
| **EnvironmentProvider** | Any | No | No | CI/CD only |
| **KeychainProvider** | macOS | Yes (hardware) | Yes | Dev machines |
| **SecretServiceProvider** | Linux | Yes | Yes | Dev machines |
| **WindowsCredentialProvider** | Windows | Yes | Yes | Dev machines |
| **VaultProvider** | Any | Yes (AES-256) | Yes | Production |
| **FileProvider** | Any | No (disk only) | Yes | Fallback |

### FR-CM-001: Resolution Chain

```
get_credential("anthropic")
├─ 1. Environment variable      $ANTHROPIC_API_KEY           ← CI/CD
├─ 2. OS Keychain               com.axiom.anthropic     ← dev default
├─ 3. Vault                     secret/axiom/anthropic        ← production
├─ 4. Credential file           ~/.axi/credentials/ (0600)  ← fallback
└─ 5. Return None               (caller degrades gracefully)
```

### FR-CM-002: Credential Metadata

Every credential carries lifecycle data:

```python
@dataclass
class CredentialMetadata:
    saved_at: str          # ISO timestamp
    saved_by: str          # "axiom connect" | "vault-sync"
    expires_at: str        # ISO timestamp or "" (unknown)
    last_verified: str     # Last successful health check
    last_used: str         # Last get_credential() call
    provider: str          # "keychain" | "vault" | "file"
    rotation_url: str      # Where to rotate the credential
```

### FR-CM-003: Agent Roles in Credential Lifecycle

| Agent | Role | Events |
|-------|------|--------|
| **TIDY** | Credential steward — monitors expiry, periodic verification, stale detection | `credentials.expiring`, `credentials.expired`, `credentials.stale` |
| **TRIAGE** | Credential doctor — auto-recovers 401/403 errors, prompts rotation | Subscribes to `connections.unhealthy`, `credentials.expired` |
| **SCAN** | Credential sentinel — scans git/inbox for accidentally committed secrets | `security.credential_leak` |

### FR-CM-004: `axiom connect` Keychain Integration

```bash
axiom connect anthropic
# → Saves to OS Keychain (primary) + file (backup)
# → Records metadata: saved_at, provider
# → Health checks immediately
# → TIDY schedules periodic re-verification

axiom connect --migrate
# → Migrates .env credentials to OS Keychain
```

---

## Part 3: EC Defense & Authorization

> **This section applies only to deployments with export-controlled providers
> configured** (`routing_tier = "export_controlled"` in `llm-providers.toml`).
> Facilities with no EC providers — non-domain-specific labs, universities, research
> environments — do not require any of the features in this section.
> Axiom operates fully without them.

### Problem

Axiom handles export-controlled data regulated under 10 CFR 810.
Classification routes queries to appropriate endpoints, but classification
alone is not a complete security posture. Authorization (who may access),
defense (sanitization, scanning), and audit (tamper-evident logging) are
required layers.

These layers are **activated automatically when an EC provider is configured**
and are **no-ops in all other deployments**.

### Solution: Layered Defense + OpenFGA

**Defense layers (EC deployments only):**
- Layer 1: Export control classification (keyword + Ollama SLM) — always active when EC provider exists
- Layer 2: VPN network boundary (physical isolation)
- Layer 3: Chunk sanitization before LLM injection (FR-EC-001)
- Layer 4: System prompt hardening for EC sessions (FR-EC-002)
- Layer 5: Response scanning at network boundary (FR-EC-003)
- Layer 6: Session suspension on repeated leakage (FR-EC-004)
- Layer 7: Store quarantine for EC content in public RAG (FR-EC-005)

**Authorization (Phase 3):**
- OpenFGA for fine-grained RBAC + ReBAC + ABAC
- Connection-level access control (who can use which LLM provider)
- Document-level access control (who can read which RAG corpus)
- Identity from Kratos feeds into OpenFGA

### FR-EC-001: Chunk Sanitization

Strip injection patterns from RAG chunks before LLM injection:

| Pattern | Threat |
|---------|--------|
| `[tool:` | Tool call injection |
| `SYSTEM:` | Role override |
| `ignore previous instructions` | Instruction override |
| `override routing` | Routing manipulation |

### FR-EC-002: System Prompt Hardening

Non-negotiable security instructions prepended to EC session system prompts.
Cannot be overridden by user messages or RAG content.

### FR-EC-003: Response Scanning

Classify LLM responses before they cross the network boundary. Withhold
responses containing EC keyword matches.

### FR-EC-004: Session Suspension

Terminate sessions after N leakage events (configurable, default: 2).
Escalate persistent patterns via webhook.

### FR-EC-005: Store Quarantine

Background scan public RAG stores for EC content. Quarantine immediately,
preserve for forensic investigation.

### FR-EC-006: Security Audit Log (PostgreSQL)

> **Superseded.** All logging requirements have been moved to
> [prd-logging.md](prd-logging.md). FR-EC-006 is replaced by FR-LOG-001
> through FR-LOG-005 in that document. The routing audit log is now scoped to
> Phase 1 (v0.5.x) rather than Phase 5 (v0.6.x) because it is a prerequisite
> for confident EC operations.

HMAC-protected `routing_events` table. SHA-256 hashed queries/responses
(no plaintext EC data in audit log). See [Logging PRD](prd-logging.md) for
the full schema, HMAC chain design, and `axiom log` CLI specification.

### FR-EC-007: OpenFGA Authorization

```
classify(query) → tier = "export_controlled"
  → select_provider() → "private-qwen"
    → Kratos: who is this user?
      → OpenFGA: check(user, "private-qwen", "can_access")
        → allowed? → proceed / deny with guidance
```

**Roles:** `public_access`, `export_controlled_access`, `admin`,
`compliance_officer`

---

## Phased Implementation

### Phase 1: Connections + TRIAGE Self-Healing (v0.4.x) ✅ SHIPPED

- Connection registry, credential resolution chain
- `axiom connect` CLI with tab completion, health checks
- Managed service lifecycle (launchd/systemd/Windows)
- TRIAGE subscribes to connection events for auto-recovery
- Capabilities (read/write), usage tracking, throttle detection

### Phase 2: OS Keychain + Credential Metadata (v0.5.0)

- KeychainProvider (macOS), SecretServiceProvider (Linux), WindowsCredentialProvider
- `axiom connect` saves to Keychain by default
- CredentialMetadata (saved_at, expires_at, last_verified)
- `axiom connect --migrate` from .env to Keychain
- TIDY expiry watch + SCAN secret scanning

### Phase 3: Identity (Ory Kratos) + Local Auth (v0.5.x)

- Kratos deployed as managed service
- `axiom login` / `axiom logout` / `axiom whoami`
- Local registration + TOTP MFA
- Session management
- Identity in agent context + audit logs

### Phase 4: OAuth + Institutional SSO (v0.6.0)

- Google, Microsoft, GitHub, GitLab OAuth via Kratos
- LDAP/AD integration via Kratos
- Generic OIDC + SAML providers
- `auth.toml` configuration
- Role mapping from IdP claims

### Phase 1.5: EC Routing Audit Log (v0.5.x)

- Routing audit log (FR-LOG-001 through FR-LOG-005) — moved forward from Phase 5
  as a prerequisite for confident EC operations
- See [prd-logging.md](prd-logging.md) for full scope

### Phase 5: EC Defense Layers (v0.6.x)

- Chunk sanitization (FR-EC-001)
- System prompt hardening (FR-EC-002)
- Response scanning (FR-EC-003)
- Identity-enriched audit log (FR-EC-006 Phase 2 — see prd-logging.md)
- `axiom doctor --security`
- Red-team test suite (promptfoo)

### Phase 6: OpenFGA Authorization (v0.7.0)

- OpenFGA sidecar deployment
- Connection-level + document-level access control
- Kratos identity → OpenFGA roles
- Per-user tier access configuration

### Phase 7: Vault + Rotation Automation (v0.7.x)

- VaultProvider for production deployments
- Automatic credential rotation where APIs support it
- `axiom connect --migrate --target vault`
- Dynamic secrets for database credentials

---

## Success Metrics

| Domain | Metric | Target |
|--------|--------|--------|
| **Identity** | Login-to-first-command time | < 10 seconds |
| **Identity** | Audit attribution coverage | 100% of commands |
| **Credentials** | Credentials in Keychain (dev) | 100% (no plaintext) |
| **Credentials** | Time from expiry to notification | < 14 days before |
| **Credentials** | Leaked secrets detected by SCAN | 100% of known patterns |
| **EC Defense** | Injection patterns caught | 100% of red-team suite |
| **EC Defense** | EC keyword leakage caught | 100% of verbatim matches |
| **EC Defense** | False positive rate | < 1% of EC responses |
| **Authorization** | Unauthenticated EC access | 0% (blocked) |

---

## Open Questions

1. Should Kratos run in the K3D cluster alongside PostgreSQL, or as a
   standalone binary managed by ServiceManager?
2. How do we handle the first user bootstrapping in an air-gapped
   environment? (Proposal: first `axiom login` creates admin account)
3. Should `axiom chat` require login? (Proposal: configurable, default
   write-commands-only)
4. How do service accounts (CI/CD) authenticate? (Proposal: API keys
   stored in Kratos, scoped permissions via OpenFGA)
5. Should OAuth refresh tokens be stored in OS Keychain?
6. HMAC key rotation cadence for audit log integrity?
7. Should Kratos UI be served via `axiom serve` or standalone?

---

## Related Documents

- [Connections PRD](prd-connections.md) — Connection abstraction, `axiom connect` UX
- [Connections Spec](../tech-specs/spec-connections.md) — Credential resolution, health checks
- [Agent Platform PRD](prd-agents.md) — TIDY, TRIAGE, SCAN agent capabilities
- [Executive PRD](prd-executive.md) — Product vision
- [OKRs 2026](prd-okrs-2026.md) — O7 (community), O8 (multi-facility)
- [Federal Data Management PRD](prd-doe-data-management.md) — DOE DMSP requirements

**Federal Data Management Integration:** Data Sharing Agreement (DSA) acceptance gates defined in the DOE DMSP requirements will be integrated into the OpenFGA authorization flow for restricted datasets — users must accept applicable DSA terms before access tuples are written. ORCID identity linking will be supported as an optional identity attribute in the Kratos identity schema, enabling researcher attribution for PID-bearing datasets. See [Federal Data Management PRD](prd-doe-data-management.md).
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
