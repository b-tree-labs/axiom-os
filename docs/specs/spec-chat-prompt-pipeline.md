# Spec: Chat Prompt Pipeline — the trustworthy-answer turn contract

**Status:** Draft (2026-09-18) · **Decision:** ADR-113 (deterministic answering), ADR-115 (capability-scoped data answering) · **Related:** [prd-deterministic-quantitative-answering.md](../prds/prd-deterministic-quantitative-answering.md), [spec-output-provenance-gate.md](spec-output-provenance-gate.md), [spec-analytics-tool.md](spec-analytics-tool.md), [spec-medallion-answering.md](spec-medallion-answering.md), [spec-prompt-registry.md](spec-prompt-registry.md) (templates), ADR-054 (model tiers), ADR-072/073 (capability projection), ADR-114 (authority gates).

## 1. Purpose & scope

Defines the **turn pipeline** every Axiom chat surface runs — the ordered stages between a user
message and a delivered answer, with each stage's contract and failure posture. This is the
productized form of what today exists as a consumer-layer serving implementation; the ONE-chat
extraction implements this spec, and any surface (CLI, HTTP, MCP-fronted client) MUST traverse
the same pipeline so one identity, one policy, and one answer quality hold everywhere.

Out of scope: transport details (spec-serve), template file format (spec-prompt-registry),
retrieval internals (spec-rag-architecture).

## 2. The stages (ordered; each names its failure posture)

| # | Stage | Contract | On failure |
|---|---|---|---|
| 1 | **Command expansion** | A bare `/command` (or unfilled palette template) expands to real intent BEFORE retrieval and the cache key; the rewritten turn is what the model sees | pass through unexpanded |
| 2 | **Persona resolution** | Client system message wins; else persona model-suffix; else the caller's role default. Resolved BEFORE the cache key so personas never share cached answers | default persona |
| 3 | **Identity + access context** | Resolve principal (`@name:context`); derive access tiers; authz decision logged (enforcement per deployment flag; ADR-114 gates tool effects regardless) | unidentified → public tier only |
| 4 | **Cache key** | `hash(data-epoch + persona + principal + non-system history)`; degraded/suppressed answers are NEVER cached | cache miss |
| 5 | **Query rewrite** | Standalone search query via the fast tier (ADR-054 SMALL_MODEL); bounded tokens | raw message; retrieval never breaks |
| 6 | **Retrieval** | Hybrid (dense + BM25 → RRF → rerank) with the stage-3 access tiers enforced fail-closed | empty hit set |
| 7 | **Tool loop** | Tools = the **discovered** capability set (ADR-072/073 projection: generic medallion verbs, declared quantities, bespoke pack verbs, the analytics tool). Multi-round; every dispatch rides `invoke_capability` (ADR-114 identity + effect gates). **Hardcoded dispatch tables are non-conformant.** Every tool offered is dispatchable (manifest-declared tools included); a call to a name outside the table this turn offered is answered to the model with the nearest offered names and is neither run, put to approval, nor rendered to the user | tool error → typed error result to the model; never fabricated data |
| 8 | **Determinism directive** | The system prompt routes aggregate/fit/rate computation to deterministic tools (analytics / medallion verbs); the model narrates, tools compute | — |
| 9 | **Output-provenance gate** | Fail-closed: a specific value not grounded in this turn's evidence (retrieved docs, tool results, prior turns, published constants — unit-scale-aware) is suppressed and replaced by the configured abstention; suppression is traced with the pre-gate answer | suppression IS the failure handling |
| 10 | **Stamp + persist + trace** | Data-epoch stamp on the answer; memory append; cache write (only clean turns); append-only turn trace incl. gate decisions | best-effort; never blocks the answer |

## 3. Capability-scoped behavior (the three install modes)

The pipeline is identical in all modes; only stage 7's discovered set differs
(prd-deterministic-quantitative-answering §7): chat-core → no data verbs → quantitative
questions about operator data abstain cleanly at stage 9; +data platform → generic
`gold.*` verbs answer them; +domain packs → curated declared verbs sharpen them. Coverage
reconcile runs against the discovered set.

## 3a. Identity and session start (surface contract)

- The identity layer opens with a sentence built from the active branding: the assistant is the
  product's mascot in the product, and internal agent names and roles (the platform's design
  vocabulary) are not an introduction.
- A session states at start which model it uses, where that model runs (this machine, or the
  serving host), and how to switch it; a product adds its own start lines through
  `BrandingConfig.chat_context_fn` (for example, what the session is connected to).
- Run under the platform's own command where a product that declares its command is installed,
  the CLI names that command once on stderr.
- Log records are operator diagnostics: they go to the user-state chat log, and to stderr only
  with `--verbose`; they never interleave with the conversation.
- Streamed model output is decoded as UTF-8 (SSE is UTF-8 by definition, whatever the
  `Content-Type` omits). A terminal renderer strips control characters and degrades characters
  its console encoding cannot represent, so its line accounting matches the terminal.

## 3b. A model too small for the turn, and the per-answer footer

