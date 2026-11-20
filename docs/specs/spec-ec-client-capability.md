# Spec — EC Client Capability (the host-client exfiltration boundary)

Status: Accepted (2026-06-26)
Related: [spec-classification-boundary.md](spec-classification-boundary.md) ·
[spec-model-routing.md](spec-model-routing.md) ·
[spec-builtin-mcp-server.md](spec-builtin-mcp-server.md)

## Context

Axiom classifies content into tiers (`public` | `export_controlled`) and the
gateway fail-closes export-controlled content to in-enclave providers — it is
never sent to a public-cloud model. That protects the **model** path.

There is a second exfiltration path the gateway does not see: the **host
client**. An IDE/agent connects to Axiom over MCP and *also* runs its own
model. Some clients run that model in a **public cloud they control** and
**proxy all inference through their own servers** (e.g. an editor that builds
prompts server-side). For such a client:

- The MCP tools run **locally / in-enclave** — safe in themselves.
- But the tool **result is handed back to the client**, whose cloud model then
  receives it. So an export-controlled tool *output* (a retrieved controlled
  document, a controlled artifact record) **leaves the boundary** even though
  the tool ran locally.

A client whose model is **not** the in-enclave endpoint is therefore an
**exfiltration sink**. Local MCP tools under such a client are a *liability*,
not a convenience, for controlled work.

## The rule

> **Export-controlled content is never returned to a client that is not
> EC-capable.** A client is EC-capable **iff its model is the in-enclave
> endpoint** (i.e. it was routed to the local Axiom ingress). Otherwise the MCP
> server runs the tool locally and **withholds** any result that classifies
> export-controlled.

EC-capability is a property of **where the model runs**, not of the IDE brand.
The same IDE is non-EC-capable on its default cloud model and EC-capable only
after it has been routed (`axi mcp install --route-model`) to the in-enclave
ingress.

## Capability matrix

Encoded as the single source of truth in `ToolSpec.model_routable`
(`axiom.extensions.builtins.mcp.install`):

