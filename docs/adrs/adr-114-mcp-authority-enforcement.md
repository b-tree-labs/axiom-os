# ADR-114 — MCP authority enforcement: identity gate + site-rule effect gate

**Status:** Accepted (2026-09-17) — §1 identity gate + §2 effect gate + §3 surface-scoped rules all IMPLEMENTED 2026-09-17
**Deciders:** Benjamin Booth
**Related:** ADR-006 (MCP agentic access), ADR-038 (built-in MCP server), ADR-073 (registry-driven MCP tool surface — the path this hardens), ADR-072 (capability projection / `surfaces`), ADR-056 (skills as functions), ADR-035 (human-principal binding), ADR-077 (local-principal authentication / progressive trust), ADR-026 (ownership model — the "owner"), `spec-builtin-mcp-server.md` (§8 tools, `allowed_principals`), `spec-ec-client-capability.md` (the client-seam EC gate). Supersedes the interim withholding in the P8 program doc for the egress writes once built.

---

## Context

Two authority controls the MCP surface is supposed to have are **declared but not built**,
surfaced while widening the MCP tool surface (ADR-073 registry path, the P8 work):

1. **`allowed_principals` is not enforced.** `spec-builtin-mcp-server.md` §8 says platform tools
   default to owner-only (`@<owner>:local`); `MCPToolDecl.allowed_principals` carries it and
   `axi ext lint` (AEOS072) validates the pattern — but **nothing checks a caller's principal
   against it at call time**, and `mcp/server.py::dispatch_call` carries **no caller identity**.
   The registry-projected tools set no `allowed_principals` at all. So "owner-only" is
   aspirational for *every* MCP tool; the only real bound today is the **transport**
   (owner-local stdio). When the HTTP/bearer transport lands (ADR-038 §9 / D6), that bound
   disappears with nothing behind it.

2. **There is no way to require approval for a specific tool.** Every tool call consults GUARD
   (`authority.authority_pre_invoke_hook`, "P5 step 1") and maps its verdict to
   allow / deny / approval — this path *is* enforced (`invoke_capability`), and a `propose`
   verdict on a headless surface is queued on the durable approval queue and refused
   (fail-closed). But the only rule loaded is the hardcoded **open posture**
   (`open_tool_rule`, permit `tool.*`), and the **site-manifest Rule loader is an unbuilt
   follow-up** (named as such in `authority.py`). So a site cannot say "publishing requires
   approval": novel actions match the open rule and proceed.

The trigger: the registry path now exposes write tools on MCP (notifications.send, press.*,
webapp.gold_sync). The external-egress ones (`press.publish`, `press.mirror_sync`) were
**withheld from MCP as an interim** (drop `mcp` from `surfaces`) precisely because neither
control above exists to gate them properly. That interim hides the surface instead of gating
the effect — the opposite of the platform's stated doctrine (ADR-072 §4.9.4: a capability
declares its surface + its effect, and the effect is gated, not the surface hidden). It is a
stopgap, not the design.

## Decision

Build the two controls as **distinct, composable layers**. One answers *who may call*, the
other *whether this effect is allowed* — they are different questions and must not be
collapsed.

### 1. Identity gate — enforce `allowed_principals` at dispatch (the *who*)

- The MCP transport resolves the **caller principal** (`@name:context`) — the local node
  identity for stdio (ADR-077), the token subject for HTTP/bearer (ADR-038 §9). `dispatch_call`
  receives it and threads it to (a) the identity check and (b) the authority envelope.
- Before dispatch, the caller principal MUST match the tool's `allowed_principals`
  (Matrix-style patterns, ADR-035). No match → refuse (never dispatch).
- **Registry-projected tools stop declaring nothing.** They pick up the general tool default
  `@*:local` (spec §8), via a `SkillSpec.allowed_principals` field honored by the projector;
  platform primitives keep owner-only (`@<owner>:local`); unmapped tools fail closed to owner-only — closing the current gap where they declare none. A capability widens its
  callers only by declaring so, exactly as it widens its surfaces.


**Implemented (2026-09-17).** `mcp/identity_gate.py` holds the matcher
(`principal_admitted`, Matrix-style `@name:context` with whole-segment `*`),
caller resolution (`caller_principal`), and `refusal_reason` (fail-closed:
no patterns → owner-only). `SkillSpec.allowed_principals` carries a capability's
declared patterns; `skill_tool_contribution` maps each projected tool to
`spec.allowed_principals or ("@*:local",)` (never unbound); `MCPSurface`
carries the merged `{tool: patterns}` map (platform default `@*:local`, registry
from the contribution, unmapped → owner-only at check). Enforcement lives in the
shared `server.py::_raw_dispatch` chokepoint — so **both** dispatch paths
(EC-capable and non-EC) refuse a non-admitted caller before the handler runs.

*Deviation from the sketch above:* the caller is resolved at `_raw_dispatch`
from the transport-stamped `AXIOM_MCP_CLIENT_PRINCIPAL` env var (the same
mechanism the EC client-seam already uses for `AXIOM_MCP_CLIENT_EC_CAPABLE` /
`AXIOM_MCP_CLIENT`), not by adding a principal parameter to `dispatch_call`'s
signature. stdio falls back to the local owner; an authenticated HTTP/bearer
transport (ADR-038 §9) stamps the token subject. Part (b) — threading the
principal into the authority *envelope* — remains with §3 (surface-in-envelope).

