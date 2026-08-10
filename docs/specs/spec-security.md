# Axiom Security Spec

**Status:** Draft
**Owner:** Ben Booth
**Created:** 2026-03-18
**Last Updated:** 2026-03-18
**PRD:** [Security PRD](../prds/prd-security.md)

**Scope:** Identity (Ory Kratos), AuthN (OAuth/LDAP/SAML/OIDC/local), AuthZ
(OpenFGA — RBAC + ReBAC + ABAC), Credential & Key Management (Keychain/Vault),
Export Control Defense (8-layer), Audit Logging (PostgreSQL + HMAC)

---

## 1. Architecture Overview

Three systems, one design surface:

```
┌─────────────────────────────────────────────────────────┐
│                    axiom CLI / agents                     │
│                                                         │
│  axiom login ─────→ Ory Kratos (identity)                │
│                       ↓ post-login webhook              │
│                    Axiom hook-service                │
│                       ↓ write tuples                    │
│                    OpenFGA (authorization)               │
│                       ↑ check(user, resource, relation) │
│  axiom chat ──→ Gateway ──→ OpenFGA gate ──→ LLM        │
│                                                         │
│  get_credential() ──→ Credential Provider Stack         │
│                       ├─ OS Keychain                    │
│                       ├─ Vault                          │
│                       └─ File (fallback)                │
└─────────────────────────────────────────────────────────┘
```

**Services (all managed by ServiceManager):**

| Service | Binary | Port | Storage | Purpose |
|---------|--------|------|---------|---------|
| Ory Kratos | `kratos` | 4433 (public), 4434 (admin) | PostgreSQL | Identity + authentication |
| OpenFGA | `openfga` | 8080 (HTTP), 8081 (gRPC) | PostgreSQL | Authorization |
| PostgreSQL | via K3D | 5432 | Disk | Shared backend |

---

## 2. Trust Model: Deterministic vs Model-Mediated

Axiom has two fundamentally different kinds of behavior, and
conflating them is a security bug. Every feature, agent action, and
documented workflow must be classified as one or the other, and the
classification must be made visible to the reader.

### 2.1 The Two Categories

**Deterministic behavior** — code plus explicit policy plus
cryptographic verification. Testable, provable, replayable.
Categories include:

- Authorization checks (OpenFGA relation lookups, RACI gate enforcement)
- Identity verification (Ed25519 signature checks, TOFU key binding,
  fingerprint comparison)
- Schema validation (JSON schema, dataclass type checks, migration
  integrity)
- Version compatibility checks (`MIN_PEER_VERSION_FOR_IDENTITY_BINDING`,
  migration window enforcement)
- Export-control and classification-tier gates (public / restricted /
  export_controlled — see content-tier model in
  `spec-rag-architecture.md`)
- Cryptographic revocation propagation

**Model-mediated behavior** — LLM judgment.
Non-deterministic, approximate, useful for *shaping* and *classifying*
but **not for authorizing**. Categories include:

- Classification and triage (signal severity, content relevance)
- Natural-language policy interpretation
- Writing assistance, summarization, briefing generation
- Install/upgrade guidance narration (Bonsai LM explaining why a
  version bump matters, helping an operator reason through a
  rare human-decision point)
- Agent workflow steering via SKILLS.md (tone, domain focus,
  workflow style)
- LLM-as-judge evaluation (recommendation, never enforcement)

### 2.2 The Rule

**Authorization is ALWAYS deterministic.** RACI + OpenFGA + signature
check. An LLM output never grants or denies access to any resource
or action.

**Classification can be model-mediated**, but results that feed into
an authorization decision must flow through a deterministic gate
afterward. A model may classify a document as `restricted`, but the
act of refusing access is a code check against the classified tag,
not the LLM's say-so.

**Guidance, narration, UX enrichment** is model-mediated; the
underlying action being narrated is deterministic. Bonsai LM may
say "I recommend upgrading — here's why"; the actual pip install +
signature check + version bump + validation sequence is
deterministic code.

### 2.3 SKILLS.md Files Are Model-Mediated