| Client class | `model_routable` | EC-capable when… |
|---|---|---|
| Direct-connect, endpoint-configurable (writable base-URL/env, calls the endpoint directly) | `true` | routed to the in-enclave ingress (`--route-model`) |
| Cloud-proxied, no installable endpoint config (proxies inference through the vendor's cloud; model config is cloud/GUI-only) | `false` | **never** — model cannot be put in-enclave; MCP-tools-only, and EC tool output is withheld |

`ec_capable = model_routable AND routed`. A non-routable client is never
EC-capable; a routable client is EC-capable only once actually routed.

## Enforcement (defense in depth)

1. **Gateway tier fail-close** (existing) — EC content is only ever sent to an
   in-enclave provider; never to a public cloud. Protects the model path.
2. **Peer gate** (`mcp.routing.route_tool_call`, amended 2026-10-27) — a request
   whose arguments carry a non-public label, or an unlabelled request on a site
   whose default label is non-public, is refused before dispatch to a
   non-EC-eligible federation peer. The label decides; the detector only
   advises (see "The peer gate decides on labels" below).
3. **Client-sink gate** (this spec, `mcp.routing.gate_result_for_client`) — for a
   non-EC-capable host client, the tool runs locally and the **result** is
   **withheld** if it carries a non-public stored label (ADR-175). A refusal
   breadcrumb (`routing.refused = true`) replaces the result.

#### The gate classifies the result, not the client's own arguments (amended 2026-10-06)

The gate classifies only the tool **result** — what flows *out* to the client.
A tool's arguments came *in* from that same client; echoing them back, or acking
them, exfiltrates nothing it does not already hold. Folding the arguments into
the sink classification refused a benign memory append whose only controlled-looking
text was the summary the client itself had just sent. A read is unaffected: its
result is the payload, and that is exactly what is still classified. Inbound
argument screening, where wanted, is the peer gate's job (enforcement #2), not
the sink's.

#### A committed mutation is reported committed, never refused (amended 2026-10-06)

The gate runs the tool locally **before** it classifies — so by the time a
withhold is decided, a *mutating* tool's side effect has already happened. The
result bytes are still withheld from a non-EC client, but the envelope must not
report `refused`: that is false, and it invites a duplicate retry (observed:
a memory append committed to the ledger while the caller was told it was refused,
twice). For a mutation the envelope carries `committed: true` and
`result_withheld: true` with `refused: false`; the server surfaces this as a
success note ("the operation completed; its result was withheld … do not retry"),
not an error. A tool is a mutation when its `read_only_hint` annotation is
explicitly `false`; an unknown tool is treated as a read (safe — its result is
still withheld, it simply does not get the committed envelope it does not need).

#### The peer gate decides on labels; the detector advises (amended 2026-10-27, ADR-201 decisions 1, 4, 16)

`route_tool_call` used to run the small model over the request arguments and
refuse a non-EC-eligible peer on its verdict. It now takes its verdict from
labels only: the request's declared `access_tier` / `classification` values
join to the strictest, and an unlabelled request carries the site's
`default_label` (`public` unless the site declares otherwise). A non-public
request is refused for a peer that is not EC-eligible, with `classifier: label`
and the labels in the block; a public request, and any request that names no
peer, is never refused here, and no model output is an input to the verdict.

The detector is consulted only where its answer could change the route: a
public request that names a non-EC-eligible peer. Its answer is advice. An
export-control answer narrows the route to local dispatch and the block records
`advice.effect: rerouted_local` with what advised; a detector that fails removes
the peer option (`advice.effect: unavailable`) and the request is still served
locally. It is not consulted when no peer is named or the peer is EC-eligible.

#### Every withhold is auditable by the id it returns (amended 2026-10-06; location and reporting amended 2026-10-27)

The gate hands the client a `routing_event_id`. That id must resolve: every
sink-gate withhold and every peer-gate refusal — a label, or the fail-closed
label-scan error — is written to the routing audit log keyed by that id
(`routing_audit.log_routing_decision(routing_event_id=…)`), and `audit explain
<id>` resolves a routing-event id out of that log when it is not an authz
verdict. Before this, the enforced branch logged nothing, the log did not store
the id, and the breadcrumb the client was given resolved nowhere.

The log is one file per install, in the platform state directory
(`~/.axi/logs/routing_audit.jsonl`; `AXI_STATE_DIR` redirects it). It used to
hang off the project root, which a process resolves from its working directory
or `AXIOM_ROOT`: a server spawned by a harness wrote under `/private/tmp`, a
developer with many worktrees had one log per worktree, and `audit explain` run
anywhere else read a different file, so the ids handed out on 2026-10-23 could
not be resolved. The old location is still read.

A record that could not be written is not swallowed: the block carries
`audit_recorded: false` and `audit_error`, the client's message says the decision
is not on the audit record, and the server logs a warning.

#### What the client-sink gate withholds on (amended 2026-10-06, ADR-175; was ADR-152)

**A stored non-public label, and nothing else.** If any item in the result carries a non-public
`access_tier` or `classification`, the whole result is withheld (`classifier: label`). The result
text is not classified: the control-list keyword match and the semantic model no longer run at the
sink, and `AXIOM_ROUTING_SINK_SEMANTIC` has no effect (a warning says so if it is set).

Why: export control attaches to the controlled codes and their execution by a named person, and
is enforced where the code is held and run (`axiom.governance.controlled_code`). A code's name,
and the inputs, outputs and results of running it, are not export controlled. A keyword list of
code names matched against result text withheld exactly that material.

The refusal names the regime of the label that caused it:

| Label value | Reported `tier` | Client is told |
|---|---|---|
| `export_controlled`, `export-controlled`, `ec`, `controlled` | `export_controlled` | export-controlled content withheld |
| `internal`, `restricted`, `proprietary`, `confidential`, `sensitive`, `pii`, `safeguards` | `access_controlled` | a data-access label, not export control |
| any other non-public value | `unknown` | a label whose regime is not declared (fails closed) |

A mixed result takes the strictest regime present. Content from a controlled code that was
ingested is withheld because it was labelled at ingest; that labelling is the result-side control.

The installer stamps the client's identity and capability into the per-client
MCP server env (`AXIOM_MCP_CLIENT`, `AXIOM_MCP_CLIENT_EC_CAPABLE`). The server
reads it; the gate is bypassed (zero overhead) for EC-capable clients and
active for everyone else.

### Fail-closed defaults

- `AXIOM_MCP_CLIENT_EC_CAPABLE` is treated as `true` **only** on an explicit
  `"true"`. Unset / any other value → non-EC-capable → gate active.
- If the result's labels cannot be read for a non-EC-capable client, the result is
  **withheld** (`fail_safe: true`). Unlabelled content is released: labelling at ingest is the
  control that carries the weight (ADR-175).

### The serving process states its revision (added 2026-10-22, ADR-202)

The gate is code in a long-lived process, and the process imports it once. A server
started before an install moved keeps applying the rule it loaded. Observed 2026-10-22, and
again on 2026-10-27: a server process started on 2026-10-05, the day before ADR-175 shipped,
was still serving a session three weeks later and withheld a memory write and a memory read
on a model's verdict, while `main` and the refreshed install did not. The response said
`classifier: ollama` and nothing about the age of the code.

Built 2026-10-27 (`axiom.infra.revision`; the rest of ADR-202 decision 3 is open):

- A revision is the package version, the directory the package was imported from and, when
  that is a git checkout, the commit at HEAD, read from the `.git` files without a subprocess.
  The server records the revision it loaded at start (`mcp/runs.py` stamps `commit` and
  `source` beside `version`).
- Every `routing` block carries `server.revision`; a withhold or refusal also carries
  `server.stale` and `server.current`, and the audit record carries the same fields.
- `axi mcp status` reports a harness whose last start loaded a different version, or the same
  version at a different commit, with both revisions. Nothing restarts it.
- A withhold from a server older than the install says so in the client's message: both
  revisions, that it may be applying a replaced rule, and that the remedy is to restart the
  harness that started it. `axi doctor` does not yet report it.

### Other surfaces

This spec governs the MCP client sink. The same labels, the same strictest-regime rule and the
same fail-closed defaults apply on the other surfaces. Their profiles (terminal chat, web
application, embedded widget) and the order in which constraints, fit and optimization are
evaluated are in `spec-model-routing` §15 (ADR-201).

## Practical guidance

- **EC development requires an EC-capable client** — one whose model is the
  in-enclave endpoint (routed to the local ingress). Cloud-model clients are
  structurally disqualified for controlled work regardless of MCP.
- A cloud-model client (non-`model_routable`) is fine for **non-controlled**
  work; its MCP tools simply withhold controlled output.
- `axi mcp install` reports each client's resulting capability so the operator
  can see, per IDE, whether it is EC-capable or MCP-tools-only.

## Discoverability — the capability chart

The matrix is surfaced two ways, both from the single source of truth
(`install.client_capabilities()`):

- **CLI:** `axi mcp clients` renders a chart of every harness × `EC-routable?`
  (+ protocol, MCP-tools, notes). `--json` emits it structured.
- **MCP:** the `axiom_mcp__client_capabilities` tool returns the same matrix so
  an agent can ask "which harnesses may I route to an EC model?" at runtime.

`ec_routable` mirrors `ToolSpec.model_routable`: a harness is routable to an EC
model only if its model can be put in-enclave. Unverified harnesses are
fail-closed (`model_routable=False`) until their data path is confirmed.
