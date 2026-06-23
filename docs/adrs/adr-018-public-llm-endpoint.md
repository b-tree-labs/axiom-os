# ADR-018: Exposing a Self-Hosted LLM Endpoint Beyond VPN

**Status:** Proposed
**Owner:** Ben Booth
**Created:** 2026-04-07
**Deciders:** Ben Booth, the org IT/security contact
**Related:** `spec-federation.md`, `spec-rag-architecture.md`, `adr-013-k3d-containerd.md`, `prd-knowledge-graph.md`

---

## Context

The self-hosted node is a tower running an NVIDIA RTX PRO 6000 (97 GB VRAM) with a Qwen 122B MoE model. Today, access requires the org VPN — which limits the platform to on-network users with VPN credentials.

The external researcher release (Release 2) requires an external researcher (at a partner institution) to query the node's LLM and RAG corpus from their own machine. Future federated nodes at other partner institutions will need the same access. Requiring each collaborator to have org VPN credentials does not scale and creates an administrative burden on the org IT team.

**The question:** Can we safely expose the node's LLM inference endpoint to the internet without VPN?

**The answer:** Yes — if and only if the LLM is never directly internet-facing. We expose only the Axiom Web API, which authenticates every request via federation node identity (Ed25519 cryptographic signatures) before proxying to the LLM.

---

## Decision

**Expose the Axiom Web API on the node over HTTPS (port 443) with federation authentication. The LLM process (`llama-server`) remains bound to localhost and is never directly accessible from the network.**

---

## Architecture

```
┌─────────────────────── Internet ───────────────────────┐
│                                                         │
│  Researcher Node        Partner Node      Future Node   │
│  (Ed25519 keypair)      (Ed25519 keypair) (future)      │
│       │                      │                          │
│       └──────── HTTPS ───────┘                          │
│                    │                                    │
└────────────────────┼────────────────────────────────────┘
                     │ Port 443 only
                     ▼
┌─────────────────── Node ───────────────────────────────┐
│                                                         │
│  ┌──────────────────────────────────┐                   │
│  │  Caddy Reverse Proxy (K3D)      │ ← TLS termination │
│  │  - Let's Encrypt auto-cert      │                   │
│  │  - Port 443 → Axiom Web API     │                   │
│  └──────────────┬───────────────────┘                   │
│                 │                                       │
│  ┌──────────────▼───────────────────┐                   │
│  │  Axiom Web API (K3D pod)        │                   │
│  │                                  │                   │
│  │  1. Verify Ed25519 signature     │ ← reject unknown │
│  │  2. Check node trust store       │   nodes (401)    │
│  │  3. Enforce access tier          │                   │
│  │  4. Rate limit (per-node)        │                   │
│  │  5. Log request (audit)          │                   │
│  │  6. Proxy to LLM or RAG store   │                   │
│  └──────┬──────────────┬────────────┘                   │
│         │              │                                │
│  ┌──────▼────┐  ┌──────▼──────────┐                     │
│  │ llama-srv │  │ PostgreSQL      │                     │
│  │ localhost │  │ (pgvector)      │                     │
│  │ :41883    │  │ 256k chunks     │                     │
│  │ NOT       │  │ NOT             │                     │
│  │ exposed   │  │ exposed         │                     │
│  └───────────┘  └─────────────────┘                     │
│                                                         │
│  Firewall: ONLY port 443 open inbound                   │
│  SSH: Port 22, VPN only (admin access unchanged)        │
└─────────────────────────────────────────────────────────┘
```

### What Is Exposed

| Service | Port | Accessible From | Authentication |
|---------|------|----------------|----------------|
| Axiom Web API (via Caddy) | 443/HTTPS | Internet | Federation Ed25519 signature (mandatory) |
| SSH | 22 | VPN only | SSH key + VPN (unchanged) |

### What Is NOT Exposed

| Service | Port | Why |
|---------|------|-----|
| `llama-server` (Qwen 122B) | 41883 | Bound to localhost. Only the Axiom Web API can reach it. |
| PostgreSQL (RAG store) | 5432 | K3D internal only. Only the Axiom Web API can query it. |
| K3D API server | 6443 | Localhost only. |

---