Every agent's `SKILLS.md` file falls on the model-mediated side. It
shapes an agent's behavior within already-granted capabilities. It
**never grants capability**. If a malicious actor edited a
SKILLS.md to claim the agent could bypass RACI or access restricted
data, nothing would actually change — the deterministic gates would
refuse, because authorization is enforced in code. The blast radius
of SKILLS.md tampering is *behavioral misbehavior* (agent acts
weird, gives bad advice, lies about what it's doing), not
*authorization bypass*.

This is a deliberate design property: SKILLS.md files are
untrusted-shaping-only. They can be authored, reviewed, modified,
and even federated across nodes without introducing capability
leakage, because capability is not what they govern.

Every SKILLS.md must contain an explicit "Authorization Model"
section that states this boundary for its agent: which actions
flow through deterministic gates (and cite them), which aspects
of behavior are LLM-shaped.

### 2.4 Installation and Upgrade Assistance

Install and upgrade flows benefit from model-mediated assistance
precisely because they occasionally require human judgment
("peer key rotation detected — is this expected?", "upgrade
skipped a version — roll forward or investigate?"). Bonsai LM
narrates these moments, surfaces context, and elicits reasoning.

Constraint: the underlying action must remain deterministic. Bonsai
may say "upgrading is recommended"; the pip install, wheel
signature check, migration integrity check, and validation smoke
test must all be code-verified. If Bonsai is unavailable, the
deterministic flow still runs with plain-text output — no action
is gated on LLM availability.

This policy applies equally to `axi nodes add` key-rotation
refusal, `axi update` version-skew warnings, `axi install-shim`
PATH-setup guidance, and any future install/upgrade entry point.

### 2.5 Labeling Convention in Docs

Documents that describe agent behavior, federation protocols, or
decision flows must label sections where ambiguity is possible.
Adopted conventions:

- **[deterministic]** — section describes behavior with hard
  guarantees from code, policy, or cryptography.
- **[model-mediated]** — section describes LLM-shaped behavior.
- **[hybrid]** — section involves both; must explicitly describe
  the handoff (e.g. "LLM classifies the signal; RACI gate decides
  whether to act on it").

Unlabeled sections are presumed deterministic. A section that is
actually model-mediated but labeled or defaulted to deterministic
is a documentation bug and a potential source of operator
misunderstanding.

### 2.6 Validated Classification — Canonical [hybrid] Pattern

Many properties in the system are declared statically today (node
profile, trust level, content tier, agent capability, federation
relationship type) but the declaration becomes wrong over time as
reality drifts. **Validated classification** is the [hybrid]
pattern that addresses this:

1. Declare the property statically at creation time.
2. Periodically (not per-action — cost/latency matter at scale),
   the node's built-in LM re-validates the declaration against
   observed behavior and evidence.
3. The LM emits an advisory re-classification with confidence and
   cited evidence.
4. A deterministic gate decides whether the advisory triggers an
   operator-approval prompt, an automatic change (only under
   narrow, policy-bounded conditions), or a log entry.

**The LM's classification is advisory, never authoritative.** It is
a better starting point than a stale static declaration, but it
does not itself confer privilege. Privilege change still flows
through deterministic code — RACI approval, policy match, or an
explicit operator action.

**Good candidates:** properties that drift with time or scale.
Node profile (workload changes), trust level (accumulated
interaction history), content tier (re-reading reveals hidden
export-controlled content), agent capability (actual workflow
demonstrates more/less than advertised), federation relationship
(partnerships dormant, consortiums active).

**Bad candidates:** cryptographic primitives (root keys,
signatures, hashes), schema versions, identity roots. These are
hard-versioned and must never be re-classified by heuristic.

**Audit cadence:** daily to weekly per property, not per
transaction. Confidence thresholds are policy knobs
(`confidence > 0.9 across 30 days of consistent signal` for
auto-promotion; lower thresholds only surface advisory prompts,
never take action).

**Log the delta:** every validated-classification cycle records
`declared X, validated Y, confidence Z, evidence [...]`. This is
audit material — drift detection often matters more than the
classification itself.

### 2.7 Review Questions

When reviewing any proposed agent behavior or system response, ask:

1. **"What's the deterministic check backing this?"** If the answer
   is "the LLM decides," the proposal is not acceptable for
   anything involving authorization or data-tier gating —
   redesign required.
2. **"Is this declaration going to drift?"** If a property is
   declared statically but reality will change it over time,
   consider the validated-classification pattern (§2.6) instead
   of hoping the declaration stays accurate.

---

## 3. Identity: Ory Kratos

### 4.1 Deployment

Kratos runs as a managed service via `ServiceManager`:

```
axiom connect kratos
    → brew install ory/tap/kratos (macOS)
    → ServiceManager registers com.axiom.kratos
    → Kratos schema migrated: kratos migrate sql postgres://...
    → Config generated: ~/.axi/services/kratos.yml
    → Service started (RunAtLoad, KeepAlive)
```

### 3.2 Kratos Configuration

Generated by `setup_kratos()` based on `auth.toml`:

```yaml
# ~/.axi/services/kratos.yml (generated, not hand-edited)
version: v1.2.0

dsn: postgres://axiom:axiom@localhost:5432/axiom_kratos?sslmode=disable

serve:
  public:
    base_url: http://localhost:4433/
    cors:
      enabled: true
  admin:
    base_url: http://localhost:4434/

selfservice:
  default_browser_return_url: http://localhost:19821/
  flows:
    login:
      ui_url: http://localhost:19821/login
      after:
        hooks:
          - hook: web_hook
            config:
              url: http://localhost:19822/hooks/post-login
              method: POST
              body: base64://...  # JSON template
    registration:
      ui_url: http://localhost:19821/register
      after:
        hooks:
          - hook: web_hook
            config:
              url: http://localhost:19822/hooks/post-registration
              method: POST
              body: base64://...

identity:
  default_schema_id: default
  schemas:
    - id: default
      url: file:///~/.axi/services/identity.schema.json

# OAuth / social sign-in (populated from auth.toml)
selfservice:
  methods:
    oidc:
      enabled: true
      config:
        providers: []  # Populated dynamically from auth.toml
    password:
      enabled: true
    totp:
      enabled: true
    webauthn:
      enabled: true
```

### 3.3 Identity Schema

```json
{
  "$id": "https://axiom.dev/identity.schema.json",
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Axiom Identity",
  "type": "object",
  "properties": {
    "traits": {
      "type": "object",
      "properties": {
        "email": {
          "type": "string",
          "format": "email",
          "ory.sh/kratos": {
            "credentials": { "password": { "identifier": true } },
            "verification": { "via": "email" },
            "recovery": { "via": "email" }
          }
        },
        "name": {
          "type": "object",
          "properties": {
            "first": { "type": "string" },
            "last": { "type": "string" }
          }
        },
        "organization": { "type": "string" },
        "facility": { "type": "string" }
      },
      "required": ["email"]
    }
  }
}
```

### 3.4 OAuth Provider Configuration

OAuth providers are configured in Kratos via `auth.toml`. The `setup_kratos()`
hook reads `auth.toml` and generates the Kratos OIDC provider config:

```python
# Pseudo-code for setup_kratos()
def _build_oidc_providers(auth_config: dict) -> list[dict]:
    providers = []
    for name, cfg in auth_config.get("oauth", {}).items():
        if not cfg.get("enabled") or not cfg.get("client_id"):
            continue
        providers.append({
            "id": name,
            "provider": _provider_type(name),  # "google" | "microsoft" | "github" | "generic"
            "client_id": cfg["client_id"],
            "client_secret": get_credential(f"oauth_{name}_secret"),
            "mapper_url": f"file:///~/.axi/services/mappers/{name}.jsonnet",
            "scope": cfg.get("scopes", ["openid", "profile", "email"]),
        })
    return providers
```

### 3.5 CLI Auth Flow

```
axiom login
    ↓
1. Check if Kratos is running (ensure_available("kratos"))
2. Fetch available login methods from Kratos API
3. Present choices to user (OAuth providers + local)
4. User selects provider:
   a. OAuth → open browser to Kratos /self-service/login?provider=google
       → Google authenticates → redirect to localhost:19821/callback
       → Kratos creates session → returns session token
   b. Local → prompt email + password → POST to Kratos login flow
       → Kratos verifies → returns session token
5. Store session token at ~/.axi/session.json (0600)
6. Post-login webhook fires → OpenFGA tuples written
7. Print: "✓ Logged in as user@example.org"
```

---

## 4. Authorization: OpenFGA

### 4.1 Deployment

```
axiom connect openfga
    → brew install openfga/tap/openfga (macOS)
    → ServiceManager registers com.axiom.openfga
    → OpenFGA migrations: openfga migrate --datastore-engine postgres
    → Auth model loaded: openfga model write
    → Service started
```

### 4.2 Authorization Model

```dsl
model
  schema 1.1

type user

type role
  relations
    define member: [user]

type connection
  relations
    define can_access: [user, role#member]
    define admin: [user, role#member]

type rag_corpus
  relations
    define can_query: [user, role#member]
    define can_index: [user, role#member]

type document
  relations
    define owner: [user]
    define can_read: owner or can_query from parent_corpus
    define can_write: owner or admin from parent_connection
    define parent_corpus: [rag_corpus]
    define parent_connection: [connection]
```

### 4.3 Predefined Roles

| Role | Tuples Written | Grants |
|------|---------------|--------|
| `public_access` | `user:X → role:public_access#member` | Public tier only |
| `export_controlled_access` | `user:X → role:ec_access#member` + `role:ec_access → connection:private-qwen#can_access` | EC tier + VPN providers |
| `admin` | `user:X → role:admin#member` + `role:admin → connection:*#admin` | All connections + config |
| `compliance_officer` | `user:X → role:compliance#member` | Read-only audit access |

### 4.4 Webhook Bridge (Kratos → OpenFGA)

Axiom runs a lightweight hook-service that receives Kratos webhooks
and writes OpenFGA tuples:

```python
# src/axiom/infra/auth/webhook.py

async def post_login_hook(identity: dict) -> None:
    """Called by Kratos after successful login."""
    user_id = identity["traits"]["email"]
    org = identity["traits"].get("organization", "")

    # Read role mapping from auth.toml
    role_mapping = load_role_mapping()

    # Extract groups from IdP claims (if OAuth login)
    idp_groups = identity.get("metadata_public", {}).get("groups", [])

    # Write base tuple (user exists)
    openfga.write(user=user_id, relation="member", object="role:public_access")

    # Map IdP groups to Axiom roles
    for idp_group, axi_role in role_mapping.items():
        if idp_group in idp_groups:
            openfga.write(user=user_id, relation="member", object=f"role:{axi_role}")

    # First-user bootstrap: if no admin exists, make this user admin
    if not openfga.has_any("role:admin#member"):
        openfga.write(user=user_id, relation="member", object="role:admin")
```

### 4.5 Authorization Check in Gateway

```python
# In gateway._select_provider():

def _select_provider(self, tier, session):
    provider = self._find_provider(tier)

    # Authorization gate (if OpenFGA is running)
    if self._openfga_available():
        allowed = openfga.check(
            user=f"user:{session.user_id}",
            relation="can_access",
            object=f"connection:{provider.name}",
        )
        if not allowed:
            return GatewayResponse(
                text=f"Access denied: your role does not allow {provider.name}. "
                     f"Contact your administrator.",
                success=False,
                provider="auth_gate",
            )

    return self._call_provider(provider, ...)
```

---

## 5. Credential Providers

### 5.1 Provider Implementations

```python
class CredentialProvider(ABC):
    @abstractmethod
    def name(self) -> str: ...
    @abstractmethod
    def available(self) -> bool: ...
    @abstractmethod
    def get(self, service: str) -> str | None: ...
    @abstractmethod
    def store(self, service: str, value: str, metadata: CredentialMetadata | None = None) -> bool: ...
    @abstractmethod
    def delete(self, service: str) -> bool: ...
```

### 5.2 macOS: KeychainProvider

```python
class KeychainProvider(CredentialProvider):
    SERVICE_PREFIX = "com.axiom"

    def get(self, service):
        result = subprocess.run([
            "security", "find-generic-password",
            "-s", f"{self.SERVICE_PREFIX}.{service}",
            "-a", "axiom", "-w",
        ], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    def store(self, service, value, metadata=None):
        self.delete(service)  # Remove existing
        comment = json.dumps(metadata.to_dict()) if metadata else ""
        subprocess.run([
            "security", "add-generic-password",
            "-s", f"{self.SERVICE_PREFIX}.{service}",
            "-a", "axiom", "-w", value, "-j", comment, "-U",
        ], capture_output=True)
        return True
```

### 5.3 Linux: SecretServiceProvider

```python
class SecretServiceProvider(CredentialProvider):
    def get(self, service):
        result = subprocess.run([
            "secret-tool", "lookup",
            "application", "axiom", "connection", service,
        ], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None
```

### 5.4 VaultProvider

```python
class VaultProvider(CredentialProvider):
    def available(self):
        return bool(os.environ.get("VAULT_ADDR"))

    def get(self, service):
        resp = requests.get(
            f"{self._addr}/v1/secret/data/axiom/{service}",
            headers={"X-Vault-Token": self._token}, timeout=5,
        )
        return resp.json()["data"]["data"].get("token") if resp.ok else None
```

### 5.5 Resolution Chain

```
get_credential("anthropic")
├─ 1. $ANTHROPIC_API_KEY           ← CI/CD, containers
├─ 2. Keychain/SecretService       ← dev machines (encrypted)
├─ 3. Vault                        ← production (if VAULT_ADDR set)
├─ 4. ~/.axi/credentials/ (0600)  ← fallback
└─ 5. None                         ← caller degrades gracefully
```

---

## 6. EC Defense Layers

### 5.1 Defense Architecture

```
Layer 1: Export control classification (keyword + Ollama SLM)
Layer 2: VPN network boundary (physical isolation)
Layer 3: Chunk sanitization (strip injection patterns before LLM)
Layer 4: System prompt hardening (non-negotiable security instructions)
Layer 5: Response scanning (classify response before network boundary)
Layer 6: Session suspension (kill session after N leakage events)
Layer 7: Store quarantine (isolate EC content in public RAG)
Layer 8: OpenFGA authorization gate (independent of classification)
```

### 5.2 Security Audit Log

```sql
CREATE TABLE security_events (
    id              BIGSERIAL PRIMARY KEY,
    event_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,
    session_id      TEXT,
    user_id         TEXT,
    query_hash      TEXT,       -- SHA-256, not plaintext
    response_hash   TEXT,
    matched_terms   TEXT[],
    source_paths    TEXT[],
    provider        TEXT,
    routing_tier    TEXT,
    resolved        BOOLEAN DEFAULT FALSE,
    event_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    hmac            TEXT NOT NULL
);
```

---

## 7. Identity Extension

All identity infrastructure is packaged as a builtin extension:

```
src/axiom/extensions/builtins/identity/
    axiom-extension.toml          # Declares kratos + openfga connections
    __init__.py
    cli.py                       # axiom login / logout / whoami
    connections.py               # setup_kratos(), ensure_kratos_running(),
                                 # setup_openfga(), ensure_openfga_running()
    webhook.py                   # Kratos → OpenFGA hook-service
    session.py                   # Session management (~/.axi/session.json)
    tests/
```

### 6.1 Extension Manifest

```toml
[extension]
name = "identity"
version = "0.5.0"
description = "Identity, authentication, and authorization"
builtin = true
kind = "utility"
module = "platform"

[[cli.commands]]
noun = "login"
module = "axiom.extensions.builtins.identity.cli"
description = "Log in to Axiom"

[[cli.commands]]
noun = "logout"
module = "axiom.extensions.builtins.identity.cli"
description = "Log out of Axiom"

[[cli.commands]]
noun = "whoami"
module = "axiom.extensions.builtins.identity.cli"
description = "Show current identity and roles"

[[connections]]
name = "kratos"
display_name = "Ory Kratos (Identity)"
kind = "cli"
endpoint = "kratos"
credential_type = "none"
health_check = "http_get"
health_endpoint = "http://localhost:4433/health/alive"
category = "identity"
capabilities = ["read", "write"]
ensure_module = "axiom.extensions.builtins.identity.connections"
ensure_function = "ensure_kratos_running"
post_setup_module = "axiom.extensions.builtins.identity.connections"
post_setup_function = "setup_kratos"

[connections.install_commands]
macos = "brew install ory/tap/kratos"
linux = "bash <(curl https://raw.githubusercontent.com/ory/meta/master/install.sh) -b /usr/local/bin kratos"

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

---

## 8. Implementation Plan

### Phase 2: OS Keychain + Credential Metadata (v0.5.0)

- KeychainProvider, SecretServiceProvider, WindowsCredentialProvider
- CredentialMetadata (saved_at, expires_at, last_verified)
- `axiom connect --migrate` from .env to Keychain
- TIDY expiry watch

### Phase 3: Identity (Kratos + Local Auth) (v0.5.x)

- Deploy Kratos as managed service
- Identity extension with `axiom login` / `axiom logout` / `axiom whoami`
- Local registration + TOTP MFA
- Session management
- Identity in agent context + audit logs

### Phase 4: OAuth + SSO (v0.6.0)

- Google, Microsoft, GitHub, GitLab OAuth via Kratos
- LDAP/AD, generic OIDC, SAML via Kratos
- `auth.toml` configuration
- IdP claim → role mapping

### Phase 5: OpenFGA Authorization (v0.6.x)

- Deploy OpenFGA as managed service
- Kratos → OpenFGA webhook bridge
- Connection-level + document-level access control
- Gateway authorization gate
- `axiom admin grant/revoke` commands

### Phase 6: EC Defense Layers (v0.7.0)

- Chunk sanitization, response scanning, prompt hardening
- Security audit log (PostgreSQL + HMAC)
- `axiom doctor --security`
- Red-team test suite (promptfoo)

### Phase 7: Vault + Rotation (v0.7.x)

- VaultProvider for production
- `axiom connect --migrate --target vault`
- Automatic credential rotation

---

## Related Documents

- [Connections PRD](../requirements/prd-connections.md) — Connection abstraction
- [Connections Spec](spec-connections.md) — `get_credential()`, health checks
- [Agent Architecture Spec](spec-agent-architecture.md) — TIDY, TRIAGE, SCAN
- [Model Routing Spec](spec-model-routing.md) — EC classification
- [Executive PRD](../requirements/prd-executive.md)
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
