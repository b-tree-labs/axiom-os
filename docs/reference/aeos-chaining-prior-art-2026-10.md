# AEOS tool-chaining declarations: prior-art and standards survey (2026-10)

**Role:** research reference for a standards decision on the additive AEOS
declarations `risk`, `idempotent`, `requires`, `produces`, `next_steps`,
`recipes`, the result `chain` key, chain-id audit propagation, and the
plan/apply hash-approval pattern. Snapshot dated 2026-10-08. The field moves
weekly: re-verify any SEP status before citing it in a normative document.

**Evidence labels.** Every claim carries one of three tags.

- `verified-in-source`: fetched the primary source (spec page, repository
  issue or pull request via the GitHub API, vendor documentation) during this
  survey.
- `from-search-snippet`: seen only in a search-result summary or a secondary
  write-up; the primary source was not opened.
- `recalled-unverified`: background knowledge not re-checked in this survey.

## 1. Verdict in five lines

1. No existing standard covers the whole set. MCP, the closest ecosystem, has
   no tool-dependency, precondition, next-step or recipe construct in its
   2026-07-28 revision, and its attempts at them (SEP-1385 `requires`,
   SEP-1610 chaining) were closed as dormant for lack of a sponsor.
2. Parts exist in public form. Tool risk and idempotency exist as the four MCP
   annotation hints (advisory only). Digest-bound, single-use approval of a
   plan exists as a pattern on MCP's new Multi Round-Trip Requests (MRTR)
   mechanism. Typed output refs feeding later calls exist in Arazzo and OpenAPI
   Links for HTTP APIs. Reusable agent instructions exist as Agent Skills and
   the MCP Skills extension (Final, 2026-09-13). Approval interrupts exist in
   every major agent framework.
3. Nobody combines them. No public artifact found declares preconditions with a
   mandatory exact remediation, evaluated at call time, and returns unmet
   preconditions as a structured refusal. No public artifact found fills the
   arguments of a follow-up call from produced refs in the result itself.
4. The recipe idea (vetted, validated, ordered chain with approval points on
   destructive steps) has many informal cousins (Goose recipes, Windsurf
   workflows, Cursor commands, Skills) but all are prose or unvalidated
   prompts; none validates the chain against the tool catalog's declared
   risk, produces and requires.
5. Open ground is real but narrow, and MCP's Tool Annotations Interest Group,
   Skills Over MCP Working Group and Agents Working Group are the venues where
   it would be contested. The practical posture is: adopt MCP wire shapes where
   they exist, express the AEOS delta as a namespaced `_meta` and
   `structuredContent` convention that degrades cleanly, and stake the claim on
   the semantics and a conformance suite rather than on new transport.

## 2. Landscape facts that shape the decision

### 2.1 MCP specification, revision 2026-07-28

All items below are `verified-in-source` unless noted.

- Latest released revision is 2026-07-28; a draft tree continues. Source:
  https://modelcontextprotocol.io/specification/2026-07-28/changelog
  (accessed 2026-10-08).
- The revision removed protocol sessions and the initialize handshake
  (SEP-2567, SEP-2575), added `server/discover` (capabilities, identity, an
  optional natural-language `instructions` string), moved Tasks into an
  extension `io.modelcontextprotocol/tasks` (SEP-2663), and introduced Multi
  Round-Trip Requests (SEP-2322) which replace server-initiated elicitation and
  sampling requests. Every result now carries `resultType` (`complete` or
  `input_required`). Sampling, Roots and Logging are deprecated (SEP-2577).
- Cross-call state is by explicit server-minted handles passed as ordinary tool
  arguments. The tools page has a non-normative "Stateful Tools" section that
  says exactly this and gives a `create_basket` then `add_item` example. This
  is the closest the core spec comes to `produces`, and it states that "the
  model is responsible for carrying" the handle forward.
  https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- Tool definition fields: `name`, `title`, `description`, `icons`,
  `inputSchema`, `outputSchema`, `annotations`. `structuredContent` may be any
  JSON value. Tool execution errors use `isError: true` and "SHOULD" be given
  to the model for self-correction. There is no field for preconditions,
  dependencies, produced refs, next steps or risk class beyond annotations.
- Annotations are the four hints `readOnlyHint` (default false),
  `destructiveHint` (default true), `idempotentHint` (default false),
  `openWorldHint` (default true), plus `title`. Clients MUST treat them as
  untrusted unless from a trusted server. Source for defaults and framing:
  https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/
  (2026-03-16, Hungerford, Morrow, Chang). The 2026-07-28 tools page itself
  lists only `annotations` as "optional properties describing tool behavior"
  and repeats the untrusted warning; the per-field table was not on the
  fetched page, so exact field text rests on the blog.
- MRTR `requestState`: an opaque string; servers MUST treat it as
  attacker-controlled, MUST integrity-protect it (HMAC or AEAD) when it affects
  authorization or business logic, SHOULD bind it to principal, short expiry and
  a digest of the originating request, and "MUST enforce" single-use
  server-side where it matters. Retries use a new JSON-RPC id.
  https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr.md
- Prompts are "user-controlled" templates: `prompts/list` (name, title,
  description, arguments) and `prompts/get` (messages). They can return
  `InputRequiredResult` under MRTR. There is no step, dependency or approval
  structure.
  https://modelcontextprotocol.io/specification/2026-07-28/server/prompts.md
- Extension mechanism: `extensions` field in capabilities, SEP-2133. Official
  extensions include Tasks, Apps, Skills and auth extensions.
  https://modelcontextprotocol.io/llms.txt (index)
- Skills extension `io.modelcontextprotocol/skills` (SEP-2640, Final, merged
  2026-09-13): `skills/list` and `skills/get` serve Agent Skills directories
  over Resources, each with a SHA-256 manifest; approval binds to the manifest.
  Its stated purpose includes "reusable workflows that combine several tools".
  It carries instructions, not a machine-checkable chain.
  https://modelcontextprotocol.io/extensions/skills/overview.md and
  https://modelcontextprotocol.io/community/working-groups/skills-over-mcp