## Security Controls (Defense in Depth)

### Layer 1: Network

| Control | Implementation | What It Prevents |
|---------|---------------|------------------|
| TLS everywhere | Let's Encrypt certificate via Caddy auto-HTTPS. No self-signed certs. Certificate transparency logged. | Eavesdropping, MITM |
| Single port | Firewall allows ONLY port 443 inbound from internet. All other ports blocked. | Port scanning, lateral movement |
| SSH unchanged | Port 22 remains VPN-gated. Admin access requires VPN + SSH key. | Unauthorized admin access |
| No direct LLM access | `llama-server` binds to `127.0.0.1:41883`. Not routable from any network interface. | Direct LLM exploitation |
| No direct DB access | PostgreSQL is a K3D ClusterIP service. No NodePort, no LoadBalancer. | Direct database access |

### Layer 2: Authentication (Federation Identity)

| Control | Implementation | What It Prevents |
|---------|---------------|------------------|
| Ed25519 signatures | Every API request includes an `Authorization` header with a signed payload (timestamp + method + path + body hash). The node verifies against its federation trust store. | Unauthorized access, replay attacks |
| Invite-only enrollment | New nodes must be explicitly invited via `axi federation invite`. The invite includes the node's public key. No self-registration, no open enrollment. | Drive-by access, credential stuffing |
| Timestamp validation | Signed requests include a timestamp. Requests older than 5 minutes are rejected. | Replay attacks |
| Node identity binding | Each federation node has a unique Ed25519 keypair generated at `axi setup`. Private key never leaves the node. | Impersonation, credential sharing |
| Mutual verification | Both sides verify identity. The node verifies the caller; the caller verifies the node's response signature. | MITM, response tampering |

**How this compares to VPN:**

| Property | Org VPN | Federation Ed25519 |
|----------|--------|-------------------|
| Identity granularity | Per-person (institutional account) | Per-node (cryptographic keypair) |
| Credential type | Password + 2FA | Ed25519 private key (never transmitted) |
| Revocation | IT disables the institutional account | Admin removes node from trust store (`axi federation revoke`) |
| Audit trail | VPN connection logs | Per-request signed audit log |
| Lateral movement risk | VPN grants campus network access | Node can ONLY access the Axiom API, nothing else |
| Dependency | Org IT VPN infrastructure | Self-contained (no third-party dependency) |

**Federation auth is arguably stronger than VPN** for this use case: VPN grants broad network access to the campus subnet, while federation auth grants access to exactly one API endpoint with per-request cryptographic verification.

### Layer 3: Authorization (Access Tier Enforcement)

| Control | Implementation | What It Prevents |
|---------|---------------|------------------|
| Per-node access tier | Each node is authorized for specific tiers: `public`, `restricted`, or both. Stored in the federation trust store. | Privilege escalation |
| Query-time tier filtering | RAG search filters chunks by `access_tier <= caller_tier` before returning results. A `public`-tier node never sees `restricted` chunks. | Data exfiltration across tiers |
| LLM routing by tier | `public`-tier queries may use cloud LLMs. `restricted`-tier queries MUST route to the node's local Qwen. Enforced by the gateway router. | Restricted content leaking to cloud APIs |
| Export-controlled isolation | EC content exists only on a dedicated compliant enclave (future). This node never stores or serves EC data. This is a physical boundary, not a software control. | EC data exposure |

### Layer 4: Rate Limiting & Abuse Prevention

| Control | Implementation | What It Prevents |
|---------|---------------|------------------|
| Per-node rate limit | Token bucket: 30 requests/minute per node (configurable). Burst of 10. | DoS, runaway agents |
| Token budget | Optional daily token cap per node (e.g., 500k tokens/day). | GPU resource exhaustion |
| Request size limit | Max prompt: 32k tokens. Max context window per request: 128k tokens. | Memory exhaustion |
| Concurrent request limit | Max 2 concurrent inference requests per node. | GPU queue flooding |
| Circuit breaker | If a node exceeds rate limits 3x in 10 minutes, temporarily block for 1 hour. Auto-unblock. | Sustained abuse |

### Layer 5: Audit & Monitoring

