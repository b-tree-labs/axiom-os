# Spec: web app build-out, P0 foundation and P1 MVP

**Owner:** platform (web) • **Status:** Draft, hand-off for implementation • **Last updated:** 2026-10-30
**Roadmap:** [spec-webapp-buildout-roadmap.md](spec-webapp-buildout-roadmap.md) • **PRD:** [prd-site-view.md](../prds/prd-site-view.md)

A fresh session can build P0 and then P1 from this file and the roadmap alone. The MVP
upgrades a deployed site node's web app (Axiom + the product + the site's brand and
configuration) on the staging node, and runs it as three variants (bare Axiom, bare
product, site; roadmap §4). Items 0–23 each carry scope, what is out of scope,
acceptance rows with IDs, four-surface parity and flags.

**Conventions.**
- **Parity** lists the page, the CLI verb, what chat does, and the MCP tool. One skill
  backs all four (ADR-056 / ADR-072). Verb names marked *(new)* are proposals in
  `noun verb` form; check the registry for an existing verb before adding one.
- **Flags** use the flag contract (`on | off | soon`; default → environment → node →
  account → user; audited). Values are the staging defaults for the site variant;
  the bare variants take registry defaults.
- **Trace** links each row to the PRD (§), the heuristics (H1–H7, PRD §14.7), the
  cohesion rules (R19.n, PRD §19) or the variant rows (VAR). The product's build-out
  index maps these IDs to its journey, parity and audit tables.
- **As built.** Much of this exists. Each item names what to reuse; the first step of
  every work package is to measure what is on `main` and change only what the rows
  need. Build from the as-built system; do not redesign working components (PRD §16.1).

## P0 Foundation

**Entry:** none. **Size:** L. **Work packages** (at most three at once):
P0.a tokens and the component gallery (MVP-20) → P0.b shell (MVP-00) ·
P0.c flags (MVP-12) · P0.d variants and CI smoke (VAR) · P0.e device-class contract ·
P0.f acceptance harness.

**Gate:** every MVP-00, MVP-12, MVP-20, VAR, P0-DEV and P0-ACC row proven; appkit and
Axiom released; the three variants run on a developer machine with `axi app run --all`.

### MVP-00 Shell

**Scope.** The standard rail with no section headers: Chat · Site · Model · Chart ·
Log · Library · Team, then History at the bottom (PRD §12.1, §13.1, §14.1); Model and
Chart are shown by user type or role, as the product decides. Team is universal: on
every rail by default, and a person or site may switch it off. A hidden item stays
reachable through Search and ⌘K, and through a More item at the bottom of the rail that
appears only while something is hidden; More lists the hidden items, each with Pin to
rail. Settings is where the rail is changed. Search is
not a rail item: a full-width search row under the wordmark (and ⌘K) opens
the one search modal, as built in SoilMetrix (`SearchModal`) and ported to appkit
(`SearchPalette`); on a phone it sits in the top bar (MVP-01). A product
renames items, inserts its own after Site, and sets persona presets; a person hides or
shows items. The landing page is configurable and defaults to full-page chat
(§12.2). The trail ("›", up to four prior states, each restoring item, mode, selection,
filters and time window; §14.7). One drawer per object with fixed sections (R19.1). The
attention bar on every screen (content in MVP-21). The "not available" page that
names a next step (H4). Phone breakpoint behaviour from MVP-20.

**Out of scope.** Site's modes (P4), Log's content (P3), Model (P6), walls (P4).

**Reuse.** appkit `NodeApp`, `DefaultApp`, `NavRail`, `navDefaults`, `navVocabulary`,
`Breadcrumbs`, `DetailDrawer`, `WorkbenchLayout`.

**Parity.** Page: the shell. CLI: `axi app rail` *(new)* prints the resolved rail for a
person. Chat: "what's on my rail" answers from the same skill. MCP: `app.rail`.

**Flags.** `site=soon`, `model=soon`, `log=soon` (the change log lives in Settings
until P3; decided 2026-10-30), `chart=on`, `library=on` (decided 2026-10-30), `history=on`, `search=on`, `chat=on`.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-00-A1 | The rail renders in the standard order with no section headers; a product rename and an inserted item appear in place | §12.1, §13.1 |
| MVP-00-A2 | Landing defaults to full-page chat; a persona preset and a person's override each change it | §12.2, §12.3 |
| MVP-00-A3 | A `soon` item shows its coming-soon page; an `off` item is absent from rail, search and ⌘K | flag contract |
| MVP-00-A4 | Any object opens in the drawer with the fixed sections; Open goes to its one home | R19.1 |
| MVP-00-A5 | A crumb restores item, selection, filters and time window in one click; browser Back does the same | H2 |
| MVP-00-A6 | Anything is reachable in ≤ 2 clicks from landing (rail + object, or ⌘K + pick) | H1 |
| MVP-00-A7 | A missing or forbidden page says why and offers one next step | H4 |

### MVP-12 Feature flags (on / off / soon)

**Scope.** As specified in the product's web app spec, Phase 1, which this item adopts
unchanged: registry from manifests, `default_by_env`, `protected_in`,
`AXIOM_FLAG_<KEY>`, overrides at node / account / user, audit on every write, the admin
section in Settings. Add the variant layer (roadmap §4) as a configuration source
between environment and node.

