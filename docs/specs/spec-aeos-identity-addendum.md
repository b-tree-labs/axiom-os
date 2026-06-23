# spec-aeos — Identity & Credential Addendum (0.1 → 0.2)

> Addendum to `spec-aeos-0.1.md`. Defines the runtime + manifest contract for
> identity and credentials, per ADR-077 (Progressive Trust) and ADR-075 (SSO/OIDC).
> Adds two surfaces; introduces no new capability kind (providers remain
> `adapter`, runtimes `service`, verbs `skill`→`cmd`).

## 1. Runtime contract — `SkillContext.principal`

Every skill invocation MUST receive a populated principal. `SkillContext` gains:

```python
@dataclass(frozen=True)
class PrincipalContext:
    handle: str                 # @name:context  (matrix-style)
    posture: str                # "open" | "attested" | "sso" | "service"
    assured: bool               # True iff cryptographically/IdP proven
    public_bytes: bytes | None  # present at attested+/sso

# SkillContext gains:
    principal: PrincipalContext   # NEVER None
```

- In `open` posture the principal is OS-derived (`@local:<os-user>@<host>`),
  `assured=False`.
- At `attested`/`sso`/`service`, `assured=True` and `public_bytes` is set.
- Skills MUST NOT special-case "no identity"; there is always a principal. Skills
  that perform consequential actions SHOULD check `principal.assured` /
  `principal.posture` and may trigger step-up (§3).

## 2. Manifest — `[[extension.consumes]]` credential declaration

Extends the existing `consumes` vocabulary so an extension declares what identity
+ credentials it needs; the runtime/wizard satisfies them (ADR-075 §6).

```toml
[[extension.consumes]]
kind = "credential"
idp = "entra"                                  # provider adapter name
scopes = ["https://graph.microsoft.com/Calendars.ReadWrite", "offline_access"]
mode = "delegated"                             # delegated | app_only | device_code
min_posture = "sso"                            # the floor this credential demands
bind = "token_source"                          # how it's injected at runtime

[[extension.consumes]]
kind = "secret"
ref = "openbao://kv/data/axiom/hpc/llm-key"
min_posture = "attested"                       # touching this real key needs proof
require_mfa = true                             # + a FRESH second factor at release time
```

**Principal custody** is itself a pluggable `adapter` (ADR-077 §5b):
`keychain` (default), `badge` (key derived on-demand from biometric — no secret at
rest), `hardware` (token/TPM). `require_mfa` means a *fresh* second-factor
confirmation at credential-release time (a Touch ID / Badge tap), not merely a
session unlock.

The runtime resolves a `consumes.credential` to a `token_source` (ADR-075) and a
`consumes.secret` to a KEEP capability dereference (ADR-055), injecting them into
the extension's config — the extension never sees raw OAuth or raw keys.

## 3. Posture enforcement (the conformance delta)

A conformant runtime MUST:

1. **Populate `ctx.principal`** for every skill, per the node posture
   (`identity.posture`, default `open`).
2. Compute the **effective floor** of an operation as
   `max(node_posture, resource_floor)` where `resource_floor` is the `min_posture`
   of any `consumes` credential/secret it touches.
3. **Step up** the principal to the effective floor *before* dereferencing a
   floored credential — one interactive unlock (keychain) / sign-in (IdP) that
   then persists for the session.
4. **Bind + verify**: at `attested`+, capabilities are signed by the authenticated
   principal and verified before any secret dereference (retiring the
   `b"\x00"` placeholders). At `open`, signing is advisory.
5. **Label provenance**: receipts/audit records MUST carry the principal's
   `posture` + `assured` so `open`-mode provenance is never mistaken for attested.

## 4. Conformance additions

- **AEOS-ID-1.** A skill MUST run under a populated `ctx.principal`.
- **AEOS-ID-2.** A floored credential/secret MUST NOT be dereferenced below its
  effective posture floor.
- **AEOS-ID-3.** Refresh tokens + raw secrets MUST be resolved via the vault /
  KEEP, never embedded in extension code or logs.
- **AEOS-ID-4.** Default node posture is `open` — a runtime MUST NOT demand auth
  unless a node posture or a resource floor requires it.

## 5. Standards alignment

The interactive flow is **OAuth 2.1 + PKCE** — the MCP auth model AEOS already
wraps — so the *public subset* is harness-interoperable. The *AEOS delta*
(requires an AEOS-conformant runtime): KEEP capability-brokering, the local
`attested` principal, posture enforcement, and federation. Matches spec-aeos §1's
public-standard-subset vs federation-governance-delta split.

---

_Copyright (c) 2026 The University of Texas at Austin. Apache-2.0 licensed._