| Control | Implementation | What It Prevents |
|---------|---------------|------------------|
| Request logging | Every request logged: node identity, timestamp, endpoint, access tier, tokens consumed, response time. Query text is NOT logged for restricted-tier requests (only a hash). | Undetected access |
| SECUR-T anomaly rules | Existing anomaly detection (5 rules, 30 tests) monitors for: unusual request patterns, tier probing, injection attempts. | Sophisticated attacks |
| Tidy monitoring agent | Node uptime, GPU utilization, request queue depth, error rates. Alerts on anomalies. | Undetected outages or abuse |
| Federation audit sync | Audit events sync to the federation audit log (site-scoped). Admins at any site can review access patterns. | Blind spots in distributed system |

---

## What Could Go Wrong (and Mitigations)

### Scenario 1: Stolen Node Private Key

**Risk:** An attacker obtains a node's Ed25519 private key and impersonates that node.

**Mitigations:**
- Private keys are generated locally and never transmitted over any network
- Admin can revoke a node instantly via `axi federation revoke <node-id>`
- Anomaly detection flags requests from a node that suddenly changes IP, request pattern, or volume
- Rate limits cap damage even if impersonation succeeds

**Residual risk:** Low. Equivalent to a stolen SSH private key — the standard mitigation is revocation.

### Scenario 2: Prompt Injection via RAG

**Risk:** An attacker with a valid node crafts queries designed to extract restricted content through prompt manipulation.

**Mitigations:**
- Tier filtering happens at the SQL level (WHERE clause), not in the prompt. The LLM never sees chunks above the caller's tier.
- RAG sanitizer (already built) scans retrieved chunks for prompt injection patterns before injecting into the LLM context
- Response content is tier-bounded: the LLM can only reference chunks that passed the tier filter

**Residual risk:** Very low. The security boundary is in the database query, not in the LLM's behavior.

### Scenario 3: DDoS / Resource Exhaustion

**Risk:** A valid node (or compromised node) floods the node with requests, monopolizing the GPU.

**Mitigations:**
- Per-node rate limits (30 req/min)
- Concurrent request limit (2 per node)
- Circuit breaker (auto-block after repeated limit violations)
- GPU has finite throughput (~10-20 tok/sec at Q4_K_M) — the queue naturally bounds concurrency

**Residual risk:** Low. A single compromised node can waste its own quota but cannot deny service to other nodes.

### Scenario 4: Node Compromise

**Risk:** An attacker exploits a vulnerability in Caddy, the Axiom Web API, or K3D to gain shell access to the node.

**Mitigations:**
- The node runs Ubuntu 24.04 with automatic security updates
- LUKS full-disk encryption (data at rest)
- K3D containers provide process isolation
- The Axiom Web API is a Python application with no shell execution paths
- SSH remains VPN-gated — even with API compromise, admin access requires VPN + SSH key
- No sensitive credentials stored on the node (federation keys are per-node; the node's key is only the node's identity)

**Residual risk:** Medium. Standard server hardening applies. This risk exists today with VPN access and is not increased by exposing port 443.

---

## What Changes for Existing Users

**Nothing.** VPN access continues to work. SSH remains VPN-only. The API endpoint is additive — it gives federated nodes a new way to reach the node without changing how on-network users access it.

Current users who access the node via VPN can continue to do so. They can also switch to federation auth if they prefer (e.g., their `axi chat` would use Ed25519 auth instead of relying on VPN network access).

---

## Implementation Plan

| Step | Work | Owner | Prerequisite |
|------|------|-------|-------------|
| 1 | Open port 443 on the node firewall (ufw) | Ben + reviewer approval | This ADR approved |
| 2 | Deploy Caddy as K3D ingress with Let's Encrypt auto-cert | Ben | DNS record for `node.example.org` pointing to the node's public IP |
| 3 | Add federation auth middleware to Axiom Web API | Ben | Federation identity code (already built, 254 tests passing) |
| 4 | Add rate limiter middleware | Ben | Step 3 |
| 5 | Add audit logging middleware | Ben | Step 3 |
| 6 | Test from the external researcher's node (partner network, no VPN) | Ben + an external researcher | Steps 1-5 |
| 7 | Document in ops runbook | Ben | Step 6 verified |