- Server Cards (SEP-2127, Final, Extensions Track) deliberately exclude tools,
  resources, prompts and capabilities from the static card because they vary
  by identity and deployment. A follow-on is anticipated.
  https://modelcontextprotocol.io/seps/2127-mcp-server-cards.md
- Roadmap (last updated 2026-08-22) lists four relevant priorities: agentic
  messaging primitives (Tasks), agent identity (DPoP, workload identity
  federation, ID-JAG, token exchange), improved primitives including
  "progressive discovery" of large catalogs and a redesign of the `tools/call`
  result shape, and the extension contract. Nothing on workflows, recipes or
  preconditions.
  https://modelcontextprotocol.io/development/roadmap

### 2.2 MCP proposals in flight or closed (GitHub API, 2026-10-08)

All `verified-in-source` via `gh api repos/modelcontextprotocol/modelcontextprotocol`.

| SEP / issue | What | State |
|---|---|---|
| SEP-1385 (#1385) | `requires` field on tool definitions: capabilities, scopes, roles, resource permissions; goal is early failure detection | Closed 2026-01-26 as `dormant`, no sponsor since 2025-08 (maintainer comment) |
| SEP-1610 (#1610) | Model-declared `ChainContent` block, `$ref` JSON-Pointer argument mapping between steps (`step:0#/Paris`), capability `tools.chain` | Closed 2026-06-26, labelled `dormant` |
| SEP-1862 (PR #1862) | `tools/resolve`: argument-specific tool metadata (refined annotations) before a call; LSP resolve pattern | Closed 2026-09-02, not merged; still listed "Draft" in the Tool Annotations IG charter |
| SEP-1913 (PR #1913) | Trust and sensitivity annotations (`sensitiveHint`, `privateHint`, `attribution` with propagation rules) | Open, updated 2026-10-01 |
| SEP-1984 (PR #1984) | Six more hints incl. `reversibleHint`, `privilegedAccessHint`, `slowExecutionHint` | Closed 2026-09-23 by author to sync with the IG |
| SEP-2793 (PR #2793) | Tool Risk Metadata: `riskLevel` low/medium/high/critical, `category`, `blastRadius`, `reversibility` auto/manual/none, `sideEffects`, `approvalRecommendation` none/single/multi, `minTrustLevel` | Open, stalled, awaiting sponsor, last activity 2026-09-28; reviewers say hints need enforcement or they drift |
| #2745 | Advisory policy hints in `_meta`: `effect` read/write/delete/external, `idempotent`, confirmation preference, sensitivity | Closed 2026-05-27 (process: must be a PR) |
| SEP-1792 / #1938 | `agencyHint` | Closed; maintainers worried annotations will "grow indefinitely like capabilities" |
| SEP-1763 | Interceptors (validators and mutators as an MCP primitive) | Closed issue; now an Interceptors Working Group (charter 2026-04-21), SEP listed Draft |
| #3350 | Tool Outcome Attestation extension (signed delivery outcomes) | Closed 2026-09-22 as an issue; authors asked for a collaborator to open the PR; two champions volunteered 2026-09-25 |
| #3394 | Lost-response retry on a mutating `tools/call` re-executes effects; asks for spec language on ambiguous outcome and dedup | Open 2026-09-27; comments converge on logical-operation idempotency keys |
| #2852 | Scoped single-use execution receipts for high-risk tools (signed, TTL, bound to tool, resource, approval id) | Closed 2026-06-24 as completed, as a non-normative pattern; routed to the Authorization IG fine-grained authorization group |
| #2167 | "Skills" capability to publish tool-usage workflows (prerequisites, sequences) | Closed; became SEP-2076 then SEP-2640 |

Charters, `verified-in-source`:

- Tool Annotations Interest Group (chartered 2026-04-20; facilitators from
  GitHub and OpenAI; members from Cloudflare, Microsoft, Nordstrom): explicitly
  asks whether runtime annotations, response annotations and a "richer,
  extensible vocabulary" belong in the protocol. It lists tool resolution,
  trust and sensitivity, comprehensive annotations and model preferences as the
  active agenda. Its out-of-scope list excludes "changes to tool discovery,
  invocation, or lifecycle that do not relate to annotations".
  https://modelcontextprotocol.io/community/interest-groups/tool-annotations.md
- Primitive Grouping Interest Group (chartered 2026-06-18): context overload
  from flat primitive lists, reference extensions for grouping, tool-search and
  code-mode; "MCP Grouping Convention v0.1" proposed.
  https://modelcontextprotocol.io/community/interest-groups/primitive-grouping.md
- Agents Working Group (chartered 2026-08-04): Tasks stabilization, evaluating
  whether an Agents Extension is needed. Explicitly out of scope: planning and
  orchestration internals.
  https://modelcontextprotocol.io/community/working-groups/agents.md
- Interceptors Working Group (chartered 2026-04-21): validators and mutators
  at lifecycle hooks, including audit-mode semantics.
  https://modelcontextprotocol.io/community/working-groups/interceptors.md

### 2.3 Workflow and API-sequence description standards

- **OpenAPI Arazzo 1.1.0** (published 2026-05-17, Apache-2.0,
  `verified-in-source`): workflows with ordered `steps`, `dependsOn`, step and
  workflow `outputs`, `successCriteria`, `onSuccess` and `onFailure`, runtime
  expressions such as `$steps.stepId.outputs.field` and `$inputs.name`. This is
  the strongest existing match for `produces` plus `next_steps` argument
  feeding, but it targets HTTP operations in a separate document, not
  runtime-returned follow-ups, and has no risk, approval or precondition-fix
  construct. https://spec.openapis.org/arazzo/latest.html
- **OpenAPI Links** (OAS 3.2.1, 2026-09-10, `verified-in-source`): Link Object
  with `operationId`, `parameters` and `requestBody` as runtime expressions
  drawn from the response. Closest standard to "next step with pre-filled
  arguments from produced refs", but static per response code, not conditional
  on result values beyond what expressions select.
  https://spec.openapis.org/oas/latest.html
- Arazzo and agents: several practitioner articles (Nordic APIs, SmartBear,
  Tyk, Bump.sh) argue Arazzo should drive agent workflows and MCP generation
  (`from-search-snippet`). No MCP/Arazzo binding standard was found.
- **HATEOAS / hypermedia for agents:** practitioner writing (Nordic APIs,
  2025-09-25, quoting Darrel Miller) argues response-embedded links constrain
  tool selection; a Flywire "Agents API" ships `_links` navigation
  (`from-search-snippet` for Flywire; `verified-in-source` for the Nordic APIs
  article). Nothing standardizes this for tool results.
- **schema.org Action** (V30.1, 2026-09-16, `verified-in-source`): `actionStatus`,
  `agent`, `object`, `result`, `instrument`, `error`, `target`,
  `potentialAction`. No precondition property on Action itself.
- **CNCF Serverless Workflow** (CNCF Sandbox since 2020-07-14,
  `verified-in-source` for status): no MCP or agent construct found on the
  repository page. BPMN, CWL, Argo, Tekton, Temporal, Hydra and JSON-LD
  Hydra: `recalled-unverified`; none targets LLM tool catalogs and none was
  re-fetched.
- **PDDL / STRIPS / HTN:** the precondition and effect model is the formal
  ancestor of `requires` and `produces`. Recent LLM work reuses it for tool
  selection (see 2.5). A PDDL-to-MCP planning bridge exists in toy form
  (`from-search-snippet`).

### 2.4 Agent interoperability standards

- **A2A 1.0.0** (`verified-in-source`, partial read: first 100k of 254k
  characters): Agent Card skills have `id`, `name`, `description`, `tags`,
  `examples`, `inputModes`, `outputModes`; `capabilities.extensions`;
  signed cards; task states include `input-required` and `auth-required`.
  "No explicit workflow chaining or approval mechanisms are defined at the
  protocol level." https://a2a-protocol.org/latest/specification/
- **Agentic AI Foundation (Linux Foundation)** formed 2025-12-09 with MCP,
  goose and AGENTS.md (`verified-in-source`).
  https://www.linuxfoundation.org/press/linux-foundation-announces-the-formation-of-the-agentic-ai-foundation
- **Agent Skills / SKILL.md** (`verified-in-source`): frontmatter `name`,
  `description`, `license`, `compatibility`, `metadata` (string map),
  `allowed-tools` (experimental); progressive disclosure; validator
  `skills-ref`. No risk, requires or chain fields. The `metadata` string map is
  the sanctioned extension point. https://agentskills.io/specification
- **AGNTCY OASF** (`verified-in-source`): record object annotated with skills,
  domains and modules from taxonomies, modeled on OCSF; no preconditions, risk
  or workflow content. https://docs.agntcy.org/oasf/open-agentic-schema-framework/
- **IETF individual drafts** (`from-search-snippet` plus one fetch):
  draft-han-ai-manifest-01 (2026-05-15, expires 2026-11-16, `verified-in-source`)
  embeds ordered UI workflow steps for web agents and is not endorsed by the
  IETF; Agent Identity Protocol (signed capability manifest), Agent Discovery
  Protocol, Agent Trust Negotiation, draft-cui-ai-agent-discovery-invocation.
  None addresses tool preconditions or chaining as far as the snippets show.
- **W3C AI Agent Protocol Community Group** (launched 2025-05; mission includes
  "standardized metadata formats" for agent capabilities; `from-search-snippet`).
  No published specification found.
- **NIST CAISI AI Agent Standards Initiative** (announced 2026-02-17) and NCCoE
  concept paper (2026-02-05) target identity, authentication and authorization
  (`from-search-snippet`, via Cloud Security Alliance notes). No tool-metadata
  work found.
- **OWASP Top 10 for Agentic Applications 2026** (`from-search-snippet`):
  ASI02 Tool Misuse and Exploitation; guidance to require human approval for
  irreversible actions and to use least privilege.
- **OpenID AuthZEN Authorization API 1.0** (Final, published 2026-01-12,
  `from-search-snippet`): a standard subject, action, resource, context
  decision call. Relevant as the policy-decision seam for `risk`, not as a
  metadata format.
- **AARM, Autonomous Action Runtime Management** (Errico, arXiv 2602.09433,
  2026-02-10, adopted into a Cloud Security Alliance initiative,
  `verified-in-source` for the abstract): runtime interception before
  execution, session context, policy, approval and deferral services,
  tamper-evident receipts; classifies actions as forbidden, context-dependent
  deny, context-dependent allow. Protocol-agnostic. This is the nearest
  conformance-oriented effort for the approval and receipt side.
  https://arxiv.org/abs/2602.09433
- Framework approval hooks (`from-search-snippet` unless noted): OpenAI Agents
  SDK `needs_approval` with serializable `RunState`; Microsoft Agent Framework
  `ApprovalRequiredAIFunction`; Google ADK `require_confirmation` (per-call
  predicate); LangChain `humanInTheLoopMiddleware`; LangGraph `interrupt()`
  (`verified-in-source`: requires a checkpointer, side effects before an
  interrupt re-execute on resume so must be idempotent). All are per-tool
  boolean gates implemented in the client runtime, none declares a risk class
  in a portable descriptor.
- Coding-agent permission systems: Claude Code evaluates deny, then ask, then
  allow, first match wins, and PreToolUse hooks cannot override a deny or ask
  (`verified-in-source`,
  https://code.claude.com/docs/en/permissions). A Claude Code feature request
  to honor MCP annotation hints was filed 2026-08-17 and closed
  (`verified-in-source`, issue #87452). A third-party write-up states Codex CLI
  always requires approval when `destructiveHint` is true and offers a hard
  block setting (`from-search-snippet`, the author notes it is not source-code
  verified).

### 2.5 Safety and approval patterns, and the research literature

- **Terraform saved plans:** a plan saved with `-out` records the state lineage
  and serial; apply fails with "Saved plan is stale" if they differ
  (`from-search-snippet`, HashiCorp Discuss threads; the CLI reference pages
  fetched did not state it). This is the human-tooling ancestor of
  plan_hash-then-apply. **Kubernetes** server-side dry-run (`dryRun=All`) and
  `resourceVersion` optimistic concurrency (`verified-in-source`,
  https://kubernetes.io/docs/reference/using-api/api-concepts/).
- **MCP plan-hash approval already described as a shipped pattern:** a vendor
  article (bex.co, 2026-09-23, `verified-in-source` for its content, but it is
  a single-vendor blog, not a spec) describes a deploy gate on MRTR: the server
  builds a plan, returns `input_required` with the digest sealed in
  `requestState`, a human approves a card bound to the digest, the retry echoes
  `requestState`, the server verifies digest, single-use and expiry, executes
  once and returns a receipt. This matches the MRTR spec's own
  `requestState` guidance. https://bex.co/blog/2026/09/23/mcp-multi-round-trips-approval-gates-agent-deploys
  Companion post on the same site (2026-09-24) argues annotations are advisory
  and server-owned parameters must be derived from the session, not client
  arguments (`verified-in-source`).
  `planlock` (MIT, one GitHub star, early stage) is an MCP proxy that binds one
  approval to one hash of an agent-drafted multi-step plan
  (`verified-in-source`). https://github.com/JosephCurwin/planlock
- **Structured error recovery:** Srinivasan, "Bridging Protocol and Production"
  (arXiv 2603.13417, 2026-03, `verified-in-source`) proposes SERF: machine
  readable error categories, retryability flags, retry timing and "suggested
  actions". Blog patterns return `{kind, retryAfterSec, reason, param, fix}`
  (`from-search-snippet`). These are the nearest thing to a "structured
  refusal with exact fix", but they are error-recovery hints after a failed
  call, not declared call-time preconditions.
- **Research on tool dependencies:**
  - ToolChoiceConfusion (arXiv 2606.06284, 2026-06-05, `verified-in-source`
    for abstract-level): each tool declares preconditions as required input
    variables and effects as output variables; causal filtering exposes only
    tools whose preconditions can be met. This is the academic form of
    `requires` plus `produces`.
  - SkillGraph (arXiv 2604.19793, 2026-04-07, `verified-in-source`): semantic
    similarity fixes selection but inverts ordering (Kendall tau down to
    -0.433 on API-Bank); ordering must come from mined trajectories. Evidence
    that tool descriptions alone do not carry sequencing, which supports
    declaring `next_steps`.
  - ToolSandbox (arXiv 2408.04682, NAACL Findings 2025) models stateful tools
    with implicit state dependencies; tau-bench evaluates policy adherence;
    NESTFUL, ToolBench, API-Bank cover nested calls and sequences
    (`from-search-snippet`).
  - No benchmark was found that scores an agent on obeying declared
    preconditions with remediation, or on plan-hash approval.
- **Audit correlation:** OpenTelemetry GenAI semantic conventions cover model,
  agent and tool-execution spans, and MCP documents `traceparent`,
  `tracestate`, `baggage` in `_meta` (SEP-414, `verified-in-source` via the
  changelog). W3C PROV extensions for agents exist (PROV-AGENT,
  `from-search-snippet`).

### 2.6 Tool discovery for large catalogs

- **Anthropic tool search tool** (`verified-in-source`): `defer_loading: true`
  per tool, regex and BM25 variants (`tool_search_tool_*_20251119`), up to
  10,000 deferred tools, results returned as `tool_reference` blocks, prompt
  cache preserved, custom client-side search allowed. Docs state selection
  accuracy degrades past 30 to 50 tools. For MCP, `defer_loading` is set on the
  `mcp_toolset` `default_config`.
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool
- **MCP:** `server/discover` carries one free-text `instructions` string;
  roadmap "progressive discovery" and the Primitive Grouping IG (grouping,
  tool-search, code-mode) are the open work. SEP-2127 explicitly defers
  advertising primitives in static documents.
- **Research:** graph-prior retrieval (SkillGraph) and precondition filtering
  (ToolChoiceConfusion), above.
- No public convention was found for a byte-budgeted, machine-built capability
  block in `instructions`. Practitioner posts measure catalog token overhead
  (`from-search-snippet`) but do not standardize a budget.

### 2.7 Recipes, playbooks, runbooks as first-class artifacts

- **Goose recipes** (AAIF project, `verified-in-source`): YAML with
  `parameters` (typed, required or defaulted), `extensions`, `sub_recipes`
  (isolated sessions, sequential), `retry` with shell success checks, `response`
  JSON schema. The reference states there are no approval or ordering fields
  beyond sequential sub-recipes. https://goose-docs.ai/docs/guides/recipes/recipe-reference
- **Windsurf Workflows** (markdown steps in `.windsurf/workflows/`, invoked as
  slash commands), **Cursor commands** (markdown in `.cursor/commands/`),
  **OpenHands microagents** (markdown with trigger keywords): prose
  instructions, no validation (`from-search-snippet`).
- **Agent Skills and MCP Skills extension:** prose `SKILL.md` plus scripts.
  The MCP extension adds integrity (digest manifest) and approval binding for
  the skill content, not for the chain it describes.
- **Runbook automation vendors:** ServiceNow "playbook as MCP tool" and
  Grafana playbooks with per-call approval of MCP actions (`from-search-snippet`).
  These expose a whole playbook as one tool or gate each call, rather than
  publishing a validated step graph as an MCP prompt.
- **n8n and Zapier MCP:** `recalled-unverified`; not fetched.

## 3. Concept-by-concept coverage

Rating key: covered (an existing public mechanism does the same job),
partially (a mechanism does part of it or only for a different substrate),
not covered (nothing found).

| AEOS declaration | Closest existing mechanism | Coverage | Source and status | How to align |
|---|---|---|---|---|
| `risk`: read, reversible-write, destructive | MCP `readOnlyHint` and `destructiveHint`; SEP-2793 `category`/`reversibility`/`riskLevel`; SEP-1984 `reversibleHint` | Partially. Hints are binary, advisory, and cannot express "write that can be undone". Graded proposals are stalled or closed | 2.1 blog (verified); SEP-2793 open, stalled; SEP-1984 closed | Map: emit `readOnlyHint`, `destructiveHint` derived from `risk` on the wire (read: `readOnlyHint:true`; reversible-write: `readOnlyHint:false`, `destructiveHint:false`; destructive: `destructiveHint:true`). Keep the three-valued `risk` as the AEOS source of truth. Offer the three-value enum to the Tool Annotations IG as a minimal graded alternative to SEP-2793's seven fields |
| `idempotent` | MCP `idempotentHint` (non-normative); #3394 lack of retry semantics | Partially: hint exists, semantics absent | Spec blog (verified); #3394 open | Adopt `idempotentHint` as is. Add the AEOS delta: define what idempotent means for retry (same logical-operation key yields at most one effect) and test it |
| `requires` with mandatory exact `fix`, evaluated at call time, unmet gives structured refusal | SEP-1385 `requires` (declarative, capabilities and permissions only, no fix, no result shape); SEP-1862 `tools/resolve` (preflight); SERF and blog "fix" error fields; ToolChoiceConfusion preconditions | Partially. Declaration idea and error-hint idea exist separately; the combination (declared, evaluated per call, each carrying an exact remediation, refusal shape standardized) does not | SEP-1385 closed dormant; SEP-1862 closed; arXiv 2603.13417, 2606.06284 | Stake new ground on the semantics. Wire shape: tool-execution error (`isError:true`) with `structuredContent.chain.unmet[]`, each item `{name, fix}` where `fix` is an exact call or action. Reuse SEP-2140/1303 principle that such failures are tool execution errors, not protocol errors |
| `produces`: typed output refs | MCP `outputSchema` plus `structuredContent`; MCP "Stateful Tools" handle guidance; Arazzo `outputs`; OpenAPI Links runtime expressions | Partially: typed outputs exist; marking which fields are refs that later calls accept, with a type name, does not | Tools page (verified); Arazzo 1.1.0 (verified) | Map onto `outputSchema` (declare produced refs as ordinary output properties) and adopt Arazzo-style `$steps.<id>.outputs.<name>` naming in recipe wiring. Add a type tag per ref as an AEOS extension keyword in the schema (`x-aeos-ref: "plan_hash"`), JSON Schema 2020-12 permits unknown keywords |
| `next_steps`: declared follow-ups, condition, pre-filled arguments from produced refs | OpenAPI Links; Arazzo `onSuccess`/`onFailure` and step wiring; HATEOAS `_links`; SEP-1610 `$ref` JSON-Pointer mapping | Partially: each exists for HTTP, or as an abandoned MCP proposal. Not found: runtime result-embedded follow-ups with conditions and pre-filled arguments for tool calls | OAS 3.2.1, Arazzo 1.1.0 (verified); SEP-1610 closed dormant | Map onto OpenAPI Links field names: `operationId` becomes tool name, `parameters` as runtime expressions over the result (`$response.body#/plan_hash` becomes a JSON Pointer into `structuredContent`). Reuse RFC 6901 as SEP-1610 did. Keep `condition` as the AEOS addition |
| `recipes`: vetted, validated, ordered chains, approval points on destructive steps, published as MCP prompts | Goose recipes; Windsurf workflows; Cursor commands; Agent Skills; MCP Skills extension; Arazzo workflows; MCP prompts | Partially. Packaging and distribution are covered (Skills, prompts). Static validation against declared risk, produces and requires, and mandatory approval points, are not found anywhere | Goose reference (verified: no approval fields); Skills overview (verified) | Publish each recipe two ways: as an MCP prompt (works today in every client) and as a Skill (`skill://` URI, digest manifest) for hosts that support the extension. Express the step graph in an Arazzo-compatible subset so existing tooling can parse it. AEOS delta: the validator and the approval-point rule |
| Result `chain` key: unmet, next, produced, chain_id, step_id | Nothing standard. Nearest: `_meta` conventions, `structuredContent`, Tasks handles, SEP-1913 propagation annotations | Not covered | Tools page (verified) | Stake new ground. Carry in `structuredContent.chain` (data the model must see) and mirror `chain_id` in `_meta` under a reverse-DNS key for gateways, following SEP-414 style keys. Do not use un-namespaced `_meta` keys (SEP-1788 reserves them) |
| Chain id propagated through an audit trail | W3C Trace Context in `_meta` (`traceparent`); OTel GenAI conventions; AARM receipts; SEP-1763 audit mode; #2852 receipts | Partially: trace correlation is covered, a business-level chain id with step ids and approval linkage is not | MCP changelog (verified); AARM abstract (verified) | Adopt `traceparent` for transport tracing and keep `chain_id` as a separate, durable, human-meaningful key (a trace id is not retained or semantically stable). Record `chain_id`, `step_id` and approval id in the audit event; offer an OTel attribute naming proposal |
| Plan/apply with content hash, human approval, apply presents same hash, single-use approval | Terraform saved plan with lineage and serial; Kubernetes dry-run; MCP MRTR `requestState` digest binding (vendor-described pattern); planlock; scoped receipts (#2852); AARM approval service | Partially to mostly covered as a pattern; not covered as a declared, discoverable tool-pair contract | MRTR spec (verified); bex.co (verified, single vendor); planlock (verified) | Adopt MRTR as the transport for approval where the client supports it, with the `requestState` rules of the MRTR spec (AEAD, principal, TTL, request digest, server-enforced single use). Keep the explicit `plan_hash` argument for clients without MRTR. AEOS delta: declaring `plan` and `apply` as a linked pair so clients and validators know the pattern exists |
| Byte-budgeted capability block | `server/discover` `instructions` string; Anthropic tool search; Primitive Grouping IG; Server Cards (exclude primitives) | Partially: a carrier exists (`instructions`), a budget convention does not | Discover page (verified); tool search doc (verified) | Use `instructions` as carrier. AEOS delta: a stated byte or token budget, deterministic ordering (the spec already says SHOULD return tools in deterministic order for cache hits), and a rule for what must be in it |

## 4. Alignment recommendations

Adopt or map exactly (names and shapes):

1. **Risk and idempotency on the wire.** Always emit MCP `annotations`:
   `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`, `title`.
   Derive them from `risk` and `idempotent`. Keep AEOS `risk` as the richer
   source. Do not rely on clients honoring them: the spec says untrusted, and
   a Claude Code request to honor them was closed unmerged.
2. **Typed outputs.** Declare `outputSchema` and return `structuredContent`
   plus the serialized JSON in a text block (the spec says SHOULD for
   compatibility). Declare refs as properties of the output schema.
3. **Follow-up wiring.** Use RFC 6901 JSON Pointer (as SEP-1610 did) or
   OpenAPI runtime-expression syntax for `next_steps` argument feeding, so a
   reader of Arazzo or OpenAPI Links recognizes it. Prefer OpenAPI Link Object
   field names: `operationId` (tool name), `parameters`, `description`.
4. **Recipes.** Ship every recipe as an MCP prompt now. Add the Skills
   extension binding (`skills/list`, `skills/get`, `skill://<name>/SKILL.md`,
   SHA-256 manifest) when AEOS hosts support it. Constrain the recipe step
   graph to an Arazzo 1.1.0 subset (`workflowId`, `steps`, `stepId`,
   `outputs`, `dependsOn`, `successCriteria`) and place AEOS fields under
   `x-` extensions, which the Arazzo specification allows.
5. **Approval transport.** Where the client advertises elicitation under
   2026-07-28, perform the approval with an `InputRequiredResult` and put the
   plan hash inside integrity-protected `requestState` following MRTR rules 4
   and 5. Keep the plain two-call plan/apply (hash as argument) as the
   fallback, and keep server-side single-use enforcement because MRTR
   explicitly leaves single-use to the server.
6. **Error placement.** Return unmet preconditions as tool execution errors
   (`isError:true`), consistent with SEP-1303 and the tools page, not as
   JSON-RPC protocol errors.
7. **Namespacing.** Use reverse-DNS keys in `_meta` and a single reserved
   top-level key inside `structuredContent` (`chain`). Do not add new
   top-level fields to `Tool`; the maintainers worry about annotation sprawl
   (SEP-1792 discussion), and un-namespaced `_meta` keys are reserved
   (SEP-1788, `from-search-snippet`).
8. **Tracing.** Carry W3C `traceparent` in `_meta` for transport traces; keep
   `chain_id` separate.
9. **Registries and cards.** Do not put AEOS declarations in a static Server
   Card; SEP-2127 explicitly excludes primitives because they vary at
   runtime. Serve them from `tools/list` and `prompts/list`.

Keep as the AEOS delta (nothing public to adopt):

- The three-valued `risk` with the exact reversible-write class.
- `requires` with mandatory `fix`, call-time evaluation, and the unmet-refusal
  shape.
- The `chain` result envelope and `chain_id` through audit.
- Recipe validation rules (every step's risk, requires and produces resolved
  against the live catalog; an approval point is mandatory on every
  destructive step).
- The plan/apply pair declaration and its conformance behavior.
- The capability-block budget rule.

## 5. Open ground we could stake

Confidence is that the idea is novel as a published, specified artifact as of
2026-10-08, given what was searched. "Not found" is not proof of absence; MCP
SEP and issue search covered titles and bodies by keyword only, and private or
unindexed work is invisible.

### 5.1 Call-time preconditions with mandatory exact remediation (`requires` + `fix`)

- Novelty confidence: medium-high.
- Checked: MCP issue and PR search for "precondition", "prerequisite",
  "requires", "next steps" (found SEP-1385, closed dormant, no remediation
  field); the Tool Annotations IG agenda (tool resolution, trust, annotations;
  no preconditions); A2A (no mechanism); Arazzo (success criteria, not
  remediation); arXiv for "tool precondition" (found ToolChoiceConfusion,
  which uses preconditions for filtering, not for refusal with a fix); blog
  patterns for structured errors with a `fix` field (informal, post hoc).
- Why it is distinct: the contract is that every unmet condition is
  machine-detectable before the effect, and the refusal always carries an
  exact next action, so a refusal is never a dead end.
- To make it credible: a one-page spec for the declaration and refusal schema;
  a JSON Schema; a reference implementation; a conformance suite asserting
  (a) no effect occurs on an unmet requirement, (b) every unmet item has a
  `fix`, (c) following the `fix` literally clears the requirement; an
  evaluation showing fewer wasted calls on an existing benchmark such as
  ToolSandbox or tau-bench. Publish first as an MCP Extensions Track proposal
  (SEP-2133) and bring it to the Tool Annotations IG, which already lists
  runtime and response annotations as open questions. Cite SEP-1385's closure
  as the gap and make the proposal sponsor-ready (maintainers close
  sponsor-less SEPs as dormant).

### 5.2 Result-embedded `next_steps` with conditions and pre-filled arguments

- Novelty confidence: medium.
- Checked: MCP issues for "next steps", "tool chain" (SEP-1610 is model-side
  chaining, the opposite direction, and dormant); OpenAPI Links and Arazzo
  (static, HTTP, document-level); HATEOAS-for-agents articles (concept, no
  standard, no pre-filled tool arguments contract).
- Why it is distinct: the server, not the model or a separate document, tells
  the agent which tool comes next, under which condition, with arguments
  already bound from this result's produced refs. This is hypermedia for tool
  calls, and it directly addresses the ordering-signal gap measured in
  SkillGraph.
- To make it credible: formal mapping to OpenAPI Link Object so the semantics
  are borrowed rather than invented; a validator that statically checks that
  every `next_steps` argument source exists in `produces`; a measurement of
  ordering accuracy with and without declared `next_steps` (Kendall tau on a
  public benchmark such as API-Bank, the one SkillGraph reports).

### 5.3 Validated recipes with mandatory approval points

- Novelty confidence: medium. The packaging is common; the validation
  contract is the claim.
- Checked: Goose recipe reference (no approval fields, sequential only),
  Windsurf, Cursor, OpenHands, Agent Skills, MCP Skills extension (content
  integrity only), ServiceNow and Grafana playbooks (per-call gating), Arazzo
  (no risk concept).
- Why it is distinct: a recipe is rejected at publish time if any step is
  unresolved against the catalog, any `requires` is not satisfiable by an
  earlier step's `produces` or a declared external fix, or any destructive step
  lacks an approval point.
- To make it credible: a published recipe schema (Arazzo subset plus `x-`
  fields); a `recipe lint` reference tool; golden valid and invalid fixtures as
  the conformance suite; a Skills-extension binding so it is distributable.
  Venue: Skills Over MCP Working Group (has open cross-cutting scope with
  Primitive Grouping and Agents) or the Agent Skills specification as an
  optional metadata convention.

### 5.4 A declared plan/apply pair with hash-bound single-use approval

- Novelty confidence: low-medium as a pattern, medium as a declared contract.
- Checked: MRTR spec and its replay guidance; bex.co deploy gate; planlock;
  #2852 scoped receipts; Terraform and Kubernetes precedents; AARM; framework
  interrupts. The pattern is real and spreading, so the pattern alone is not
  ownable.
- What may be ownable: declaring in the catalog that `X_plan` and `X_apply`
  form a pair, which result field is the hash, and conformance rules (apply
  with a different hash is refused; approval is consumed on first use even if
  the apply fails midway; expired plan is refused; the plan hash covers
  inputs and the observed state it depends on).
- To make it credible: conformance tests including the replay, expiry,
  tamper and concurrent-apply cases; an explicit alignment note to MRTR
  `requestState` so it is clearly an application profile of MRTR rather than
  a competitor. Venue: Security Interest Group or the Authorization Interest
  Group's fine-grained authorization group, which already received the
  receipts pattern (#2852).

### 5.5 Durable chain identity across audit (`chain_id`, `step_id`, approval linkage)

- Novelty confidence: medium-low.
- Checked: OTel GenAI conventions and W3C Trace Context in MCP `_meta`
  (transport tracing exists), AARM receipts, Tool Outcome Attestation
  (#3350, outcome signing), PROV-AGENT. No convention found for a
  business-level chain id that ties plan, approval and apply and survives
  across sessions.
- To make it credible: a short profile that maps `chain_id` to an OTel
  attribute and a PROV `wasInformedBy` chain, plus an audit-event schema and a
  verifier. Venue: contribute to the AARM receipt format or to an MCP
  Interceptors audit-mode profile rather than a standalone standard.

Not recommended as claims: the byte-budgeted capability block (an
implementation practice that the MCP progressive-discovery work may overtake),
and the generic idea of a risk class (crowded, with three open or closed SEPs
and an interest group).

## 6. Risks and things to track

- **MCP is changing the relevant surfaces now.** Watch: the Tool Annotations
  IG (could standardize a graded risk vocabulary or runtime annotations that
  collide with `risk`); the roadmap's `tools/call` result shape redesign
  (could change where `structuredContent` and the `chain` key live);
  progressive discovery (could define a catalog-narrowing mechanism that
  overlaps the capability block and `next_steps`); Tasks promotion into core
  (affects long-running applies); SEP-1913 (propagation rules for
  sensitivity); SEP-2793 (if sponsored, becomes the graded risk standard);
  Interceptors (could standardize pre-call validators, which is exactly where a
  `requires` evaluator could live).
- **Two-tier risk of a closed SEP trail.** SEP-1385 and SEP-1610 were closed
  for lack of a sponsor. A future AEOS proposal needs a named maintainer
  sponsor and a working-group home, or it will end the same way.
- **Annotation sprawl resistance.** Maintainers voiced concern that annotations
  "grow indefinitely like capabilities". Prefer one extension with a namespace
  over several new annotation keys.
- **Hints are not enforcement.** The spec, the MCP blog and the third-party
  analyses all say so. The AEOS pitch is stronger if `risk` and `requires` are
  enforced server-side and testable, which is also what reviewers of SEP-2793
  asked for.
- **Single-vendor evidence.** The bex.co articles and planlock are single
  sources; the MRTR spec text is the authoritative part. Do not cite the blog
  as consensus.
- **Naming collisions.** "Skills" now means three things: Agent Skills
  (SKILL.md), the MCP Skills extension, and AEOS skills (tools). "Recipes"
  collides with Goose recipes. "Playbook" collides with ServiceNow and Grafana
  usage. Consider documenting a mapping table, or using "tool" in external
  text and reserving AEOS "skill" for internal use. "Idempotent" and "risk"
  collide with MCP hint names only in intent, not syntax.
- **Licensing.** Arazzo 1.1.0 is Apache-2.0 (`verified-in-source`). Agent
  Skills and MCP specification licenses were not checked in this survey
  (`recalled-unverified`: MCP spec content is moving to a permissive license
  under the Linux Foundation; confirm before copying text). Check each license
  before copying schemas into a normative AEOS document; referencing by name
  needs no license.
- **Governance.** MCP, goose and AGENTS.md are under the Agentic AI
  Foundation; Agent Skills and OASF governance not fully traced. A standard
  intended to be neutral should plan for a foundation or working-group home.
- **Draft instability.** IETF individual drafts expire (draft-han-ai-manifest-01
  on 2026-11-16) and carry no standing; do not depend on them.

## 7. Sources

Access date for all entries: 2026-10-08 unless noted. "Not fetched" means the
URL appeared only in search results or was not opened.

MCP specification and governance (fetched):

- https://modelcontextprotocol.io/specification/draft/server/tools
- https://modelcontextprotocol.io/specification/2026-07-28/server/tools
- https://modelcontextprotocol.io/specification/2026-07-28/changelog
- https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr.md
- https://modelcontextprotocol.io/specification/2026-07-28/server/prompts.md
- https://modelcontextprotocol.io/specification/2026-07-28/server/discover.md
- https://modelcontextprotocol.io/development/roadmap
- https://modelcontextprotocol.io/llms.txt
- https://modelcontextprotocol.io/extensions/skills/overview.md
- https://modelcontextprotocol.io/seps/973-expose-additional-metadata-for-implementations-res.md
- https://modelcontextprotocol.io/seps/2127-mcp-server-cards.md
- https://modelcontextprotocol.io/community/interest-groups/tool-annotations.md
- https://modelcontextprotocol.io/community/interest-groups/primitive-grouping.md
- https://modelcontextprotocol.io/community/working-groups/skills-over-mcp
- https://modelcontextprotocol.io/community/working-groups/agents.md
- https://modelcontextprotocol.io/community/working-groups/interceptors.md
- https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/

MCP issues and pull requests (read through the GitHub API):

- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1385
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1610
- https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1862
- https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1913
- https://github.com/modelcontextprotocol/modelcontextprotocol/pull/1984
- https://github.com/modelcontextprotocol/modelcontextprotocol/pull/2793
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1763
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1792
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/2140
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/2167
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/2686
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/2745
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/2852
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3350
- https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394
- https://github.com/anthropics/claude-code/issues/87452

Not fetched, seen in results only: SEP-1560, SEP-1561, SEP-1487, SEP-2417
(model preferences), SEP-1686 (original Tasks), SEP-2663 (Tasks extension),
SEP-1788, SEP-1303, SEP-1938 body, the experimental-ext-tool-annotations and
experimental-ext-grouping repositories, the ext-skills specification file.

Workflow and description standards:

- https://spec.openapis.org/arazzo/latest.html (first 100k of about 110k
  characters read)
- https://spec.openapis.org/oas/latest.html (partial)
- https://schema.org/Action
- https://github.com/serverlessworkflow/specification
- Not fetched: BPMN, CWL, Argo, Tekton, Temporal, Hydra and JSON-LD, AsyncAPI.
- Not fetched, search results only: Nordic APIs, SmartBear, Tyk, Bump.sh
  Arazzo-for-agents articles; Flywire agents API HATEOAS page.
- https://nordicapis.com/hateoas-the-api-design-style-that-was-waiting-for-ai/

Agent interoperability and safety:

- https://a2a-protocol.org/latest/specification/ (first 100k of 254k read)
- https://www.linuxfoundation.org/press/linux-foundation-announces-the-formation-of-the-agentic-ai-foundation
- https://agentskills.io/specification
- https://docs.agntcy.org/oasf/open-agentic-schema-framework/
- https://datatracker.ietf.org/doc/draft-han-ai-manifest/01/
- Not fetched, search results only: IETF drafts for Agent Identity Protocol,
  Agent Discovery Protocol, Agent Trust Negotiation,
  draft-cui-ai-agent-discovery-invocation; W3C AI Agent Protocol Community
  Group pages; NIST CAISI initiative pages and Cloud Security Alliance notes
  (one note URL returned 404 on fetch); OWASP Top 10 for Agentic Applications
  2026; OpenID AuthZEN final specification notice
  (https://openid.net/authorization-api-1-0-final-specification-approved/);
  UTCP, NLWeb, Agent Network Protocol pages.
- https://arxiv.org/abs/2602.09433 (AARM)
- https://code.claude.com/docs/en/permissions
- Could not fetch: Codex security page (308 redirect not followed);
  third-party write-up
  https://codex.danielvaughan.com/2026/04/12/mcp-tool-annotations-risk-vocabulary-codex-cli/
  was read but is not source-code verified.
- https://docs.langchain.com/oss/python/langgraph/interrupts
- Not fetched, search results only: OpenAI Agents SDK, Microsoft Agent
  Framework tool approval, Google ADK confirmation, Semantic Kernel metadata,
  CrewAI, LlamaIndex, Windsurf, Cursor, OpenHands documentation pages.

Approval, plan/apply, errors, discovery:

- https://bex.co/blog/2026/09/23/mcp-multi-round-trips-approval-gates-agent-deploys
- https://bex.co/blog/2026/09/24/mcp-annotations-server-authoritative-dry-run-gap
- https://github.com/JosephCurwin/planlock
- https://kubernetes.io/docs/reference/using-api/api-concepts/
- https://developer.hashicorp.com/terraform/cli/commands/plan (saved-plan
  staleness not stated on the fetched page; staleness rests on HashiCorp
  Discuss threads seen as search snippets)
- https://arxiv.org/html/2603.13417v1 (SERF)
- https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool
- https://goose-docs.ai/docs/guides/recipes/recipe-reference

Research:

- https://arxiv.org/pdf/2606.06284 (ToolChoiceConfusion)
- https://arxiv.org/html/2604.19793 (SkillGraph)
- https://arxiv.org/abs/2408.04682 (ToolSandbox; not fetched, search result)
- Not fetched: tau-bench, NESTFUL, ToolBench, StableToolBench, API-Bank,
  MCP-Bench (2508.20453), PDDL planning-copilot papers.

Searches that returned nothing relevant (negative results): MCP issues for
"approval token" (none); "plan apply" (only agencyHint SEP); academic or
standards hits for "preconditions with mandatory remediation" (none beyond
SERF-style error hints); any public benchmark scoring declared-precondition
compliance (none found).
