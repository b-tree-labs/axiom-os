# Reuse catalog: what already exists, and what must not be rebuilt

**Design contract:** [./README.md](./README.md). This catalog is its companion.

## Purpose

Maximal reuse. Before building any element of the web app, find its row here. If the row says
`exists` or `partial`, extend the named component in the named place. If it says `missing`,
build it once, in the layer shown, and add the row's "Extend here" location to your plan.
A product built on this platform keeps its own catalog that extends these rows; it never
forks a row.

## How to read

Every row has the same fields.

- **Existing component:** repo and path, each resolved with `git show origin/main:<path>`.
- **Status:** `exists` (usable now), `partial` (a base exists, the design needs more), `missing`
  (nothing on main). Anything that lives only on an unmerged branch is stated as "partial,
  branch X, not on main" and is never counted as `exists`.
- **Extend here:** the smallest addition, and where it goes.
- **MUST NOT rebuild:** the thing an agent is tempted to write again.
- **Layer:** Axiom generic, appkit UI, or "a domain product extends this".
- **Evidence:** the sha-verified paths. Paths are relative to the repo named.

Repos: `axiom` is the platform (`src/axiom/...`). `appkit` is the React kit (`frontend/src/...`).
Design ADRs 220 to 229 and the specs they cite are on the design branch `docs/spec-site-view`
(PR #1516) and are not on main. Rows that depend on them say so.

## Repo SHAs used (origin/main)

| Repo | Short SHA | Commit date |
|---|---|---|
| axiom (b-tree-labs/axiom-os-private) | ddfb0908 | 2026-10-30 |
| appkit (b-tree-labs/axiom-appkit) | beae8ac | 2026-10-29 |

## Summary

32 rows: 12 exists, 16 partial, 4 missing.

| Status | Rows |
|---|---|
| exists | search-modal, chat, action-cards, datatable, library, charts-page, chartspec-renderer, attest, flags, data-door-gold, scheduler-executors, mcp-cli-parity |
| partial | shell, rail, discuss, expand-display, chart-slots-pins, signing-card, log-audit-chain, gate-invites-keys, fga-rbac-abac, notifications-needs-you, calendar-scheduleditem, people-actor, app-db-publish, model-registry, run-providers, tokens-themes |
| missing | concert, coreforge, improvement-loop-objects, empty-state-new |

## Rows

### shell
- **Existing component:** appkit `frontend/src/NodeApp.tsx`, `DefaultApp.tsx`, `AppShell.tsx`, `WorkbenchLayout.tsx`, `Breadcrumbs.tsx`, `AccountMenu.tsx`, `ModeChip.tsx`, `DetailDrawer.tsx`.
- **Status:** partial. The frame, drawer, crumbs and account block exist. The scope selector, the Needs you chip and one Expand control do not.
- **Extend here:** add the scope selector and chip as slots on `DefaultApp.tsx`; add the page-level crumb restore (MVP-00-A5) to `Breadcrumbs.tsx`.
- **MUST NOT rebuild:** a second app frame, a per-page rail, or a per-page drawer. Mockup shells drifted page to page once already.
- **Layer:** appkit UI.
- **Evidence:** the six files above resolved on appkit origin/main; acceptance rows MVP-00-A1 to A7 in `docs/specs/spec-webapp-buildout-mvp.md` (branch).

### rail
- **Existing component:** appkit `frontend/src/NavRail.tsx`, `navDefaults.ts` (`AXIOM_VERBS`), `navVocabulary.ts`, `features.ts` (verb on/off/soon), `settingsNav.ts`.
- **Status:** partial. `AXIOM_VERBS` is Control, Chart, Book, Log. The design rail (ADR-223, branch) is Chat, Site, [Model], [Chart], Log, Library, Team, History (Model and Chart shown by user type), with Search as a full-width row under the wordmark plus ⌘K, not a rail item.
- **Extend here:** change the defaults and labels in `navDefaults.ts`; products compose over it with the existing add, override, order and omit helpers.
- **MUST NOT rebuild:** a hand-written rail in a product, or a rail item for Search. See Duplicates D4.
- **Layer:** appkit UI.
- **Evidence:** `navDefaults.ts` lines 116 to 130 (verbs and the "Book" label) on appkit origin/main.

### search-modal
- **Existing component:** appkit `frontend/src/SearchPalette.tsx` (spotlight, quick rows, debounced suggest, Cmd+K), `NodeSearch.tsx` (full results page). Axiom `src/axiom/extensions/builtins/search/engine.py`, `api.py`, `skills/__init__.py`.
- **Status:** exists.
- **Extend here:** place the full-width search row under the wordmark in `DefaultApp.tsx`; make results permission-filtered including counts (MVP-01-A1) in `search/engine.py`.
- **MUST NOT rebuild:** a per-section search box or a Search rail page.
- **Layer:** appkit UI plus Axiom generic.
- **Evidence:** the five files above.

### chat
- **Existing component:** appkit `frontend/src/chat/ChatInterface.tsx`, `ChatContext.tsx`, `axiomEngine.ts`, `chat/SurfaceChat.tsx`, `surface/ChatPanel.tsx`, `ChatOverlay.tsx`. Axiom `src/axiom/extensions/builtins/chat/` and `src/axiom/serve/chat_completions.py`.
- **Status:** exists. Centered landing and docked slide-out are two presentations of one component.
- **Extend here:** the "+" attachment into Library and the scope chip, both inside `ChatInterface.tsx`.
- **MUST NOT rebuild:** a second chat input on any screen (MVP-02-A6). Questions from other pages open this chat. See Duplicates D3.
- **Layer:** appkit UI plus Axiom generic.
- **Evidence:** the paths above.

### action-cards
- **Existing component:** appkit `frontend/src/NextCards.tsx`, `chat/SuggestedActions.tsx`, flag key `next_cards` in `flags.ts`. Axiom `src/axiom/extensions/builtins/next/engine.py`, `api.py`.
- **Status:** exists.
- **Extend here:** seed site cards through the `next` engine, not in the browser.
- **MUST NOT rebuild:** card ranking in a page. It is `next.suggest`, and CLI, MCP and chat return the same list.
- **Layer:** Axiom generic plus appkit UI.
- **Evidence:** the four files above.

### discuss
- **Existing component:** appkit `frontend/src/charts/Charts.tsx` (`discuss`), `BriefViews.tsx` (Discuss on a case), `surface/ChatPanel.tsx`, `surface/openers.ts`, `chat/viewContext.ts`, `surface/context.ts`.
- **Status:** partial. Discuss exists on Charts and on a case. A generic "Discuss this object" action for every drawer is a design addition (MVP-25, branch).
- **Extend here:** one `Discuss` action in `DetailDrawer.tsx` that calls `surface/openers.ts`.
- **MUST NOT rebuild:** an "Ask about this" box. Discuss opens the one chat with the view context attached.
- **Layer:** appkit UI.
- **Evidence:** the files above on appkit origin/main.

### expand-display
- **Existing component:** appkit wall mode in `frontend/src/charts/Charts.tsx` and `charts/groups.ts` (`kioskFromParams`); expandable previews in `library/preview/PreviewPane.tsx`; `chat/InlineChartModal.tsx`.
- **Status:** partial, branch only for the shared piece. Three separate expand behaviours exist. The shared `Expandable` and `DisplayMode` do not exist on main.
- **Extend here:** extract one `Expandable` wrapper (Esc and the corner close, hover or touch reveal) in a new `frontend/src/Expandable.tsx` that the three callers adopt; display actors and kiosk URLs are the P4 addition.
- **MUST NOT rebuild:** another full-screen toggle per component.
- **Layer:** appkit UI.
- **Evidence:** the three callers above; the design is in `docs/specs/spec-site-view.md` addendum v7 (branch).

### datatable
- **Existing component:** appkit `frontend/src/table/DataTable.tsx`, `DataTablePanel.tsx`, `useTable.ts`, `useTableSelection.ts`, `ColumnProfileBand.tsx`, `rowSource.ts`, `doorRowSource.ts`, `toCsv.ts`, `README.md`. Axiom `src/axiom/extensions/builtins/scidisplay/table_spec.py`.
- **Status:** exists. In use in `FlagsAdmin.tsx` and `NodeSettings.tsx`.
- **Extend here:** new row sources through `rowSource.ts`; new cell kinds through `cellRenderers.tsx`.
- **MUST NOT rebuild:** a table for any list. Every table is this one (MVP-20-A8).
- **Layer:** appkit UI.
- **Evidence:** the files above.

### library
- **Existing component:** appkit `frontend/src/library/` (`LibraryPage.tsx`, `browser/LibraryBrowser.tsx`, `UploadProvider.tsx`, `preview/PreviewPane.tsx`, `EcReview.tsx`, `book/DataBook.tsx`). Axiom `src/axiom/extensions/builtins/library/` (`service.py`, `store.py`, `pipeline.py`, `intake.py`, `extract/`, `preview/`, `book/`), ADR-212 and ADR-213.
- **Status:** exists.
- **Extend here:** labels and retention class on each item (MVP-04-A1, MVP-22) in `library/classify.py` and `taxonomy.py`; generated record-package folders through `book/`.
- **MUST NOT rebuild:** a file browser, uploader or previewer. Publications and results are files in a Library folder, with no new screen.
- **Layer:** Axiom generic plus appkit UI.
- **Evidence:** the paths above.

### charts-page
- **Existing component:** appkit `frontend/src/charts/Charts.tsx`, `GroupSection.tsx`, `SiteControls.tsx`, `groups.ts`, `saved.ts`, `views.ts`, `client.ts`, `drive.ts`, `lane.ts`. Axiom `src/axiom/extensions/builtins/charts/api.py`, `block.py`, `data_platform/charts/__init__.py`, `webapp/api/catalog.py`.
- **Status:** exists. Groups, window, Configure, Share, Layout, wall mode and Discuss are built.
- **Extend here:** new feeds appear through connector plus channel map with no code (MVP-03-A1); keep the standalone `charts/Ask.tsx` hidden inside any shell.
- **MUST NOT rebuild:** a Chart page, a plain-words-or-SQL ask bar, or a second catalog of feeds.
- **Layer:** appkit UI plus Axiom generic.
- **Evidence:** the paths above.

### chartspec-renderer
- **Existing component:** Axiom `src/axiom/extensions/builtins/scidisplay/unified_spec.py` (ChartSpec 2.0), `chart_kinds.py`, `chart_svg.py`, `suggest.py`, `plotly_import.py`, `unified_migrate.py`, ADR-214 and ADR-216. appkit `frontend/src/figure/spec.ts` (browser mirror), `Figure.tsx`, `chat/SuggestedChart.tsx`, `chat/chartSuggestions.ts`.
- **Status:** exists. The browser holds the spec and asks the platform to draw it.
- **Extend here:** a new kind is added to `unified_spec.py` and `chart_kinds.py`, then mirrored in `figure/spec.ts` (`SPEC_KINDS`).
- **MUST NOT rebuild:** a chart drawn in the browser, a new spec vocabulary, or a chart library in a page. See Duplicates D1.
- **Layer:** Axiom generic plus appkit UI.
- **Evidence:** the paths above.

### chart-slots-pins
- **Existing component:** appkit saved groups and shared views in `frontend/src/charts/saved.ts`, `groups.ts`, `views.ts`; "save to catalog" in `chat/SuggestedChart.tsx`.
- **Status:** partial. Saving a chart or group exists. "Pin to a slot" on another page is designed only (spec-site-view addendum v7, branch).
- **Extend here:** a `slot` field on a saved chart in `charts/saved.ts` and a `ChartSlot` renderer that other pages mount.
- **MUST NOT rebuild:** a chart widget with its own data call on Site, Model or Device pages. A page shows a chart slot; the figure lane draws it.
- **Layer:** appkit UI.
- **Evidence:** the three files above.

### signing-card
- **Existing component:** appkit `frontend/src/ConfirmCard.tsx` ("this is exactly what will be signed", class `axk-signcard`), `DecisionActions.tsx` (Primary, Hold, Ask), `signing.ts` (the sign sequence), `EntryComposer.tsx`, `RecordThread.tsx`, ADR-144 and ADR-146.
- **Status:** partial. The card and sequence exist. ADR-227 (one signing card for every promotion and approval, branch) is not yet applied to the promotion and case flows.
- **Extend here:** make `ConfirmCard.tsx` take any presentation from `attestApi.ts`, and route promotions and approvals through it.
- **MUST NOT rebuild:** a per-feature confirm dialog or a Sign button that does not show the exact recorded content.
- **Layer:** appkit UI.
- **Evidence:** the files above.

### attest
- **Existing component:** Axiom `src/axiom/attest/canonical.py`, `chain.py`, `anchor.py`, `tools/verify.py`; `src/axiom/extensions/builtins/attest/` (`service.py`, `logbooks.py`, `obligations.py`, `grants.py`, `presence.py`, `devices.py`, `signing.py`, `store.py`, `skills/`). appkit `frontend/src/attestApi.ts`, `LogbookPage.tsx`, `LogbookView.tsx`, `useLogbook.ts`, `obligation.ts`, `AlarmBand.tsx`, `ObligationCountdown.tsx`, `ProofBadge.tsx`.
- **Status:** exists. ADR-142 to ADR-147 and ADR-150.
- **Extend here:** new record kinds (waiver, finding, corrective action) as entry types in a logbook definition, not new tables.
- **MUST NOT rebuild:** a record store, hash chain, signature scheme or logbook UI. Logbooks come from extensions inside the one Log host.
- **Layer:** Axiom generic plus appkit UI. A domain product adds its logbook definitions.
- **Evidence:** the paths above.

### log-audit-chain
- **Existing component:** Axiom `src/axiom/extensions/builtins/authz/skills/chain_verdicts.py`, `show_verdict.py`, `explain.py`, `healthcheck.py` (the decision chain behind the audit tools); `src/axiom/attest/chain.py`; `src/axiom/extensions/builtins/receipts/` (cases, brief, cause). appkit `ProofBadge.tsx`, `RecordThread.tsx`, `CaseCard.tsx`, `control/Control.tsx`.
- **Status:** partial. Chain verification and decision records exist. The Log page's Audit mode and the one change log (MVP-13) are not built.
- **Extend here:** add audit rows as a record kind through `authz` and `attest`; render in the Log host with `DataTable`.
- **MUST NOT rebuild:** a second audit table or hash chain. `axi log` is the command noun for logs, and the system log becomes the log `platform` (ADR-243). See Duplicates D8.
- **Layer:** Axiom generic plus appkit UI.
- **Evidence:** the paths above.

### concert
- **Existing component:** none on main. ADR-229 and spec-site-view addendum v6 are on branch `docs/spec-site-view`.
- **Status:** missing.
- **Extend here:** `concert.*` skills in a new Axiom extension that reads the `attest` reviews logbook, `obligations.py` and the chain verifier; its queue is a `DataTable` in the Log host.
- **MUST NOT rebuild:** evidence packaging (`attest/tools/verify.py` already ships in packages), a second signature path, or an external-audit portal outside Log.
- **Layer:** Axiom generic. A domain product adds its control catalog.
- **Evidence:** `src/axiom/attest/tools/verify.py` resolved; the ADR is branch-only.

### gate-invites-keys
- **Existing component:** Axiom `src/axiom/webauth/invitations.py`, `api_keys.py`, `keys.py`, `session.py`, `jwt.py`, `magic_link.py`; `src/axiom/extensions/builtins/webgate/api/redeem_route.py`, `self_key_route.py`, `account_flows.py`; `src/axiom/extensions/builtins/auth/`. appkit `useGateUser.ts`, `Profile.tsx`, `AccountMenu.tsx`.
- **Status:** partial. The backend exists. No invitation or key-management screen is in appkit.
- **Extend here:** an Account section for keys and a Settings section for invitations in `Settings.tsx`, calling the existing routes.
- **MUST NOT rebuild:** token minting, invitation single-use logic, or a second sign-in. Upgrades keep sessions (MVP-14-A1).
- **Layer:** Axiom generic plus appkit UI.
- **Evidence:** the paths above.

### fga-rbac-abac
- **Existing component:** Axiom `src/axiom/extensions/builtins/authz/decide.py`, `policy.py`, `rules.py`, `openfga.py`, `fga/site.fga`, `fga/starter.fga`; `src/axiom/governance/` (`actor.py`, `capability.py`, `classification.py`, `resource.py`).
- **Status:** partial. A decision function and model exist. One function answering page, API, search, chat and MCP (MVP-07-A1) is not yet proven by a test.
- **Extend here:** add the five-surface test beside `authz/decide.py`; export-control and retention labels as attributes in `governance/classification.py`.
- **MUST NOT rebuild:** a per-surface permission check or role table in a page.
- **Layer:** Axiom generic.
- **Evidence:** the paths above.

### flags
- **Existing component:** Axiom `src/axiom/extensions/builtins/flags/` (`registry.py`, `service.py`, `store.py`, `api.py`, `cli.py`, `db_models.py`), ADR-210. appkit `frontend/src/flags.ts`, `features.ts`, `FlagProvider.tsx`, `FlagsAdmin.tsx`.
- **Status:** exists. Values are on, off, soon. Do not confuse with `axi features` node functions (ADR-164).
- **Extend here:** declare new flags in an extension's manifest; add the variant scope (MVP-12-A4) in `flags/service.py`.
- **MUST NOT rebuild:** a flag table, an environment-variable switch, or `DEFAULT_FEATURES` in a product.
- **Layer:** Axiom generic plus appkit UI.
- **Evidence:** the paths above.

### notifications-needs-you
- **Existing component:** Axiom `src/axiom/extensions/builtins/notifications/inbox.py`, `approval_bridge.py`, `skills/list_inbox.py`, `skills/ack.py`, `channels/`, `gateway/`; `next/engine.py`; `receipts/brief.py`. appkit `BriefViews.tsx` (needs-you rows), `control/FacetBar.tsx`, `CaseCard.tsx`, `NextCards.tsx`.
- **Status:** partial. The inbox and cards exist. A top-right "Needs you" chip and the person-scoped queue page (MVP-21) do not.
- **Extend here:** a `NeedsYouChip` in the `DefaultApp.tsx` header slot reading `notifications` inbox plus pending approvals; the queue renders with `DataTable`.
- **MUST NOT rebuild:** a notification store, a bell with its own model, or per-page "waiting" banners.
- **Layer:** Axiom generic plus appkit UI.
- **Evidence:** the paths above.

### calendar-scheduleditem
- **Existing component:** Axiom `src/axiom/extensions/builtins/schedule/calendar/` (`protocol.py`, `sync.py`, `vendors/`), `schedule/store.py`, `conflicts.py`, `blackout.py`, `cadence.py`. appkit `figure/DayPicker.tsx` (a date picker only).
- **Status:** partial. Sources and a vendor sync exist. `ScheduledItem` does not exist in code (spec-site-view addendum v14, branch); no calendar page is in appkit.
- **Extend here:** define `ScheduledItem` in `schedule/` as a read-only projection; one provider per source; the agenda renders in a new appkit `Agenda` over `DataTable`.
- **MUST NOT rebuild:** a calendar store. Features own their items; the calendar reads them and writes nothing (MVP-24-A5).
- **Layer:** Axiom generic. A domain product adds providers.
- **Evidence:** the paths above.

### people-actor
- **Existing component:** Axiom `src/axiom/extensions/builtins/directory/` (`protocol.py`, `resolution.py`, `sync.py`), `agents/skills/roster.py`, `agents/consent.py`, `agents/liveness.py`, `principal/models.py`, `src/axiom/governance/actor.py`. appkit `control/AgentDrawer.tsx`, `control/AgentProfile.tsx`, `Avatar.tsx`, `surface/AgentMark.tsx`, `EntityRef.tsx`, `Profile.tsx`.
- **Status:** partial. Directory, roster and drawers exist. The unified Team list (people, agents, nodes in one list, MVP-23) does not.
- **Extend here:** a `Team` page using `DataTable` over the directory, roster and node skills; open each row in `DetailDrawer.tsx`.
- **MUST NOT rebuild:** a people table, an agent registry, or a second profile drawer. The directory writes nothing new (MVP-23-A4).
- **Layer:** Axiom generic plus appkit UI.
- **Evidence:** the paths above.

### data-door-gold
- **Existing component:** Axiom `src/axiom/extensions/builtins/data_platform/read_api.py`, `skills/gold.py`, `tiers.py`, `conformance/catalogue.py`, `timeseries/`, ADR-128. appkit `table/doorRowSource.ts`, `charts/client.ts`.
- **Status:** exists. Consumers read gold only.
- **Extend here:** a new view is a new gold object and a gold verb, then a row source.
- **MUST NOT rebuild:** a query path that reads silver for a consumer, or a page-local SQL layer. If gold lacks it, add a gold object.
- **Layer:** Axiom generic.
- **Evidence:** the paths above.

### app-db-publish
- **Existing component:** Axiom `src/axiom/extensions/builtins/webapp/catalog/sync.py`, `store.py`, `models.py`, `provenance_check.py`, `webapp/api/catalog.py`, `bound_web.py`, ADR-129.
- **Status:** partial. Channel catalog publish from gold works. The rule that every published table names its gold source and version (MVP-11-A4) and the rebuild-without-touching-state test (MVP-11-A6) are not proven.
- **Extend here:** `catalog/provenance_check.py` plus a generic published-table registry.
- **MUST NOT rebuild:** a join from the app database into gold or a copy from silver.
- **Layer:** Axiom generic.
- **Evidence:** the paths above.

### scheduler-executors
- **Existing component:** Axiom `src/axiom/extensions/builtins/schedule/engine.py` (`Executor` protocol), `executor.py` (`SkillExecutor`), `lease.py`, `recovery.py`, `store.py`, `manifest.py`, `skills/register.py`, `fire_now.py`.
- **Status:** exists. Single-flight leases, durable fire, recovery.
- **Extend here:** register a new job with `schedule.register`; a new executor implements the `Executor` protocol.
- **MUST NOT rebuild:** a cron loop, a job table, or a poller inside an extension.
- **Layer:** Axiom generic.
- **Evidence:** the paths above.

### model-registry
- **Existing component:** Axiom `src/axiom/artifacts/registry.py` (`ArtifactRegistry`, content-addressed, versioned, optionally signed), `pg_backend.py`.
- **Status:** partial. The content-addressed store exists and is used by classroom and the agent pipeline. The model nouns, the state and evidence axes, and `model_ref` resolution are absent (prd-model-loop, branch).
- **Extend here:** a model catalog extension that registers kinds (spec, deck, code, surrogate) as `ArtifactRegistry` entries. A domain product supplies its own kinds and names the catalog.
- **MUST NOT rebuild:** a second versioned store or a registry inside a model repository. See Duplicates D6.
- **Layer:** Axiom generic. A domain product extends this.
- **Evidence:** the paths above; `docs/prds/prd-model-loop.md` lines 65 to 71 (branch).

### coreforge
- **Existing component:** none in Axiom. A domain product owns the geometry and material builders; Axiom needs only the hook that checks a spec document.
- **Status:** missing (generic side).
- **Extend here:** a spec-check skill contract in the model catalog extension; builders register as providers.
- **MUST NOT rebuild:** a builder inside the platform, or a spec format that is not the product's versioned spec document.
- **Layer:** a domain product extends this.
- **Evidence:** none to cite; see the product catalog.

### run-providers
- **Existing component:** Axiom `src/axiom/compute/dispatch.py` (signed dispatch), `compute/adapters/base.py`, `mock.py`, `compute/events.py`; `src/axiom/compute_decomposition/` (`runner.py`, `registry.py`, `routing.py`, `coordination/peer_dispatcher.py`). Branch `feat/modsim-engine-skeleton` adds `src/axiom/modsim/` and ADR-204 (providers, slots, run record, control gate); not on main.
- **Status:** partial. Adapters and dispatch exist. There is no batch-queue (HPC) provider on main, and the run-record engine is branch-only.
- **Extend here:** a batch-queue adapter under `compute/adapters/` implementing `base.py`; the engine on the branch lands first.
- **MUST NOT rebuild:** a job submitter in an extension, a run table, or a second dispatch signer.
- **Layer:** Axiom generic. A domain product registers its codes and queue.
- **Evidence:** the main paths above; the branch tip is `4284f52b` (2026-10-27).

### improvement-loop-objects
- **Existing component:** none for hypothesis, output tag, training set, V&V record or monitor on main. Partial pieces: `src/axiom/extensions/builtins/graduation/shadow.py` (shadow), `src/axiom/bench/` (benchmarks), `src/axiom/artifacts/registry.py` (versions), `attest/` (promotion evidence).
- **Status:** missing. Design is `docs/prds/prd-model-loop.md` (branch).
- **Extend here:** objects as catalog entries and engine slots; promotion is an attest record signed through the one signing card.
- **MUST NOT rebuild:** a data-versioning store (training data versions live in the catalog over gold), a second approval flow, or notebook execution in the app.
- **Layer:** Axiom generic. A domain product fills the nouns.
- **Evidence:** the partial paths above resolved on main.

### tokens-themes
- **Existing component:** appkit `frontend/src/tokens.css` (the single source of UI tokens, light and dark), `theme.ts` (system, light, dark), `figure/colour.ts` (series colour as `light-dark()`). Axiom `src/axiom/extensions/builtins/scidisplay/palettes.py` (chart palettes), `chart_colour.py`, `colour_preferences.py`, `src/axiom/infra/web_brand.py` (brand accent).
- **Status:** partial. Tokens and theming exist. The semantic status tokens with contrast and colour-vision checks, and the refusal of a brand accent near a status colour (ADR-226 and MVP-20, branch), do not.
- **Extend here:** add status tokens to `tokens.css`; add the accent check to `infra/web_brand.py`.
- **MUST NOT rebuild:** a palette list or a colour literal in a component. See Duplicates D2. `scidisplay/themes.py` is a code-highlighting theme and is not a UI palette.
- **Layer:** appkit UI plus Axiom generic.
- **Evidence:** the paths above.

### empty-state-new
- **Existing component:** only a private `EmptyState` inside appkit `frontend/src/control/Control.tsx`, and `Notice.tsx` (a dismissible state line, not an empty state).
- **Status:** missing as a shared component. MVP-25 is branch-only.
- **Extend here:** a shared `EmptyState` and a `NewButton` named by its noun in new `frontend/src/EmptyState.tsx`; `Control.tsx` adopts it.
- **MUST NOT rebuild:** a per-page empty message or a "Create" button that is not "+ New <noun>".
- **Layer:** appkit UI.
- **Evidence:** `Control.tsx` lines 242 and 332 on appkit origin/main.

### mcp-cli-parity
- **Existing component:** Axiom `src/axiom/infra/capability_projection.py` (one registered skill projects to CLI, MCP and chat tool), ADR-056, ADR-072, ADR-073; `src/axiom/extensions/builtins/mcp/` (`server.py`, `skill_tools.py`, `manifest_schema.py`, `http_mount.py`).
- **Status:** exists. Whether every web skill is projected is a manifest check (MVP-17-A1).
- **Extend here:** write the feature as a skill in an extension; do not add a route that has no skill behind it.
- **MUST NOT rebuild:** an HTTP-only endpoint, a hand-written MCP tool, or a CLI verb with its own logic.
- **Layer:** Axiom generic.
- **Evidence:** the paths above and `src/axiom/infra/tests/test_capability_projection.py`.

## Duplicates and winners

### D1 Chart vocabularies
Three exist: the 1.x query document (`scidisplay/chart_spec.py`), the data-platform catalogue entry (`data_platform/charts/__init__.py`), and ChartSpec 2.0 (`scidisplay/unified_spec.py`, mirrored in appkit `figure/spec.ts`). **Winner: ChartSpec 2.0.** Deciding document: ADR-214 (`docs/adrs/adr-214-one-chart-spec-for-native-and-imported-charts.md`). `scidisplay/unified_migrate.py` already reads the two old forms. `chart_choice.py` (a chooser limited to the 1.x kind) is superseded by `suggest.py` per ADR-216. Do not add a vocabulary in any product; a product's chart code fills a ChartSpec 2.0 document.

### D2 Palette and colour registries
`scidisplay/palettes.py` is the data-colour registry (categorical, sequential, diverging, contrast-checked). appkit `charts/PalettePicker.tsx` carries its own hard-coded four-name list (`PALETTES`) that mirrors it by hand. UI chrome colour lives separately in `tokens.css`. **Winner for data colour: `palettes.py`; the picker should read the server list. Winner for UI and status colour: `tokens.css`, with ADR-226 (branch) as the semantic status contract.** `themes.py` (code highlighting) and `infra/web_brand.py` (brand accent) are not rivals.

### D3 Chat inputs
`chat/ChatInterface.tsx` is the chat. Other inputs: `ChatDock.tsx` (the older panel idiom, an `<input>`), `charts/Ask.tsx` (a textarea posting to the site ask route), `control/Conversation.tsx` (a reply box inside one agent thread). **Winner: `ChatInterface` plus the Discuss slide-out**, per MVP-02-A6 and the one-input rule. `Ask.tsx` stays hidden whenever a shell is present and should be retired; `ChatDock.tsx` should be retired or re-based on `ChatInterface`; the thread reply in `Conversation.tsx` is a reply to a record, not an ask box, and stays.

### D4 Rail vocabulary
`navDefaults.ts` `AXIOM_VERBS` is Control, Chart, Book, Log. The design rail (ADR-223, branch) is Chat, Site, [Model], [Chart], Log, Library, Team, History (Model and Chart shown by user type), with Search as a row under the wordmark plus ⌘K. **Winner: ADR-223**, and "Book" is a retired label for Library (ADR-211 retires the older agent-verb name). Edit `navDefaults.ts`; do not patch it in a product.

### D5 Signing and attestation paths
Human acts: `src/axiom/attest/` chain and the attest extension (ADR-142, ADR-143). Machine compute receipts: `compute/dispatch.py` and `scidisplay/compute_signing.py`. Extension supply chain: `src/axiom/cli/ext/signing.py`. These answer different questions and all three stay. In the UI, `ConfirmCard.tsx` is the signing card; **winner for any person's signature: the attest chain through `ConfirmCard` (ADR-144, ADR-227 branch)**. No feature gets its own signing flow.

### D6 Model catalog naming
The platform has `ArtifactRegistry` (content-addressed store). A domain product ships its own model catalog that does not use it today. **Winner: one catalog, built as the product's model registry over `ArtifactRegistry` content addressing** (`docs/prds/prd-model-loop.md` lines 65 to 71, branch). The product's catalog keeps its name and moves onto the shared store; no third registry.

### D7 Channel catalogs
`data_platform/conformance/catalogue.py` (gold catalogue) and `webapp/catalog/store.py` (app-database copy) both list channels. **Winner: the gold catalogue is the source; the app copy is a published projection** (ADR-129). Co-refresh them; do not add a third.

### D8 "Log" naming
People see "log"; code keeps `logbook`; `log` is the command noun (`axi log <log> ...`), and today's system log in `builtins/log/cli.py` moves to `axi log platform` (ADR-243). The Log rail item is the logs and audit (`attest/`).

### D9 Flags and node functions
`flags/` (ADR-210) and `axi features` (`infra/node_functions.py`, ADR-164) are different. `settings` is a preference store. Keep them separate.