- When the answering model's context window cannot hold the turn (the tool definitions are
  usually most of it), the turn says so plainly: it names the model, its context window, the
  approximate size of the request and of the tool definitions, and the command that configures a
  larger provider (`<cli> connect list`, then `<cli> connect <preset>`). It never retries the
  request without its tools to make it fit: a tool-less answer to a question about the person's
  data is invented, and reads as tool-cited. (The tool-less retry remains only for a server that
  rejects the `tools` parameter itself.) Streaming and non-streaming turns behave the same; the
  gateway marks such a response `error = CONTEXT_WINDOW_EXCEEDED`.
- Every turn that reached a model ends with its footer (`render_status`: model, tokens, cost or
  "unpriced", tier, where it ran), whether it ended in an answer, a refusal like the one above,
  an unrunnable tool attempt, exhausted tool rounds, or the turn deadline.

## 3c. The web app's screen, and changing it from chat (dual drive)

The web surface sends `view_context` with each message (`POST /api/v1/chat/conversations/{id}/messages`).
Its shape is appkit's `chat/viewContext.ts`, schema `axiom.view_context/1`, mirrored by
`chat/view_drive.py`: route, screen, site, binding, the open groups and graphs (`key` is
`<group>/<graph>`), the focused graph key, the selection, the actions the page accepts
(`create_chart`, `update_chart`, `remove_chart`, `focus`, `set_window`, `open_view`, `set_control`), the views it can
open, flags, and a prose summary. A graph also says its chart `kind` and its `controls` (id, type,
label, current value, options), so the model can name a kind on create or update (checked against the
ChartSpec 2.0 registry, ADR-214), create a chart from a whole `spec`, and move a slider, dropdown,
toggle, segmented picker, range or playback with `set_control` (refused with a reason when the chart has
no such control or the control cannot take the value). On a flame or icicle chart, `focus` may carry a frame `path`
(`'a;b;c'`), which zooms the chart's `path` control to that frame; search, invert and direction are
ordinary `set_control` targets. On a 3D chart (`mesh3d`, `surface`, `scatter3d`, `volume`), `set_camera` takes a view
(`iso`, `top`, `side`, `front`) or `{azimuth, elevation, zoom}`, and `focus` may carry an `element` id
to pick. A terminal sends none, and nothing below happens there.

- **Hidden from the transcript.** The context is parsed, bounded (16 KiB; 8 groups, 24 graphs, 32
  series a graph; the focused graph is kept through every cut and `truncated` says something was
  dropped) and carried on the request's scope (`ChatScope.view_context`). It is rendered into the
  `session_memory` layer as a description of the person's screen, not an instruction. It is never
  added to the session's messages and never stored with the conversation. The older
  `{surface, summary}` shape and a bare string are read as summary-only context with no actions.
- **Actions.** A command is validated against the view (the graph exists, a window has both ends
  and ends after it starts, the screen accepts the action, a view exists) and travels as a
  `ui_command` SSE frame carrying `id`, `auto` and `source`. The page applies it through its own
  handlers, with an undo. Nothing on the node changes.
- **Two paths, never both.** A model that calls client tools gets `ui_<action>` tools for the
  actions the view accepts (READ: they only propose). A model that ignores client tools (a site
  node's chat runs its own verbs) cannot, so a deterministic planner reads clear requests first
  ("plot X for <day>", "add Y to that chart", "remove ...", "zoom to <day>", "open <view>",
  "focus <name>"), resolving series names against the bound site's channel list (`data.channels`)
  by name tokens, by coverage of the requested window, and with modelled or estimated feeds ranked
  below measured ones. When it plans, the tools are withheld that turn; a name nothing matches is
  said, not guessed.
- **The focused chart, answered from its series.** A question about the focused chart reads each
  of its series (up to four) with `data.show` over the chart's window before the model answers
  (the maximum always, the minimum when it is asked about), through the ordinary tool loop, so the
  turn is a measurement. The numbers go into the prompt and the answer ends with a line naming the
  verb, the site, the series, the window and the bucket.
- **A site node is asked whole.** For a provider placed on a remote node the round is not
  streamed: the node gates its whole answer before sending any of it, and only the non-streaming
  response carries `x_verbs_called`, without which an answer the node read with its own verbs was
  labelled "not a measurement".
- **A bound data question that names a channel is read here.** When the question names a channel
  the bound site lists (`data.channels`) and nothing else has read it this turn, the turn reads it
  before the model answers: `data.latest` for latest/current/now, otherwise `data.show` (maximum,
  and minimum when asked) over the named day or the latest day. A node whose model has no tools
  for the site still gets a reading this way, and the answer carries the source line. A read the
  node refused with a rate limit is tried once more after a pause, inside the turn's deadline.
- **Never an empty answer.** A bound turn whose model returned no text answers with what its own
  reads found, or says plainly that the node returned no answer. An answer that ignores this
  turn's own reading (quotes none of its numbers) is led by that reading.

## 4. Conformance

- One pipeline implementation; surfaces differ only in transport adapters.
- Stage order is normative: expansion before cache key; persona before cache key; gate before
  stamp/persist/cache; suppressed or errored turns never cached.
- Every stage's failure posture is the one listed — a stage that fails open where this table
  says fail-closed (or vice versa) is a conformance bug.
- The turn trace must be sufficient to answer "why did the gate block this" without reproducing
  the turn (query, verbs+args, gate decision, unsupported claims, pre-gate answer).
