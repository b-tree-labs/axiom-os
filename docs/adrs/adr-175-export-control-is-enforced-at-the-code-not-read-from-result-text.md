# ADR-175: Export control is enforced where the controlled code is held and run, not read from result text

**Status:** Accepted (2026-10-06)
**Supersedes:** the control-list and semantic-model steps of [ADR-152](adr-152-the-semantic-classifier-advises-and-labels-lists-and-agreements-decide.md) as they apply to the MCP client-sink gate. ADR-152's rule that labels decide stands.
**Related:** `axiom.governance.controlled_code` (the doctrine and the registry), [ADR-158](adr-158-export-control-health-privacy-and-personal-data-are-regimes-of-one-mechanism.md) (regimes of one mechanism), `docs/specs/spec-ec-client-capability.md`

## Context

Export control attaches to a small, named set of controlled codes: specific
simulation programs a site holds under an export-control determination. What is
controlled is the code itself, and a named person's execution of it. A code's
name is not controlled. Neither are the inputs, outputs and results of running
it, nor the data a deployment serves. `axiom.governance.controlled_code` already
states this doctrine, and it places enforcement where the code is held and run:
filesystem permissions for a local run, and the job broker's authorized-persons
check for a managed run.

The MCP client-sink gate (`gate_result_for_client`) contradicted that doctrine.
For a client whose model runs outside the deployment, it classified the result
text in three steps: a stored label, then a keyword control list matched against
the text, then a small model's verdict (advisory since ADR-152 unless an operator
set it to enforce). The control list is, in practice, a list of code names. So
any result that mentioned a controlled code by name was withheld and described
to the person as export-controlled. That included an input deck, a run summary,
and a question about the code. This is the case the doctrine says is not
controlled. The model step had already been shown to guess wrong at scale, which
is why ADR-152 made it advisory.

## Decision

1. **The sink gate does not classify result text for export control.** The
   control-list keyword match and the semantic model no longer run at the sink.
   `AXIOM_ROUTING_SINK_SEMANTIC` is retired: if it is set, it has no effect and
   a warning says so.
2. **A stored label is the only thing that withholds a result.** Labels are
   assigned per document at ingest and the store already filters by them. Any
   non-public `access_tier` or `classification` withholds the whole result from
   a client whose model is outside the deployment, as before.
3. **Each withhold names its regime.**
   - A label declaring export-controlled content (`export_controlled`,
     `export-controlled`, `ec`, `controlled`) reports `export_controlled`. This is
     the controlled code's own access boundary: source or documentation of a
     controlled code that was ingested is labelled, and stays inside.
   - An access-control label (`internal`, `restricted`, `proprietary`,
     `confidential`, `sensitive`, `pii`, `safeguards`) reports
     `access_controlled`. It is still withheld, and it is never described as
     export control.
   - Any other non-public label reports `unknown` and is withheld until its
     regime is declared. An undeclared label fails closed.
   - A mixed result takes the strictest regime present.
4. **Execution stays gated where it is today.** Filesystem permissions and the
   job broker decide who may hold and run a controlled code. Nothing here
   changes them.

## Consequences

- People stop seeing "export-controlled content withheld" for results that only
  name a controlled code, or for data. The number of false withholds at the sink
  drops to those caused by a wrong label, and a wrong label can be found and
  corrected in one place.
- Labelling at ingest now carries the whole result-side weight. Content from a
  controlled code that is ingested without a label would be released at the
  sink. The control for that is the ingest path: a controlled code's source and
  documentation must be labelled when they are ingested, or not ingested.
- The peer-routing gate (`route_tool_call`) still classifies request arguments
  before dispatch to a federation peer. That is a separate decision about a
  separate boundary and is not changed here.
- The `router` parameter of `gate_result_for_client` is kept for call
  compatibility and is no longer consulted.
- Code imported from the running install is frozen until that install is
  refreshed. A node only stops withholding on names after it is redeployed.
