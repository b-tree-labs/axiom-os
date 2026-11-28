# ADR-280: Every node has a root core set

**Status:** Proposed (2026-11-24). A stub: it becomes Accepted only when its chunk lands with the proof passing.
**Related:** ADR-230 (KEEL is the node root; AXI the principal root); chunk [C11](https://github.com/b-tree-labs/axiom-os-private/issues/2325) of the AEOS 1.0 plan (`docs/aeos/1.0/plan.md`, private)

## Context

Basic capabilities (credentials, memory, identity, discovery, delegation, settings, help) are spread across extensions and some need a language model, so a node without a model or chat UI cannot do the basics.

## Decision (adopted Nov 24 2026, to be proven)

- **Root core set.** Every node exposes a platform-owned core skill set (credentials, memory, identity, discovery, delegation, settings, help) that needs no model. AXI is the principal-facing root agent and answers for it; KEEL is the node root. identity, principal and cred skills move to AXI; HERALD keeps reach preferences.

## Consequences

- C11 (Root core set) delivers it. Scope: Declare a core profile in the registry (credentials, memory, identity, discovery, delegation, settings, help), always on, no model required; move discovery out of the agents extension's 'advanced' tier; give principal, identity and cred their skills under AXI; AXI's roster namespaces include the core namespaces.
- Retired in the same chunk: principal, identity and cred namespaces from HERALD and KEEL; the 'advanced' tier on discovery.

## Proof

- C11: pytest tests/conformance/test_root_core_no_model.py: on a node with no model and no chat UI, MCP lists the core skills and vault.list, memory recall, whoami, agents.roster, settings.get and next.suggest succeed; agents.ask refuses with the 'no language model' reason.
