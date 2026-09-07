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
2. **Peer fail-close** (existing, `mcp.routing.route_tool_call`) — EC content is
   refused before dispatch to a non-EC-eligible federation peer.
3. **Client-sink gate** (this spec, `mcp.routing.gate_result_for_client`) — for a
   non-EC-capable host client, the tool runs locally but the **result** is
   classified and **withheld** if export-controlled. A refusal breadcrumb
   (`routing.refused = true`) replaces the result.

The installer stamps the client's identity and capability into the per-client
MCP server env (`AXIOM_MCP_CLIENT`, `AXIOM_MCP_CLIENT_EC_CAPABLE`). The server
reads it; the gate is bypassed (zero overhead) for EC-capable clients and
active for everyone else.

### Fail-closed defaults

- `AXIOM_MCP_CLIENT_EC_CAPABLE` is treated as `true` **only** on an explicit
  `"true"`. Unset / any other value → non-EC-capable → gate active.
- If the classifier itself errors for a non-EC-capable client, the result is
  **withheld** (cannot prove it is non-EC). The keyword classifier is offline
  and deterministic, so explicit controlled terms are caught even when the
  semantic classifier is unavailable.

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
