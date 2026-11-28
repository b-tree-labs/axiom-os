# ADR-278: Credentials have one holder

**Status:** Proposed (2026-11-24). A stub: it becomes Accepted only when its chunk lands with the proof passing.
**Related:** ADR-218 (keep is the vault), ADR-055 (governance fabric), ADR-263 (lesser authority across a chain); chunk [C4](https://github.com/b-tree-labs/axiom-os-private/issues/2318), [C5](https://github.com/b-tree-labs/axiom-os-private/issues/2319) of the AEOS 1.0 plan (`docs/aeos/1.0/plan.md`, private)

## Context

keep holds credentials, but eleven files read them directly, a requester is free text, and an open-principal fallback exists for floored credentials. An agent with wider access could fetch or use a secret for someone who should not have it (a confused deputy).

## Decision (adopted Nov 24 2026, to be proven)

- **Key custody.** keep is the only key holder. A value leaves keep only at its outbound chokepoint or through a break-glass reveal at the CLI with a person present. guard decides each keep request on the originating principal and the full delegation chain. No agent passes a secret value to another. WARDEN has its own signing credential, tighten-only keep powers (revoke, suspend, force rotation) and reads evidence through CLERK.

## Consequences

- C4 (Close the credential side door) delivers it. Scope: get_credential becomes keep-internal. The 11 files that call it (signals 6 sites in 5 files, publishing onedrive, web/search, llm/gateway, connect, extensions/discovery and contracts) move to outbound calls with a capability, or to a keep resolve checked by guard. Remove the environment-variable fallbacks next to them and SCAN's direct env-var secrets. Remove the open-principal fallback in vault/outbound.py for floored credentials. Lint: no get_credential, keyring read or credential env read outside keep.
- Retired in the same chunk: Every direct get_credential call outside keep; the 'or os.environ.get(...)' credential fallbacks; the open_principal fallback for floored credentials.
- C5 (Deputy chain and tighten-only keep powers) delivers it. Scope: delegate() and agents.ask carry a signed delegation chain (originating principal, each hop's agent identity and baton id) instead of a free-text requester; guard decides keep requests on the originating principal and the weakest authority along the chain. Add keep.revoke, keep.suspend and keep.force_rotate as agent and MCP skills granted to WARDEN, each recorded. Handoffs carry references or scoped short-lived capabilities, never values.
- Retired in the same chunk: The free-text requester field (agents/delegation.py, chat/scope.py agent_requester).

## Proof

- C4: pytest tests/vault/test_one_holder.py: the lint passes on the tree and fails on a planted get_credential; a floored credential with no principal is refused; each migrated caller works through a capability in its own test.
- C5: pytest tests/conformance/test_key_custody.py: WARDEN cannot get a secret value through AXI, another agent or a two-hop chain (reference or refusal only, guard names WARDEN as originator); WARDEN revokes a live capability and the next outbound call fails; a forged requester is refused.