**Out of scope.** Role or percentage targeting (decided against).

**Reuse.** Axiom `flags`; appkit `FlagProvider`, `flags.ts`, `FlagsAdmin`.

**Parity.** Page: Settings › Flags (admins). CLI: `axi flags list|resolve|set`. Chat:
"turn Chart off for me". MCP: `flags.list`, `flags.resolve`, `flags.set`.

**Flags.** n/a.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-12-A1 | Precedence default → env → variant → node → account → user, with `protected_in` honoured | flag contract |
| MVP-12-A2 | A flag set on the CLI shows on the page and through MCP within one refresh | ADR-072 |
| MVP-12-A3 | Every write leaves an audit row naming who, scope, old and new value | MVP-13 |
| MVP-12-A4 | Each variant resolves its own flag set; an override in one does not reach another | VAR-A2 |

### MVP-20 Design tokens, themes, responsive baseline

**Scope.** Roadmap §6.3 items 1–5: the semantic token set, `ThemeChoice`, the CI
checks, the canonical components (except the signing card, P2.0, and the map, P4), and
the living component gallery, and the one Expand behaviour (`Expandable` /
`DisplayMode`, generalised from the as-built ⛶ on chart cards, chat charts' Expand
and the Charts page's wall-display mode): ⛶ top right of a chart, table, tile, group
or page section, shown on hover or focus and always on touch; one way out (Esc, ✕ in
the same corner, swipe down on touch); keyboard reachable; reduced motion respected.
Kiosk display URLs and auto-cycle come with walls in P4. Breakpoints `phone ≤ 900 px`, `tablet ≤ 1180 px`; at the
phone breakpoint the rail becomes a bottom tab bar (the role's first four items plus
More), the drawer a bottom sheet, the attention bar a top badge, tables become card
lists (spec addendum v11).

**Out of scope.** The console and wall themes' device behaviour (P4); final status
hues (OD-4 decides values; the token names do not wait).

**Reuse.** appkit `tokens.css`, `theme.ts`, `useTheme`, `gen_served_tokens.py`, the icon library (appkit `docs/design/site-view/icons/`, appkit #124),
`test_token_canon.py`, `frontend/preview/gallery`.

**Parity.** Page: Settings › Appearance; the gallery. CLI: `axi app tokens check`
*(new)*. Chat: n/a (reason: a visual system). MCP: `app.tokens_check`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-20-A1 | Every status colour ≥ 4.5:1 on its panel in each theme | R19.4 |
| MVP-20-A2 | Worst pairwise ΔE among ok / warn / bad ≥ 15 under normal, deuteranopia, protanopia | R19.4 |
| MVP-20-A3 | A brand accent within ΔE 20 of a status colour is refused at configuration with a reason | R19.4, MVP-09 |
| MVP-20-A4 | Status always shows glyph + word (✓ ? ✕ — ⏳), never colour alone | R19.3 |
| MVP-20-A5 | At 390, 800 and 1024 px the shell has no horizontal scroll and follows the phone rules | §18.2 |
| MVP-20-A6 | The gallery renders every canonical component in light, dark and console themes; snapshots in CI | R19.5 |
| MVP-20-A7 | appkit ships the icon library (the platform icon standard, spec addendum v15): one `Icon` component and SVG set, named by noun or state, `currentColor`, 16/20/24/32; the shell rail, search, ✦ Discuss, ⛶ Expand and + New render from it; the gallery shows the full sheet in light, dark and console | R19.5 |
| MVP-20-A8 | No product defines a second component for a canonical job (lint) | R19.5 |

### P0 rows outside the numbered items

| ID | Acceptance | Trace |
|---|---|---|
| VAR-A1 | `axi app run --variant axiom|product|site` starts the variant on its port and path; `--all` starts three under the supervisor | roadmap §4 |
| VAR-A2 | A session from one realm is refused by the other two | roadmap §4 |
| VAR-A3 | A Library upload in one variant is not retrievable by another's chat or search | roadmap §4 |
| VAR-A4 | Each repo's CI runs its variant's smoke (rail, flags, search, chat, Chart, Library) on web-app paths only | roadmap §4 |
| P0-DEV-A1 | `DeviceClass` and `DevicePolicy` exist (spec addendum v11); the browser class is the default at enrolment; every signed record has a `device_class` field | roadmap order change 2 |
| P0-ACC-A1 | `axi app acceptance --phase P1` lists every row with its tests and pass / fail / unproven | roadmap §5 |
| P0-CRE-A1 | The component gallery (MVP-20-A6) includes the page-header create action with its ▾ and the "Ask…" state, `EmptyState`, and the ✦ ⛶ pair, in light, dark and console themes | MVP-25 |

## P1 The MVP

**Entry:** P0 gate. **Size:** XL. **Work packages** (at most three at once, in this
order of dependency):

| Package | Items |
|---|---|
| P1.a Identity and tenancy | MVP-07, MVP-10, MVP-14, MVP-15, MVP-23 |
| P1.b Data and harness | MVP-11, MVP-18, MVP-19, MVP-22 |
| P1.c Answering | MVP-01, MVP-02, MVP-05, MVP-06 |
| P1.d Working surfaces | MVP-03, MVP-04, MVP-16, MVP-25 |
| P1.e Settings and records | MVP-08, MVP-13, MVP-21, MVP-09 |
| P1.f Parity and deploy | MVP-17; the staging deploy (below) |

P1.a and P1.b first (in parallel); then P1.c and P1.d; then P1.e; P1.f closes.

**Gate:** every P1 row proven by `axi app acceptance --phase P1`; the three variants
deployed on the staging node through the session that owns it; a one-day soak with no
alert unanswered.

### MVP-01 Search

**Scope.** One search over objects the person may see: pages, Library items, charts
and saved views, chat sessions, settings, people (by role, where allowed). ⌘K opens it
everywhere; results are chips that open the drawer. Scope-aware (MVP-15).

**Out of scope.** Log entries (P3), site systems (P4).

**Reuse.** Axiom `search`; appkit `NodeSearch`, `SearchPalette`.

**Parity.** Page: the search row under the wordmark and ⌘K open one search modal (the
as-built typeahead, grouped by name and in contents). CLI: `axi search query`. Chat: "find …" calls the
same skill. MCP: `search.query`.

**Flags.** `search=on`.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-01-A1 | A result the person may not see never appears, including its count | MVP-07 |
| MVP-01-A2 | A result opens its drawer; Open goes to its home | R19.1 |
| MVP-01-A3 | Results respect the scope selector | MVP-15 |
| MVP-01-A4 | The same query returns the same ids on page, CLI and MCP | ADR-072 |

### MVP-02 Chat

**Scope.** The as-built chat (PRD §21.3), as full-page chat (the default landing) and
the chat slide-out on other pages. On landing, one input sits front and centre with
action cards beneath it (the `next_cards` flag; it is off on some products today, and
this item turns it on for the site variant); once talking, the input moves to the bottom and the conversation scrolls up.
**Exactly one chat input per screen:** the slide-out is not offered on the full-page
chat, and any other "Ask" or **Discuss** control on a page opens that one chat with the
selection attached. Search stays separate (MVP-01). It
answers from gold and from RAG with citations, renders object references as chips,
suggests charts its data supports (2–3 types, switchable, "Save" to the catalog), shows
quality marks (MVP-18) and labels (MVP-22), and takes the current view as hidden
context. Model choice per site.

**Out of scope.** Voice (P7), guest chat (stays closed), agent delegation beyond the
existing tools.

**Reuse.** Axiom `chat` and the prompt pipeline; appkit `ChatDock`, `chat`, `ChatOverlay`.

**Parity.** Page: Chat. CLI: `axi chat`. Chat: itself. MCP: the tools chat calls are
the MCP tools (no chat-only tool).

**Flags.** `chat=on`, `next_cards=on`.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-02-A1 | A served value is shown with its unit, source, as-of and quality | R19.2 |
| MVP-02-A2 | An answer cites the gold object or Library item it used; a citation opens the drawer | §14.4 |
| MVP-02-A3 | A suggested chart saved from chat appears in Chart's catalog | MVP-03 |
| MVP-02-A4 | Chat never reads silver; a request that needs silver is answered from gold or refused with a reason | ADR-128 |
| MVP-02-A5 | Every tool chat can call is reachable as an MCP tool with the same result | MVP-17 |
| MVP-02-A6 | No screen renders more than one chat input; Discuss and every "Ask" box open the one chat | PRD §21.3 |

### MVP-03 Chart

**Scope.** The as-built Charts page, on appkit (PRD §21.2): groups of graphs (group
picker, Groups menu), the window selector ("Day · latest"), + Add graph, Share link,
the settings gear, Layout, wall-display mode, the per-graph Configure panel (site,
feeds, channels), Discuss (opens the one chat), ChartSpec 2.0 kinds (line, flame, 3D
scene, Trame, VERAView, gold, twins), saved and shared views, palettes, the figure lane, the catalog of sites, feeds and channels discovered from
gold (no hard-coded site lists), the resolved query folded under the chart, a table
view, marks as a legend row. A chart can be saved to the catalog by reference (the
basis for P4 chart slots and P2 published snapshots).

**Out of scope.** Chart slots in other views (P4), published snapshots (P2).

**Reuse.** appkit `charts`, `figure`; Axiom `charts`, `scidisplay`, the catalog API;
ChartSpec (one vocabulary, per the product's web app spec, Phase 2).

**Parity.** Page: Chart. CLI: `axi chart render|list|save` *(verify names)*. Chat:
draws and saves the same spec. MCP: `chart.render`, `chart.catalog`.

**Flags.** `chart=on`.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-03-A1 | A new feed registered with a connector and a channel map appears in the catalog with no code change | product web app spec Phase 3 |
| MVP-03-A2 | A saved chart has a stable catalog id, and the same id renders identically on page, CLI and MCP | ADR-072 |
| MVP-03-A3 | Measured and simulated counterparts share a hue; simulated is dashed and labelled | R19.4 |
| MVP-03-A4 | Held or stale spans draw as neutral hatch ("—"), never as a status colour | R19.4, MVP-18 |
| MVP-03-A5 | A reference group renders identically to its reference screenshots | product web app spec |

### MVP-04 Library

**Scope.** The rail item is always "Library" (Book is retired as a rail label; a
person's Book inside it follows domain and roles and only grows, ADR-239). The as-built
Dropbox-model browser (PRD §21.1): Home (Frequently used, New, Starred), Recent,
Starred, Shared with me, As uploaded, All files, Deleted files; the core DataTable
with a selection bar; copy and move; version history (restore makes a new version);
search by name and contents (typeahead grouped by name and contents); previews; the
classification tree (a product names it) as folders; labels (MVP-22); uploads
(MVP-16). No separate Library, Book, record-package or publications screen.

**Out of scope.** Generated record packages (a read-only generated folder, NEW, with
the product workflow); publications as files in a folder (NEW, P2; OD-5).

**Reuse.** Axiom `library`; appkit `FileLibrary`, `library`.

**Parity.** Page: Library. CLI: `axi library list|show|add`. Chat: "what do we have on
…". MCP: `library.list`, `library.show`.

**Flags.** `library=on`.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-04-A1 | Every item shows its labels and its retention class | MVP-22 |
| MVP-04-A2 | An item opens its drawer with a preview; Open goes to its page | R19.1 |
| MVP-04-A3 | Empty Library says what fills it and offers one act (Upload) | §14.8 |

### MVP-05 Chat history

**Scope.** The History item at the bottom of the rail: a person's sessions, searchable,
renamable, deletable within the retention class, resumable with their view context.

**Out of scope.** Sharing a session with another person (later).

**Reuse.** Axiom `chat` history store.

**Parity.** Page: History. CLI: `axi chat history`. Chat: "continue yesterday's
session". MCP: `chat.history`.

**Flags.** `history=on`.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-05-A1 | A person sees only their own sessions, per variant | VAR-A3 |
| MVP-05-A2 | Resuming a session restores its view context | H2 |
| MVP-05-A3 | Delete honours the retention floor and is recorded | MVP-22, MVP-13 |

### MVP-06 RAG

**Scope.** Per-tenant and per-principal corpora; hybrid retrieval with citations;
labels enforced at retrieval (an item the person may not see is never retrieved).

**Out of scope.** Cross-site federation of corpora.

**Reuse.** Axiom `rag` store and retrieval.

**Parity.** Page: citations in chat; Library "used by chat". CLI: `axi rag retrieve`.
Chat: grounding. MCP: `rag.retrieve`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-06-A1 | Retrieval never returns an item above the person's export-control clearance | MVP-22 |
| MVP-06-A2 | A golden question set's recall is reported per corpus at deploy | §6 PRD NFRs |
| MVP-06-A3 | Retrieval stays inside the variant's and tenant's corpora | VAR-A3 |

### MVP-07 Auth with RBAC and ABAC

**Scope.** Roles from the product's role packs (RBAC); attributes (ABAC) for site
scope, licences and qualifications, export-control clearance, retention role and
device class, all evaluated in one relationship graph. Every page, API, search result,
chat tool and MCP tool asks the same check. A key belongs to a person (ADR-205).

**Out of scope.** Time-boxed auditor grants (P7 uses the same mechanism).

**Reuse.** Axiom `auth`, `authz`, `oauth`; appkit `useGateUser`, `session.ts`.

**Parity.** Page: Settings › People (admins). CLI: `axi gate role`, `axi authz check`
*(verify)*. Chat: "can Ana see the Library?" (admins). MCP: `gate.role`, `authz.check`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-07-A1 | One decision function answers for page, API, search, chat and MCP; a test drives all five | ADR-072 |
| MVP-07-A2 | An attribute change (clearance, scope) takes effect on the next request without sign-out | MVP-14 |
| MVP-07-A3 | Every denial is audited with the rule that denied it | MVP-13 |

### MVP-08 Account, Settings and the change log

**Scope.** Two pages, split by whose they are (PRD §20.4; this settles the shape of
OD-6, which keeps only the section list open):

- **Account** is the signed-in person's own page, opened from the avatar menu. Everyone
  has it.
- **Settings** is the site's configuration, for admins. Work on people (invitations,
  roles, the roster, offboarding) lives in Team (MVP-23), not in Settings.

| Page | Section | Who | Contents |
|---|---|---|---|
| Account | Overview | everyone | what expires soon (keys, qualifications, grants) first |
| Account | Profile | everyone | name, photo, contact (verified channels only), time zone, public-site listing (opt-in, off) |
| Account | Sign-in | everyone | single sign-on, last fresh sign-in, active sessions, sign out others |
| Account | Keys | everyone | personal keys: create, label, expiry, rescope in place (same token), revoke (ADR-205) |
| Account | Notifications | everyone | channels, quiet hours, what counts as Needs you |
| Account | Preferences | everyone | theme (a station's device theme wins, P4), landing, rail items, default scope |
| Account | My agents | everyone | delegations and agents I am accountable for: pause, resume (P4 adds scope) |
| Account | Data | everyone | export what I own; my change log |
| Account | Devices, Qualifications | everyone | read only in P1 (enrolled devices, voice); full in P4 |
| Settings | Flags | admins | node and account overrides (MVP-12) |
| Settings | Data and retention | admins | site retention (lengthen only), export-control defaults (MVP-22) |
| Settings | Health | admins | MVP-19 |
| Settings | Change log | admins | every settings, flag, role, key and membership change, newest first; the audit trail beneath (MVP-13) |

**Out of scope.** Integrations and connectors pages (existing CLI), billing.

**Reuse.** Axiom `settings`, webgate account flows (profile update, sessions) and
`POST /gate/keys` (self-minted keys), `gate.rescope`, `principal` (verified contact
endpoints); appkit as built: `Settings` (`layout="tabs"` for Account, sidebar for site Settings), `settingsNav`, `NodeSettings`, `Profile`, `AccountMenu`, `Avatar`. Add sections to them; do not redesign them (spec-site-view addendum v13).

**Parity.** Pages: Account; Settings. CLI: `axi settings get|set|list`, `axi settings
changes` *(new)*, `axi gate rescope`. Chat: "set my landing to Chart", "which of my keys
expire soon". MCP: `settings.get|set|list`, `settings.changes`, `gate.rescope`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-08-A1 | Each section is present for the roles in the table and absent for others | MVP-07 |
| MVP-08-A2 | Every change appears in the change log with who, what, old → new, when | MVP-13 |
| MVP-08-A3 | A site retention value shorter than the platform floor is refused with the floor named | MVP-22 |
| MVP-08-A4 | Account holds nothing that changes the site; Settings holds nothing personal | PRD §20.4 |
| MVP-08-A5 | Rescoping a key from Account keeps the same token working | owner rule |

### MVP-09 Branding

**Scope.** A site's brand layer: name, logo, favicon, accent tokens only, the
"powered by" credit (subtle, configurable), per variant.

**Out of scope.** Public-site brand (P2 reuses this).

**Reuse.** appkit brand layer over `tokens.css`; Axiom brand validation.

**Parity.** Page: Settings › Appearance (admins: brand). CLI: `axi brand check`
*(new)*. Chat: n/a (reason: configuration in the site repo). MCP: `brand.check`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-09-A1 | The site variant shows the site's brand; the bare variants show Axiom's and the product's | VAR |
| MVP-09-A2 | A brand cannot set a status token (refused) | R19.4, MVP-20-A3 |

### MVP-10 Multiple site tenants

**Scope.** One node serves several sites as tenants: tenant ids, per-tenant app state,
tenancy per extension from the three-option menu (single, row-level `tenant_id`, or
schema-per-tenant; ADR-052 §D4), gold views that filter by tenant.

**Out of scope.** Tenant self-service signup.

**Reuse.** Axiom `sites`, tenancy helpers.

**Parity.** Page: the scope selector (MVP-15); Settings › Sites (admins). CLI: `axi
sites list|show`. Chat: "which sites can I see". MCP: `sites.list`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-10-A1 | A person with one tenant sees no other tenant's names, counts or objects anywhere | §6 PRD NFRs |
| MVP-10-A2 | Adding a tenant is configuration only (no code edit) | product web app spec |

### MVP-11 Data platform and AI harness

**Scope.** What the MVP's pages stand on: gold serving, the app's own relational
database (below), the catalog refreshed on every conform, the LLM serving path with a
model per site, the MCP server, and development lanes for tests. Health of each is
reported to MVP-19.

**The app database is MVP infrastructure** (spec-site-view, addendum "the app database
and gold"). Domain data reaches people from gold, read directly or published into the
app's own database; silver never serves. The MVP builds three things: the app database
itself (application state: accounts and profiles, settings, preferences, drafts,
layouts, pins and chart slots, notifications); the **publish-from-gold** step for what
the pages need fast (a projection, almost a cache); and its **freshness and
invalidation** (each published table records the gold version it came from, is
refreshed when gold changes, and shows "as of" when it lags).

**Out of scope.** New medallion features; the Site view's pipeline system (P4).

**Reuse.** Axiom `data_platform`, `llm_serving`, `mcp`, `lane`.

**Parity.** Page: Settings › Health (MVP-19). CLI: `axi data serving-report`, `axi
data freshness`. Chat: "is the data fresh?". MCP: `data.serving_report`, `data.freshness`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-11-A1 | No consumer path in the MVP reads silver (a test scans served queries) | ADR-128 |
| MVP-11-A2 | The catalog shows a new conformed feed within one conform cycle | MVP-03-A1 |
| MVP-11-A3 | The model chosen for a site answers chat in each variant | MVP-02 |
| MVP-11-A4 | Every table the app database receives from gold names its gold source and version; nothing in it is copied from silver | ADR-128 |
| MVP-11-A5 | A gold change reaches the app database within its declared freshness, or the page shows "as of" with the lag | §19.7 |
| MVP-11-A6 | Rebuilding the published tables from gold gives the same rows; application state is untouched by a rebuild | — |

### MVP-13 Audit trail under the change log

**Scope.** The hash-chained audit trail of every write the MVP makes (settings, flags,
roles, keys, invitations, uploads, deletes, denials), shown under the change log with
"explain" for a row and a chain verification.

**Out of scope.** The Audit mode and evidence packages (P3, P7).

**Reuse.** Axiom audit chain and explain.

**Parity.** Page: Settings › Change log › Audit. CLI: `axi audit list|show|chain`. Chat:
"who changed the landing page". MCP: `audit.list`, `audit.show`, `audit.chain`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-13-A1 | Every MVP write produces exactly one audit row | — |
| MVP-13-A2 | Chain verification passes, and fails on a tampered row (negative control) | — |

### MVP-14 Invitations, keys, SSO, the user lifecycle

**Scope.** Invite → redeem → active → suspended → removed; SSO through the site's
realm; personal keys; **no forced re-auth**: a migration, role change or variant
upgrade moves a person in place, server-side, with at most a one-click approval.
Intake requests (P2) become invitations here.

**Out of scope.** Identity-provider plug-ins beyond the site's SSO.

**Reuse.** Axiom `auth`, gate invite and redeem, keys, `directory`.

**Parity.** Page: Settings › People; the invitation page. CLI: `axi gate invite|redeem`,
`axi keys …`. Chat: "invite Ana as a researcher" (admins). MCP: `gate.invite`,
`gate.redeem`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-14-A1 | Upgrading the node keeps every session valid; nobody signs in again | owner rule |
| MVP-14-A2 | An invitation expires and is single-use; redeeming it twice is refused | — |
| MVP-14-A3 | Suspending a person ends their sessions and keys at once | MVP-07 |

### MVP-15 Site scope selector

**Scope.** One site or many (spec §6): a selector in the shell header for people with
more than one site, carried across items; every page and API respects it.

**Out of scope.** The Site view's roll-up across sites (P4).

**Reuse.** Axiom scope model; the selector pattern from the as-built app.

**Parity.** Page: header selector. CLI: `--site` on every scoped verb. Chat: "switch to
<site>". MCP: a `scope` argument on scoped tools.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-15-A1 | The selection carries across rail items and survives reload | H5 |
| MVP-15-A2 | People with one site never see the selector | §6.1 spec |

### MVP-16 Uploads into Library, feeding RAG

**Scope.** Upload (drag, button, chat attachment) → extract → labels proposed and
confirmed → Library item → RAG ingest. Progress in the upload tray.

**Out of scope.** Bulk import from external drives.

**Reuse.** Axiom `library` upload and extraction, `rag` ingest; appkit upload tray.

**Parity.** Page: Library and chat. CLI: `axi library add`. Chat: attach. MCP:
`library.add`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-16-A1 | An uploaded file is answerable in chat within one ingest cycle, with a citation | MVP-06 |
| MVP-16-A2 | An upload with no confirmed export-control label is not ingested | MVP-22 |

### MVP-17 MCP and CLI parity

**Scope.** Every MVP capability is on page, CLI, chat and MCP (or has a stated reason),
checked by a parity manifest test.

**Reuse.** The agent coverage manifest; the skills registry.

**Parity.** n/a (this item is parity).

| ID | Acceptance | Trace |
|---|---|---|
| MVP-17-A1 | The manifest lists each MVP skill with its four surfaces; the test fails on a gap without a reason | ADR-072 |
| MVP-17-A2 | An external MCP client gets the same answer as the page for a fixed fixture | — |

### MVP-18 Data-quality marks in Chart and Chat

**Scope.** Expectations → marks on gold (held, gap, stale, suspect, refused) shown on
charts (legend row, hatch, hover) and in chat answers (with the value).

**Out of scope.** Authoring expectations in the UI.

**Reuse.** Axiom data platform expectations and marks; appkit `FreshnessBar`, hover card.

**Parity.** Page: Chart, Chat. CLI: `axi data gaps`, `axi data freshness`. Chat: shows
marks. MCP: `data.gaps`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-18-A1 | A held span is drawn "—" in Chart and named in Chat's answer | R19.4 |
| MVP-18-A2 | The hover card shows quality on every value | R19.2 |

### MVP-19 Ops guardrails

**Scope.** Health (each service and the variants), backups validated daily, upgrade
and rollback by the switch runner (blue/green, ADR-182, ADR-197), and **disk-pressure
alerting**, measured as the margin in bytes to the kubelet's eviction line rather than
as percent used: the margin is free space minus the hard eviction floor, or minus the
floor plus the minimum reclaim while pressure is on. Warn at a margin under 150 GB,
page under 80 GB, critical when free space is under the floor plus 20 GB (thresholds
per site). A missing `imagePullPolicy: Never` image pages. Every volume is watched,
including the container runtime's data root, the database's and the bronze root's.
At critical, the node sheds non-essential writes (caches, previews) before essential
ones.

**Out of scope.** Cluster autoscaling.

**Reuse.** Axiom health, `data.backup_validate`, the switch runner, `notifications`,
`hygiene` (reclaim).

**Parity.** Page: Settings › Health. CLI: `axi status`, `axi data backup-validate`,
`axi hygiene stat disk`. Chat: "is the node healthy?", "is the disk OK?". MCP:
`hygiene.status`, `hygiene.stat` (`resource=disk`), `data.backup_validate`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-19-A1 | Filling a test volume past the warn, page and critical margins raises each rung in order, each notified | owner incident 2026-10-30 |
| MVP-19-A2 | An upgrade of the three variants refuses no request (zero-gap test) | ADR-182 |
| MVP-19-A3 | Rollback returns the previous slot in one command | ADR-197 |
| MVP-19-A4 | A backup restore into a throwaway database passes validation | — |
| MVP-19-A5 | At critical, the node sheds non-essential writes (caches, previews) before essential ones | owner incident 2026-10-30 |

### MVP-21 Notifications and a minimal "Needs you"

**Scope.** The attention bar's queue named "Needs you" (OD-2 for the product's frozen
panel), MVP sections only: Due (obligations such as key expiry, invitations to
approve, intake requests) and Held drafts. To sign and Co-sign arrive with P2.0 and P3.
Notifications by channel and quiet hours.

**Reuse.** Axiom `notifications`, obligations; appkit `ObligationCountdown`, `Notice`.

**Parity.** Page: attention bar. CLI: `axi notifications list`. Chat: "what needs me".
MCP: `notifications.list`.

**Flags.** `needs_you=on`.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-21-A1 | The queue shows only the person's own items; the count matches the list | §14.11 |
| MVP-21-A2 | The most frequent due task is ≤ 1 click from landing | H3 |

### MVP-22 Export-control and retention labels

**Scope.** Every Library item, upload, chat session and export carries an
export-control label and a retention class (system, operational ≥ 5 y, qualification,
lifetime, legal hold). A site may only lengthen retention. Deletions are recorded.
Labels gate retrieval (MVP-06), publication (P2) and export.

**Reuse.** Axiom classification boundary, retention classes.

**Parity.** Page: label chips; Settings › Data and retention. CLI: `axi library label`
*(new)*. Chat: "what's this file's label". MCP: `library.label`.

**Flags.** none.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-22-A1 | An item without a label cannot be exported or retrieved | §14.6 |
| MVP-22-A2 | Deleting before the retention floor is refused; after it, the deletion is recorded | §14.12 |

### MVP-23 Team: the basic directory of people and agents

**Scope.** A rail item, **Team** (label open: OD-11), listing everyone and everything
that can act or sign at the site in one table: people from the gate's accounts and the
directory, agents from the agent roster, nodes from the node registry. Filters: people,
agents, nodes, role. One actor drawer (name, `@name:context`, kind, roles, contact
buttons, for an agent its accountable person) that opens wherever a name appears.
Admins invite from Team (the existing invitation flow, MVP-14) and set roles
(`gate.role`). PRD §20, spec addendum v13.

**Out of scope (later phases).** Duty status, qualifications with expiry, the roster with
the qualification check, the "People and shifts" Site system (P4); signed acts on a profile
(P3); full agent profiles with scope, pause and ConCert (with the agent spine, P4);
the public team section (P2, opt-in).

**Reuse.** Axiom `webgate` (`gate.list`, `gate.invite`, `gate.role`), `directory`,
`agents.roster`, `principal` (verified contact), `vega.identity` handle parsing; appkit
`DataTable`, `DetailDrawer`, `Avatar`, and `control/AgentDrawer` for an agent. No new
store: the directory is a read across these.

**Parity.** Page: Team. CLI: `axi actors list|show` *(new; a read over the sources
above)*. Chat: "who can approve an ops request?", "who owns this agent?". MCP:
`actors.list`, `actors.show` *(new)*.

**Flags.** `team=on` (the site variant), `soon` in the bare variants until the label is
decided.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-23-A1 | People, agents and nodes appear in one list; each row names its kind in a word and a shape | PRD §20.1 |
| MVP-23-A2 | Every agent row names an accountable person; an agent without one is listed as a finding | PRD §20.1 |
| MVP-23-A3 | An actor's drawer opens from a name in chat, search and the change log | R19.1 |
| MVP-23-A4 | No actor data is written by the directory itself (a test asserts no new table) | spec v13 |
| MVP-23-A5 | A viewer sees only the fields their role may see (PRD §20.3 table) | MVP-07 |

### MVP-24 Schedule read: "what's on", without a calendar

**Scope.** The `ScheduledItem` read (PRD §22.3, spec addendum v14) over sources that
already exist in the platform: the scheduler (definitions, next fire, fire log), time
slots, attest obligations of a day or longer, vault expiry. A read-only **agenda** by
CLI, MCP and chat, and a "due soon" group (next 14 days) inside "Needs you" (MVP-21).
Site time zone from site configuration (OD-18), stated on every answer.

**Out of scope.** Any calendar component or page (P4); a product's calendar tab (P5);
drag, create, edit; ICS subscribe (P7). ICS **export** from the CLI is in scope because
it is a format of the same read.

**Reuse.** `schedule` api and time slots, `schedule/calendar/protocol.EventSpec` field
names, `attest.obligations`, `vault.audit`, the one decision function (MVP-07),
`notifications` for the due-soon queue.

**Parity.** Page: "Needs you" › Due soon (a list). CLI: `axi schedule agenda`,
`axi schedule item show`, `--format ics` *(new; noun open, OD-16)*. Chat: "what's on
this week?", "what is due before Friday?". MCP: `schedule.items_list`,
`schedule.items_show` *(new)*.

**Flags.** `schedule.agenda=on`.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-24-A1 | The agenda lists scheduler runs, obligations and expiries for a window, each with its source and a link to its owner | PRD §22.4 |
| MVP-24-A2 | Every time is in site time and labelled; a date-only expiry does not shift with the viewer's zone | CAL-A2 |
| MVP-24-A3 | A minute-scale obligation is absent from the agenda | CAL-A5 |
| MVP-24-A4 | Page, CLI, chat and MCP return the same items for a fixed fixture | MVP-17 |
| MVP-24-A5 | Nothing is written to any source by the read (a test asserts no writes) | CAL-A1 |

### MVP-25 Starting and creating: + New, empty states, first run, ✦ Discuss

**Scope.** Every MVP page is a starting point for its main action. Four canonical
pieces, built inside the as-built flows (Charts' + Add graph and Groups › New group,
the Library's Upload, New folder and upload tray, chat's action cards, `gate invite`,
the data kit's `kit-init` → `kit-up` → `kit-try` → `kit-check`):

1. **+ New, named by its noun.** One primary create action per page, the last item on
   the right of the page header ("+ Add graph", "+ Upload", "+ Invite"). A ▾ beside it
   holds templates, starters and Import from…. The same action is a search command
   (typing "new") and a chat action card ("make me…"); all three open the owning
   feature's own form, prefilled. A role that cannot create sees "Ask…", naming who can;
   the ask lands in their "Needs you". A product may keep a frozen flow's own label and
   omit the ▾.
2. **EmptyState** (the Library's `EmptyDropzone`, generalised): a one-sentence honest
   statement of what would be here; one large next action; two or three starters (a
   template, a sample labelled "sample", "ask Chat to set it up"); Import from… where it
   applies. A sample is never served, charted or retrieved as data.
3. **First run.** A new node with no data, people or models opens on Chat with a
   five-step checklist in the composer's `extras` slot: connect data, invite people,
   set branding, pick a landing page, first chart from chat. Its states are read from
   the `sites.checklist` skill; each step is also an action card and a search command;
   it can be hidden, and it disappears when all five are done.
4. **✦ Discuss.** The Charts Discuss pill, generalised to an icon (tooltip "Discuss with
   the assistant"), paired with Expand in a fixed order (✦ then ⛶) in the same corner, on
   region, card and chart headers, a selected row, and an empty state. It opens the one
   chat as a slide-out (a bottom sheet on a phone) with the context attached as
   removable chips, a likely question as editable ghost text, and action cards for that
   area. It is absent on the full chat page and on display (kiosk) views.

**Reuse.** appkit `GroupSection` (+ Add graph), `LibraryPage`/`UploadModal`/`UploadTray`,
`EmptyDropzone`, `NextCards` and `POST /api/v1/next`, the chat `extras` slot, the
Charts Discuss slide-out; Axiom `webgate` invite, `data_platform` kit, `sites.checklist`.

**Parity.** Page: the header action, empty states, the checklist, ✦. CLI: the existing
create verbs (`axi gate invite`, `axi data kit-init`, `axi library upload`). Chat:
"make me…", "set up the rest for me". MCP: the same tools as the CLI verbs.

**Flags.** `create_menu=on`, `first_run=on`, `discuss_icon=on`.

| ID | Acceptance | Trace |
|---|---|---|
| MVP-25-A1 | Every MVP page (Chat excepted) has exactly one primary create action, last on the right of its header, named by its noun | design §13.1 |
| MVP-25-A2 | Typing "new" in search lists every create action the viewer's role allows, and none it does not | design §13.1 |
| MVP-25-A3 | A viewer without the create permission sees "Ask…" naming the role that can; sending it creates a "Needs you" item for that role | MVP-07, MVP-21 |
| MVP-25-A4 | Every MVP page and view renders an EmptyState on an empty fixture: a statement, one primary action, 2–3 starters; no chart or table draws fixture values as data | design §13.1 |
| MVP-25-A5 | A sample item is labelled "sample" wherever it appears and is excluded from retrieval, charts and exports (test) | MVP-06, MVP-22 |
| MVP-25-A6 | On a fresh node the checklist shows five steps with states from `sites.checklist`; completing a step updates it without reload; all done removes it | design §13.1 |
| MVP-25-A7 | ✦ opens the one chat (never a second input) with removable context chips, ghost text that Tab accepts, and 2–4 action cards for the area | MVP-02 |
| MVP-25-A8 | Context attached by ✦ comes only through gold and the app database with the page's permissions; an item the viewer may not see is shown as withheld, never sent | MVP-07, MVP-11 |
| MVP-25-A9 | An action card that writes goes through the one signing or confirm step; no write happens from ✦ without it | MVP-13 |
| MVP-25-A10 | ✦ and ⛶ never overlap data or status at 390, 800 and 1024 px; ✦ is absent on the full chat page and on display views | MVP-20-A5 |

## The staging deploy (P1.f)

1. Release Axiom and appkit, then the product, then the site (by tag; the tag is the
   deploy).
2. Announce on the staging node's owning session; deploy the three variants with
   `axi app run --all` under the supervisor, blue/green.
3. Verify: every VAR row and the per-variant smoke on the node; the health page green;
   the disk alerts fire on a test volume.
4. Soak one day. **Rollback:** the switch runner's rollback to the previous slots;
   pins move back only by a new release (pins move forward).

The site's deployment specifics (node, ports, realms, brand) live in the site repo.