**Estimated effort:** 2-3 days for steps 2-5 (federation auth code exists; this is wiring + middleware).

**Rollback:** Close port 443 on firewall. Instant. Zero impact on existing VPN access.

---

## Alternatives Considered

### Alternative 1: Keep VPN Requirement

**Pros:** No new attack surface. John is already comfortable with it.
**Cons:** Every collaborator needs org VPN credentials. Org IT must provision accounts for partner-institution researchers. Does not scale to 10+ federated institutions. The external researcher release is blocked on IT provisioning.

### Alternative 2: WireGuard Mesh VPN

**Pros:** Encrypted tunnel between specific nodes. No org IT dependency.
**Cons:** Every node needs WireGuard configured. Network-level access (not application-level). No per-request auth or audit. More complex than federation auth for the same result.

### Alternative 3: Cloudflare Tunnel / Tailscale

**Pros:** Zero-trust network access without opening ports.
**Cons:** Third-party dependency. Traffic routes through external infrastructure (Cloudflare/Tailscale servers). Restricted-tier data transiting third-party networks violates our access tier model. Not acceptable for site-restricted content.

### Alternative 4: Expose LLM Directly (No API Gateway)

**Rejected.** `llama-server` has no authentication, no rate limiting, no audit logging. Exposing it directly would be equivalent to an open GPU for anyone who finds the port.

---

## Decision Criteria

For John's review — the proposal meets these security requirements:

1. **No anonymous access.** Every request is cryptographically authenticated.
2. **No new credential management burden.** Federation identity is self-provisioned (keypair generated locally). No org IT involvement for new nodes.
3. **Minimal attack surface.** One port (443), one service (Axiom Web API), one protocol (HTTPS). The LLM and database are never network-accessible.
4. **Instant revocation.** A compromised node is removed from the trust store in one command. No IT ticket needed.
5. **Full audit trail.** Every request is logged with cryptographic identity, tier, and resource consumption.
6. **Reversible.** Close the firewall port and we're back to VPN-only. Zero data migration, zero config changes.
7. **Defense in depth.** Five independent security layers. Failure of any single layer does not grant unauthorized access.

---

## Security Audit Requirements

Exposing port 443 requires a formal security audit process before, during, and after go-live. This section defines the audit gates that must pass before the endpoint is activated.

### Pre-Deployment Audit (Gate 1 — Required Before Opening Port 443)

**Org IT Security Office Review**

