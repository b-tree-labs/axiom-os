# ADR-062 — Box as a first-class storage connector

**Status:** Accepted — 2026-06-01
**Owner:** @ben
**Related:** ADR-057 (connector primitive), ADR-059 (connector-first vendor unification), ADR-060 (cross-agent event routing), radman blocker analysis 2026-06-01

## Context

Box appears in three places in Axiom today and unifies in none:

| Surface | File | Capability |
|---|---|---|
| `axi data register … box` | `extensions/builtins/data_platform/sources/box/` | **read** (folder ingest via Playwright-SSO or Box Session API) |
| PRESS upload target | `extensions/builtins/publishing/providers/storage/box_browser.py` | **write** (browser-driven upload) |
| n/a | — | **watch** (event-driven file landed / reply on a thread) |
| n/a | — | **reply ingest** (turn comment/reply on a Box file into an inbound agent event) |

Consumer-extension workflows live partially on Box (research corpora, instrument outputs, collaborator artifacts). Per the 2026-06-01 connector-blocker analysis, Box is **Tier-A #2** for unblocking the highest-pull consumer-extension PRD — and the current ingest-only path doesn't support write, watch, or reply ingest.

The recently-shipped connector-first vendor unification (ADR-059) requires that all vendor adapters live under `extensions/builtins/connector/`, with one adapter per vendor regardless of how many downstream agents consume it. The two existing Box implementations need to fold under one `StorageConnectorProvider` surface; PRESS upload + RAG ingest + future watch + reply ingest all dispatch through the same adapter.

## Decision

Promote Box to a **first-class storage connector** under `extensions/builtins/connector/storage/`, with a `StorageConnectorProvider` Protocol covering the four capabilities:

```python
class StorageConnectorProvider(Protocol):
    vendor: str                                          # "box"
    capabilities: frozenset[StorageCapability]            # what this vendor supports

    def list_files(self, params: ListParams) -> Iterator[FileRef]: ...
    def get_file(self, ref: FileRef) -> FileContent: ...
    def put_file(self, ref: FileRef, content: FileContent) -> PutReceipt: ...
    def start_watch(self, params: WatchParams) -> WatchHandle: ...
    def ingest_replies(self, params: ReplyParams) -> Iterator[ReplyEvent]: ...
```

Capabilities that a vendor cannot honor declare absence via the `capabilities` frozenset rather than raising at call time, so the connector wizard and operator-facing `axi connector status` can surface honest support matrices.

Authentication starts with **Box developer token** (operator-pasted) for v1; OAuth lands when the Microsoft 365 Graph OAuth foundation lands (Tier-A #3, which sets the cap-token KEEP handoff pattern that Box OAuth then reuses).

### Folding the existing surfaces

- `data_platform/sources/box/` remains the **runtime ingest source** but constructs its Box SDK calls through the new `StorageConnectorProvider.list_files` / `get_file` rather than maintaining its own client. The `SourceKindProvider` shape stays — only the underlying I/O moves.
- `publishing/providers/storage/box_browser.py` calls `StorageConnectorProvider.put_file` instead of driving the browser directly. The Playwright path stays available as a fallback `BoxBrowserStorageProvider` for the no-API-token case.
- **Watch** dispatches Box webhook V2 events onto the agent bus as `connector.box.file_landed` (consumed by RIVET / PLINTH / future agents per ADR-060).
- **Reply ingest** polls Box comments on watched folders + emits `connector.box.reply` on the bus; HERALD's reply-routing primitive threads them back to the originating `ActionEnvelope`.

### Wizard registration

`connector/wizard.py` gains a `_BoxHandler` registering under vendor key `"box"` with developer-token + folder-id fields. Operators run `axi connector add box` for the same five-step collapse Twilio SMS and Teams already get.

## Consequences

**Wins**
- Consumer extensions get a single `axi connector add box` flow that covers RAG ingest + PRESS upload + (P1) watch + (P1) reply ingest.
- ADR-059's "one adapter per vendor" invariant becomes true for Box.
- Watch + reply ingest emit on the agent bus, so the bridge routes them to HERALD per recipient preferences with no per-agent wiring.
- Sets the `StorageConnectorProvider` Protocol that OneDrive + SharePoint will satisfy when the M365 Graph foundation lands.

**Costs**
- Folding two existing surfaces means careful refactors of `data_platform/sources/box/source.py` + `publishing/providers/storage/box_browser.py`. Both have working test suites and a runtime Dagster sensor depends on the data_platform side. Folding ships as `connector/storage/box.py` first (PR-1, this ADR) with both old paths intact; PR-2 folds data_platform; PR-3 folds publishing.
- Box developer tokens are 60-minute lived. Operators will hit "ReconnectRequired" on day-after runs. Acceptable for v1 (matches the existing data_platform Box experience); OAuth foundation closes it.

**Non-goals for this ADR**
- Box write through OAuth (waits for M365 Graph foundation cap-token handoff to land first).
- SharePoint / OneDrive (separate ADRs once the foundation lands).
- The reply-ingest threading semantics across Box → HERALD ActionEnvelope (HERALD §1.3 covers the model; this ADR commits only to the bus subject).

## Implementation phases

| PR | Scope | Status |
|---|---|---|
| **PR-1** (this branch) | ADR + `StorageConnectorProvider` Protocol + capability enum + `_BoxHandler` wizard registration + TDD-pinned contract tests | this PR |
| **PR-2** | `connector/storage/box.py` with `list_files` + `get_file` over Box Session API; refactor `data_platform/sources/box/source.py` to delegate | next |
| **PR-3** | `put_file` + refactor `publishing/providers/storage/box_browser.py` to delegate | next |
| **PR-4** | `start_watch` (Box webhook V2) + bus emit `connector.box.file_landed`; agent_bridge default route | next |
| **PR-5** | `ingest_replies` + HERALD reply-thread bind-back | next |

PR-1 ships the contract and unblocks the four implementation phases to land independently as Austin pulls each one.
