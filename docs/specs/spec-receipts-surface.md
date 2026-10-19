# Tech Spec: Receipts Surface (feature 0)

**Status:** Living. **PRD:** `prd-receipts-surface.md`. **ADR:** ADR-123.

## 1. Shape

Two build sites, per ADR-123 D4:

- **appkit** (`axiom-appkit/frontend/src/`): net-new exported
  components, each with `// Extraction-Exempt: net-new for the Receipts
  surface` provenance headers and vitest coverage —
  `StatusPill` (five states; color + icon + label, never color alone),
  `EvidenceRow` (kind, pill, evidence string face-up, received-at,
  signature qualifier), `StatTile` (label + value + sublabel),
  `NodeCard` (identity strip + rollup pill + kind rows + freshness
  bars), `FreshnessBar` (age vs 3× cadence, quiet until >60%).
- **platform** (`src/axiom/extensions/builtins/receipts/`): a
  purpose-named extension (AEOS/ADR-031 self-contained) owning the
  mount and the app. `webui/` is the Vite + React + TS app,
  `base:"/receipts/"`; `mount.py` serves `webui/dist` gate-fronted.
  Composition only; no component logic lives here. appkit components
  arrive via the appkit npm package once its publish branch lands —
  the dependency is deliberately ABSENT until the package exists (a
  dep that cannot install is a declared surface that doesn't exist).
  **One-UI-infra rule (founder, 2026-09-23):** this app, the NOS
  webapp, and the Aiterra/Field Hand extraction ride the same appkit
  substrate — components from appkit, tokens from the `--theme-*`
  canon via `/_appkit/tokens.css`, serving via the mount pattern. No
  surface forks its own substrate.

## 1b. Views (per PRD R8/R9)

- **Change feed (default)**: state transitions derived client-side by
  diffing consecutive polls (kind moved GREEN→UNPROVEN etc.), each
  entry a full EvidenceRow. Derivation is presentation-only — statuses
  themselves remain server-verbatim (R3 intact); an explicit "as of
  <poll time>" stamp on every entry.
- **Board**: the node-card grid, one click away.
- **Empty/amber microcopy**: a small copy map per (kind, status) ships
  with the surface — e.g. UNPROVEN service_health → "healthy was
  claimed without a measured latency; green requires latency_ms."
  Copy strings live beside the components, tested for presence in the
  can-fail suite.
- **Typed-decision qualifiers (ADR-126)**: when a row's verdict
  carries `stated_confidence` and/or a seat reliability record, they
  render as EvidenceRow qualifiers (the component's existing
  free-form qualifier slot — additive, no rework): e.g.
  `confidence: 0.91` · `seat reliability: 84% at stated 90% (n=412,
  30d)`. A deterministic verdict renders no confidence qualifier, and
  that absence is correct, not missing. Confidence never colors the
  pill — status comes from evidence alone.

## 1c. The frame (re-homed 2026-09-23, founder ordering)

The surface runs INSIDE appkit's `DefaultApp` — the shipped
ChatGPT-like frame (rail verbs via `composeVerbs`, chat landing,
FileLibrary reference, Search affordance). Receipts registers as a
view under its own added verb (`verbs.add receipts` +
`views.receipts`); Feed | Board becomes an in-view toggle (page-level
routing belongs to the frame). The chat verb renders DefaultApp's
honest no-engine state until the node's chat engine is wired — a
separate, deliberate step. Non-green rows carry next-action lines
(PRD R13) through EvidenceRow's `nextAction` slot: copyable commands
now, gated-verb affordances with the K2 approval doctrine.

## 1d. The in-harness projection (R16)
The brief and receipt answers are SERVER-COMPOSED, byte-stable
renderings (the serving face's cache-aware pattern): one renderer
feeds CLI, web, and the MCP courier verbs, so a harness agent
relaying `axiom_today` output cannot drift from what the web shows.
The MCP verbs are read-only projections plus `axiom_direct` (a
gated write: focus directives ride the ADR-114 approval floor).
Attention budget (R14) is enforced in the renderer, not the client.
Triage-seat (R15) events (hold/approve/dismiss) are TypedDecision
outcomes appended to the seat's verdict log.

### 1d.1 The web projection (landed)

`GET /api/v1/receipts/today` serves `brief_payload(brief)` — the ONE
structured shape the today-skill result and the MCP structured form
also carry. Read-only by construction: the route composes with
`snapshot=False`, so browsing never advances the trust-delta baseline;
only the deliberate surfaces (CLI `axi receipts today`, the MCP
courier) consume deltas. Site scoping mirrors fleet reads (credential
narrows, out-of-scope 404s). Internal source ids are mapped to plain
display names server-side (`PLAIN_SOURCE_NAMES`) before the wire — the
terminology ledger's rule enforced at the source. The web views are
appkit's `BriefToday` (home: the daily thread opens with the brief)
and `BriefDocket` (the Decide verb; feed + board ride behind it as
forensics tabs), both rendering the payload verbatim.

## 2. Data flow

`GET /api/v1/fleet/status` + `GET /api/v1/receipts/today` every 45s
(jittered) → render verbatim. The
client holds NO status logic: statuses, evidence, rollups come from the
server evaluator (fleet/status.py). A fetch failure renders an explicit
"console unreachable since <t>" banner and greys the board — no
last-known-fresh presentation (R3).

## 3. Serving + auth

Appkit-shell app served the webgate way (ADR-123 D2): the built
`dist/` serves under `/receipts/` from the composed app behind the
gate (`MountSpec(requires_authz=True)`), brand-injected like webgate's
shipped surface. The shared mechanics live in `http/spa.py`
(`inject_bootstrap_global` — the `window.__AXIOM_BRAND__` bootstrap,
`<`-neutralized; `safe_asset_path` — containment-checked assets),
extracted from webgate when this surface became their second consumer;
webgate delegates to the same module. Without a build the mount serves
an honest named-build-step placeholder, never a staged UI. The app runs
inside AppShell + NavRail with chrome as configurable options —
a chrome-less single-view presentation (embed/kiosk/screenshot page)
is the same app with those options deselected, never a second
architecture. Reads: the fleet status route gains the
`chain_resolvers(session, bearer)` per-route dependency (ADR-123 D3) —
cookie session for browsers, bearer unchanged for CLI/agents; scope
derivation from resolved credential unchanged (404-not-403). Ingest
untouched (bearer-only).

## 4. Token pipeline

`axiom-design-tokens/tokens.css` is the only palette source (ADR-123
D1). appkit gains `compat-tokens.css` mapping `--ground/--ink/--accent…`
→ `var(--theme-*)`; the Python-served copy becomes build-generated with
an equality test (`test_served_tokens_match_source`) so truncation
cannot recur. The Receipts app imports tokens + compat + appkit CSS in
that documented order (light-last rule preserved).

## 5. Honest-rendering tests (the can-fail suite)

Vitest, against fixture JSON of every state:
- UNPROVEN and FAILED render distinguishably with color forced off
  (icon+label assertions) — the R1 invariant, provably failable.
- A row with `signature_state:"unverified"` shows the qualifier (R4).
- Fetch-failure fixture renders the unreachable banner and no rows.
- A GREEN row asserts its evidence text is in the DOM (receipts
  face-up), and the healthy-control fixture renders all-green — the
  suite can pass as well as fail.
Playwright smoke (1): the real `/receipts/` page over a seeded local
store renders both nodes. Screenshot artifacts from this smoke are
CI-internal only (R7).

## 6. Reuse ledger (program rule 3)

| Reused | New |
|---|---|
| fleet API + evaluator + tenancy (untouched) | 5 appkit components |
| webgate serving/brand-injection pattern | receipts app shell (composition) |
| axiom-design-tokens + cascade rules | compat-tokens mapping |
| chain_resolvers/session resolver | one per-route dependency wiring |
| appkit AppShell/WorkbenchLayout/DetailDrawer | — |

## 7. Phase exits (program template)

- **P1 (this doc + mock):** ADR accepted by founder; component list
  frozen; appkit branch-order hazard acknowledged by appkit owner.
- **P2 (spine):** session-resolver wiring + token pipeline + equality
  test green; appkit components merged with provenance headers.
- **P3 (surface):** board over the real fleet; can-fail suite green;
  Playwright smoke green.
- **P4:** first UI screenshot post drafted from production; exit
  review consults the kill question (does the board change operator
  behavior — is anyone looking at it besides us?).
