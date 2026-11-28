# ADR-277: An agent exercises judgment and a core service answers the same every time

**Status:** Proposed (2026-11-24). A stub: it becomes Accepted only when its chunk lands with the proof passing.
**Related:** ADR-230 (KEEL), ADR-066, ADR-067, ADR-117 and ADR-161 (roster parts superseded in C2); chunk [C1](https://github.com/b-tree-labs/axiom-os-private/issues/2315) of the AEOS 1.0 plan (`docs/aeos/1.0/plan.md`, private)

## Context

The roster had 18 entries, several of them deterministic engines registered as agents with personas, and two retired by ADR-230 still running. Nothing said what makes something an agent.

## Decision (adopted Nov 24 2026, to be proven)

- **Agent versus service rule and the roster.** An agent owns a domain and makes or escalates judgment there; a core service gives the same answer for the same input. The roster is 11 agents (AXI, SCAN, CURIO, PRESS, KEEL, RIVET, WARDEN, PLINTH, HERALD, CLERK, CHALKE) and 3 core services (guard, pulse, keep).
- **Each core service has one agent that answers for it.** guard is answered for by WARDEN, pulse by CLERK, keep by AXI. KEEL runs node credential upkeep (expiry checks, renewals) as node health, scheduled through pulse. This replaces the first-draft answer that KEEL fronts keep.
- **Naming.** Only the 11 agents carry all-caps names and personas. guard, pulse and keep are lowercase services; tidy folds into KEEL; triage's security scans go to WARDEN and the rest to KEEL; rev-u folds into RIVET; desk folds into HERALD; secur-t is retired. CHALKE is an agent. AEOS text uses role names.

## Consequences

- C1 (One roster: agents, services and who answers for each) delivers it. Scope: Create ADR-277 (agent versus service rule, roster, fronting agent, naming rule). AgentRecord gains kind (agent or service) and fronted_by; guard, pulse and keep declare kind service with WARDEN, CLERK and AXI as fronting agents; accountable is filled for all 11 agents; program leaves the optional extension list. Status pass: ADR-066 and 067 to Accepted (shipped); every other Proposed ADR in the roster and AEOS set gets the chunk that will accept it, or 'not scheduled'.
- Retired in the same chunk: program's optional flag; the kind 'agent' declarations for guard, pulse and keep.

## Proof

- C1: pytest tests/agents/test_roster_contract.py: exactly 11 agents and 3 services; each service has a fronting agent; accountable non-empty for every agent; a default node lists program's skills.
