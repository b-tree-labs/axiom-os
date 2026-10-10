# ADR-152: The semantic classifier advises; labels, the control list and signed agreements decide

**Status:** Proposed
**Date:** 2026-10-02
**Related:** [spec-classification-boundary](../specs/spec-classification-boundary.md) (§4 invariant 1: no model output grants or denies access), [prd-access-agreements](../prds/prd-access-agreements.md), [ADR-114](adr-114-mcp-authority-enforcement.md) (authority on MCP), [ADR-142](adr-142-attestation-is-the-record-of-accountable-human-acts.md) (attestation), ADR-151 (roles; in review)

## Context

The MCP client-sink gate (`gate_result_for_client`) decides whether a tool result may be handed
to a client whose model runs outside the enclave. Until now it classified the result's text with
a small local model and withheld on that model's verdict. The spec this platform is built on says
the opposite: classification is deterministic, and no model output ever grants or denies access.

Measured against the live node and a local 3.8B model:

- **22 of 32 public questions** (public history, textbook science, a published report) were
  classified export controlled by the default prompt, which asked whether text "discusses
  sensitive or restricted content" and never defined export control. None of 12 requests for
  controlled technical data were missed, so the model was agreeing with everything serious-sounding.
- Through the gate, as a non-EC-capable client: a numeric series was withheld while a
  neighbouring series over the same seconds was released; a capabilities listing containing no data
  was withheld as export controlled (there was nothing to classify).
- Tool results already carry the evidence that should decide: the retriever filters by each
  chunk's stored `access_tier` and `classification` at the store, per document, before the gate
  sees the result.

## Decision

1. **Evidence decides, in this order.** (a) A stored non-public label withholds, and content
   labelled public is checked against the control list only: the store already adjudicated it.
   (b) The editable control list (keyword and session) is definitive. (c) The model's verdict is
   **advisory**: recorded through the routing audit for whoever maintains the list, never the
   reason a result is withheld. `routing.sink_semantic` (or `AXIOM_ROUTING_SINK_SEMANTIC`) set to
   `enforce` restores the old behaviour for a deployment that wants it.
2. **Only the model's guess is advisory.** The classifier name `ollama` is the one source treated
   that way. A verdict from any other source, including one the gate does not recognise and the
   operator's strict-mode fallback, stays definitive, and any classifier error still fails closed.
   An allowlist of "definitive" sources was tried first and failed open on an unrecognised source;
   a delegation-security test caught it.
3. **The classifier prompt defines the term.** The default now says what export-controlled
   technical data is and what is not (public history, textbook science, published reports, names,
   measurements, a sensitive-sounding topic alone). Same model: 1 of 32 public questions flagged,
   0 of 12 controlled requests missed. Longer variants were measurably worse (14, 11 and 17 of 32),
   because a 3.8B model degrades as the prompt grows; a test pins a length ceiling and a live check
   re-measures when a model is available.
4. **The semantic classifier keeps its other job.** Chat routing still uses it to choose a local
   model for a query; a wrong guess there costs capability, not access, so it stays.

## Options considered

- **Leave the model deciding and improve only the prompt.** Better (69% to 3% false positives)
  but still a guess deciding access. Kept as the second layer, not the principle.
- **Switch the model off at the sink (`routing.sensitivity = permissive`).** Works as a node
  setting but also removes advisory visibility and is easy to forget; the gate should behave
  correctly by default.
- **Make an agreement a role.** Rejected in the PRD: it collapses what you may do into what you
  have agreed to and cannot express expiry.

## Consequences

- Public and unlabelled content is no longer withheld on a model's say-so. **A real risk to name:**
  unlabelled content that is controlled but matches no list entry and was not labelled at ingest is
  now released to a non-EC-capable client, where the model might have caught it. The model's
  advisory verdict is logged so this is reviewable, ingest-time screening is the primary control, and
  an operator who wants the old stance sets `enforce`. Labelling content at ingest and a maintained
  control list are therefore load-bearing; the PRD's phase P1 gives administrators the means.
- The three refusals (role, agreement, destination) are not yet distinct; that is PRD phase P4.
- Per-person export-control authorization is still not modelled. Today the exception is a
  per-client flag. That is the PRD, not this ADR.
