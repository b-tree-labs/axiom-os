# ADR-283: Agent types extend one parent and carry a signed lineage

**Status:** Proposed (2026-11-24). A stub: it becomes Accepted only when its chunk lands with the proof passing.
**Related:** ADR-208 (left Proposed), AEOS 1.0 sections 15.7 and 15.8 (drafted on #2311); chunk [C18](https://github.com/b-tree-labs/axiom-os-private/issues/2332) of the AEOS 1.0 plan (`docs/aeos/1.0/plan.md`, private)

## Context

There is no way to declare that one agent type extends another. Python subclassing and additive skill fragments exist, but nothing anyone can see or verify, and no record follows an agent across nodes.

## Decision (adopted Nov 24 2026, to be proven)

- **Agent inheritance.** An agent type extends one parent; skills may be added, overridden or removed (removal refused when the parent marks a skill required); persona overlay; contracts only narrow without a recorded human approval; signed lineage travels with the agent card. Who may extend: an Editor on the parent, or anyone when the parent is marked extensible. Local first; across nodes only under an accord. A clone may carry memory about other people only where the labels allow it, plus consent where a regime requires it. Cloning a running agent is reserved; memory scrubbing for clones is a reserved design area. An instance's age is the state it has accumulated, not elapsed time, and is exported as a signed age manifest (proposed AEOS 15.8).

## Consequences

- C18 (Agent inheritance, version one) delivers it. Scope: extends and lineage fields in the manifest and roster; persona overlay with digests; narrowing lint over AgentContract, surfaces, principals and risk; skill removal refused for parent-required skills; extending requires Editor on the parent or a parent marked extensible; lineage signed and carried in the agent card and roster resource; local only. The names agents.clone, agent.clone and cloned_from are reserved by a registry lint, not by a stub skill. Add agents.age_manifest: a real export of an instance's age manifest (ten components, each with its portability class, owner and measures; hashed and signed by the node key; recorded by CLERK) at a defined snapshot point.
- Retired in the same chunk: Nothing old; composable_skills' additive-only rule is replaced by the add, override and remove rules.

## Proof

- C18: pytest tests/conformance/test_lineage.py: a child of RIVET that adds, overrides and removes one skill loads with two signed lineage entries; a widening child is refused without an attest approval; removing a parent-required skill is refused; a peer verifies the lineage on the card; registering any reserved name fails the lint; and tests/conformance/test_age_manifest.py: agents.age_manifest for a live instance lists all ten components with portability classes, contains no memory content or secret, verifies against the node key, gives the same digest for the same snapshot point, and changes after one more invocation but not after idle time.