- Submit this ADR + architecture diagram to the org's Information Security Office (ISO) for review
- Scope: publicly-exposed endpoint on org-managed infrastructure
- Deliverable: Written approval or required modifications from ISO
- Timeline: Submit at least 2 weeks before target go-live date
- Contact: the org ISO (or the org IT/security contact's existing ISO contact)

**Automated Vulnerability Scan**

- Run `nmap` + `nikto` + `testssl.sh` against the Caddy endpoint before opening to federation peers
- Verify: only port 443 responds; TLS 1.3 enforced; no weak ciphers; valid certificate chain; HSTS enabled
- Verify: all other ports (including 22) are not visible from outside the org VPN
- Deliverable: Scan report archived in `docs/security/audit-001-pre-deployment.md`

**Federation Auth Test Suite**

Before any external node connects, execute the following test matrix:

| Test | Method | Expected Result |
|------|--------|-----------------|
| Unauthenticated request | `curl -X POST https://node.example.org/api/v1/rag/search` (no auth header) | 401 Unauthorized |
| Invalid signature | Valid format, wrong key | 401 Unauthorized |
| Expired timestamp | Valid signature, timestamp > 5 min old | 401 Unauthorized |
| Revoked node | Previously valid node, removed from trust store | 401 Unauthorized |
| Tier escalation | `public`-tier node requests `restricted` chunks | 200 OK, but results contain only `public` chunks |
| Replay attack | Capture valid request, replay 10 minutes later | 401 Unauthorized |
| Rate limit | 50 requests in 60 seconds from single node | 429 Too Many Requests after 30th |
| Oversized payload | 1 MB request body | 413 Payload Too Large |
| SQL injection in query | `'; DROP TABLE chunks; --` as query text | 200 OK, no data loss (parameterized queries) |
| Path traversal | `POST /api/v1/../../etc/passwd` | 404 Not Found |
| LLM direct access attempt | `curl https://node.example.org:41883/v1/completions` | Connection refused (not routable) |
| DB direct access attempt | `psql -h node.example.org -p 5432` | Connection refused (not routable) |

- All tests must pass with zero exceptions
- Deliverable: Test results archived in `docs/security/audit-002-auth-test-matrix.md`

### Penetration Test (Gate 2 — Required Before Production Federation Traffic)

**Scope:** External penetration test of the node's HTTPS endpoint by a party who is NOT the developer (Ben Booth).

**Options (in order of rigor):**

1. **Org IT Red Team** — if available through ISO, preferred (institutional credibility)
2. **Peer researcher** — another computational lab member with security background runs the test suite + freestyle exploitation
3. **Automated pentest** — OWASP ZAP or Burp Suite Community against the API endpoint, with custom federation auth plugin

**Minimum test coverage:**
- All items from the Federation Auth Test Suite above
- Freestyle exploitation attempt (2-4 hours, document findings)
- TLS configuration audit (certificate pinning, protocol downgrade, cipher suite)
- API fuzzing (malformed JSON, boundary values, encoding attacks)
- Federation protocol attacks (key confusion, cross-node impersonation, trust store poisoning)

**Deliverable:** Pentest report with severity ratings (Critical/High/Medium/Low/Info). All Critical and High findings must be remediated before production traffic. Medium findings documented with remediation timeline.

**Archive:** `docs/security/audit-003-pentest-report.md`

### Post-Deployment Monitoring (Ongoing)

**Weekly (automated):**
- Review SECUR-T anomaly alerts for the past 7 days
- Check rate limit violation counts per node
- Verify TLS certificate expiry > 30 days (Caddy auto-renews, but verify)
- Verify no new ports have been exposed (automated `nmap` from external vantage point)

**Monthly (manual, 30-minute review):**
- Review audit log summary: total requests by node, tier distribution, error rates
- Review any new federation node enrollments
- Check for CVEs in Caddy, Python, PostgreSQL, llama.cpp — apply patches if needed
- Verify backup and disaster recovery procedures are current

**Quarterly (formal):**
- Re-run the full Federation Auth Test Suite
- Re-run the automated vulnerability scan (`nmap` + `testssl.sh`)
- Review and update this ADR if threat model has changed
- Archive quarterly report: `docs/security/audit-quarterly-YYYY-QN.md`
- Present summary to the org IT/security contact / site leadership

**Annual:**
- Full penetration re-test (same scope as Gate 2)
- Review federation trust store — revoke any nodes that are no longer active collaborators
- Review rate limits and token budgets — adjust based on actual usage patterns

### Incident Response

If a security incident is detected (compromised node, unauthorized access, data exfiltration attempt):

1. **Immediate (< 5 minutes):** Close port 443 on firewall (`sudo ufw deny 443`). This instantly reverts to VPN-only. Zero data loss, zero configuration changes.
2. **Triage (< 1 hour):** Review audit logs to identify scope. Revoke compromised node(s) from trust store.
3. **Notify (< 24 hours):** Inform the org IT/security contact, the org ISO, and affected federation partners.
4. **Remediate:** Fix root cause. Re-run pentest on the fix. Re-open port 443 only after remediation is verified.
5. **Post-mortem:** Document incident, root cause, and preventive measures in `docs/security/incidents/`.

### Audit Archive Structure

```
docs/security/
├── audit-001-pre-deployment.md        (vulnerability scan results)
├── audit-002-auth-test-matrix.md      (federation auth test results)
├── audit-003-pentest-report.md        (penetration test report)
├── audit-quarterly-2026-Q2.md         (first quarterly review)
├── audit-quarterly-2026-Q3.md
├── ...
└── incidents/                         (if any)
    └── INC-001-description.md
```

---

## References

- `spec-federation.md` — Federation identity protocol, Ed25519 key management, trust store
- `spec-rag-architecture.md` — Access tier model (public/restricted/export_controlled)
- `adr-013-k3d-containerd.md` — node infrastructure decisions
- `docs/specs/spec-security.md` — SECUR-T anomaly detection rules
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