### 2. Effect gate — the site-manifest Rule loader (the *whether*)

- Build the loader `authority.py` names as a follow-up: load site `Rule`s (deny / propose /
  permit, priority > the open rule) into the tool `DecideContext`. A site can then require
  approval for a specific `tool://<name>` (e.g. `press.publish`). The decision path is already
  enforced; the loader supplies the rules.
- **Fail-open until a policy is loaded, fail-closed after.** Today the hook is fail-open
  ("P5 step 1"). Once a site policy is present, an undecidable/erroring decision must deny or
  hold, not pass through — the posture flip `authority.py` already anticipates.


**Implemented (2026-09-17).** `axiom/infra/authority_rules.py` loads node-durable
TOML (`~/.axi/authority/site_rules.toml`, override `AXIOM_AUTHORITY_SITE_RULES`)
into authz `Rule`s: each `[[rule]]` maps a `tool` (or `"*"`) to a
`disposition` (permit/deny/propose/require_capability), optional `priority`,
`actor`, `name`. `authority.default_tool_context()` applies them on top of
`open_tool_rule` — deny/propose outrank the open permit by disposition, so a
site holds or refuses a specific `tool://<name>` without a default-deny (an
un-listed tool still permits). The fail-open→fail-closed flip is wired: a
present policy file makes the hook's error path **deny** instead of passing
through (`_site_policy_active()`), and a *malformed* policy still counts as
present (loud ERROR, no partial rules applied) so a typo can't silently reopen
the surface. A site rule currently gates all surfaces for a tool (CLI + agent +
MCP) — per-surface scoping is §3. TDD: 12 loader tests + 6 integration tests.

### 3. Surface-scoped rules (so egress writes can return to MCP behind approval)

- Thread the **surface** (`cli | mcp | agent_tool`) into the `ActionEnvelope` (it carries none
  today), and let a `Rule` optionally match on it. This makes "require approval for
  `press.publish` **on mcp**" expressible without gating the CLI/agent path.
- Once (2) + (3) exist, **re-add `mcp` to `press.publish` / `press.mirror_sync`** behind a
  `propose`-on-mcp rule — replacing the interim withholding with the doctrine-correct effect
  gate.


**Implemented (2026-09-17).** `ActionEnvelope` carries a `surface` field
(`cli|mcp|agent_tool`, empty = surface-less); `Rule` gains an optional
`surface_pattern` (None = any surface; a surface-scoped rule never matches a
surface-less envelope — fail-closed on scope). `dispatch_tool` stamps the
surface into the `tool.pre_invoke` payload, `invoke_capability` passes the
surface it already knows, and `build_tool_envelope`/the hook thread it through.
The site-rule loader accepts an optional `surface` key. `press.publish` /
`press.mirror_sync` re-declare `mcp` and are held by a **built-in
`propose`-on-mcp floor** (`authority.egress_write_mcp_rules`) — so on MCP a
headless call is queued for approval (durable queue, refused headless) while the
CLI/agent path is untouched, replacing the interim withholding. The floor names
the two egress writes in `authority.py` because there is no
extension→authority rule-contribution seam yet; a per-capability declaration (a
`SkillSpec` field mirroring §1's `allowed_principals`) is the documented
follow-up. TDD: 5 loader/matcher tests + 6 egress-floor tests + the MCP
surface guard flipped to assert the return-behind-the-gate.

### Layering & non-goals

- The identity gate runs first (cheap refuse); the effect gate (GUARD + rules) runs in the
  existing `tool.pre_invoke` chain. Both must pass.
- The **client-seam EC gate** (`spec-ec-client-capability.md`, `gate_result_for_client`) is a
  third, orthogonal control on *returned content* and is unchanged.
- Not in scope: a full policy engine (π_global/π_u/π_a/π_t) — that supersedes the T0-1
  `AccessContext` later; this ADR builds the two concrete gates the surface needs now.

## Consequences

- **Positive.** Owner-only becomes real for every MCP tool (not just declared); a site can
  require approval per tool; the egress writes return to MCP the *right* way (behind approval)
  instead of being hidden; defense in depth (identity gate ∧ effect gate ∧ EC client-seam).
- **Costs.** *(identity gate, done)* a transport-stamped caller principal + a
  `SkillSpec.allowed_principals` field the projector honors; *(effect gate, done)* the site-manifest Rule loader + its TOML rule format; a `surface` field on `ActionEnvelope` + rule-matcher support. More moving
  parts on the hot path (an identity compare + the existing GUARD consult).
- **Risks.** The fail-open→fail-closed flip must not break legitimate calls (stage it behind
  policy presence). Two enforcement points must compose without double-refusing or gaps. Every
  transport must reliably supply a caller identity, or the identity gate fails closed (refuse
  when identity is absent on a non-owner surface).
- **Migration (done).** §3 shipped, so `press.publish`/`mirror_sync` re-declare `mcp` (reversing
  the interim withholding `61ef1a13`) and are held by the built-in `propose`-on-mcp floor —
  discoverable + callable-with-approval on MCP, unchanged on the CLI. A site tightens with a
  `deny`; fully ungating egress-writes-over-MCP means removing `mcp` from the capability, a
  deliberate edit.
