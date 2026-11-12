# The web app design contract

**Status:** Binding for every change to an appkit-based web app, its pages, its shell,
and the model-loop workflow. **Owner:** platform (web). **Last updated:** 2026-10-30.

This folder is the entry point an agent or a person reads **before building any
screen, page, workflow or model-loop feature**. The design was settled with the owner
over sixteen review rounds; this file turns it into rules that can be checked. A rule
here wins over an older document that says otherwise. Where a rule and a product's
**frozen design** disagree inside that frozen area, the frozen design wins and the
conflict goes to the owner (rule DC-14).

## What the design is for

One web app on appkit, composed three ways (bare Axiom, a domain product, a site's
configuration), in which a person can find anything in two clicks, read every value
with its unit, source, as-of and quality, sign what they are responsible for, and
improve the models that run the site, all from one shell with one chat. The full
statement is the Site view PRD.

## Where the design lives

| What | Where |
|---|---|
| The shared design (interactive, all sections, the gallery and the twin-loop page) | https://claude.ai/artifact/4Wtqj1voZMCdw56C24HHLv (private; the owner shares it) |
| The design copy of the generic sections (README, index and icons) | appkit [`docs/design/site-view/`](https://github.com/b-tree-labs/axiom-appkit/tree/main/docs/design/site-view) (appkit #124); screens are not committed anywhere, they are linked from the shared design's Site section (https://claude.ai/artifact/4Wtqj1voZMCdw56C24HHLv) |
| The product requirements | [prd-site-view.md](../prds/prd-site-view.md), [prd-model-loop.md](../prds/prd-model-loop.md) |
| The spec | [spec-site-view.md](../specs/spec-site-view.md) |
| The build-out roadmap and the phase hand-offs | [spec-webapp-buildout-roadmap.md](../specs/spec-webapp-buildout-roadmap.md); P0 and P1: [spec-webapp-buildout-mvp.md](../specs/spec-webapp-buildout-mvp.md); P2 to P7: `spec-webapp-buildout-p<N>-*.md` beside it |
| The decisions | ADR-223 to ADR-229 in [../adrs/](../adrs/) |
| What already exists, and what to extend | [reuse-catalog.md](reuse-catalog.md) |

A domain product and a site repository each keep their own `docs/design/README.md`
that **adds** rules for their layer and links back here. They never restate or
loosen a rule in this file.

## How to use this file

1. Find the phase you are building in the roadmap, and open its hand-off spec. Build
   only the acceptance rows it lists.
2. For every element you touch, look it up in the [reuse catalog](reuse-catalog.md)
   and extend the component it names. If the catalog says "missing", build it once,
   in the layer the catalog names, and add the row.
3. Apply every rule below. Each rule says how it is checked: a **CI check** fails the
   build, an **acceptance row** is proven by a test carrying its ID
   (`@pytest.mark.acceptance("MVP-02-A6")` or `it("MVP-02-A6 …")`, roadmap §5), and a
   **review item** is a box in the pull-request checklist.
4. In the pull request, cite the design section (for example "design §chart, PRD
   §21.2") and every acceptance ID you implement, name the as-built component you
   reused, and attach screenshots beside the matching mockup.

## The rules

Each rule is a MUST. The trace column names where the rule was decided and the rows
that prove it.

| ID | Rule | Trace | Checked by |
|---|---|---|---|
| DC-01 | **As-built first.** Start from the screen or component that is on `main` today, add only inside it, and mark each addition NEW. Never redesign a working component (Library, Charts, chat charts, DataTable, chat). New needs become folders, views, kinds or sections inside the existing component. One component per repeated job; a product never defines a second component for a canonical job. | PRD §16.1, §19.5, §21; MVP-20-A8 | review item; reuse catalog; MVP-20-A8 lint (P0) |
| DC-02 | **One shell.** Every page renders inside appkit's shell (`NodeApp` / `DefaultApp`, `NavRail`, `Breadcrumbs`, `DetailDrawer`). No page builds its own rail, top bar or frame. Design mockups use the design's one shared shell snippet (`shell.js` in the design bundle), never a hand-drawn rail. | MVP-00-A1, MVP-20-A5, MVP-20-A6; ADR-223 | acceptance rows; review item (screenshots beside the mockup) |
| DC-03 | **The standard rail, in order, with no section headers:** Chat · Site · (the product's own items) · [Model] · [Chart] · Log · Library · Team · History at the bottom. Model and Chart are shown by user type or role, as the product decides; Team is universal: on every rail by default, and a person or site may switch it off. A hidden item stays reachable through Search and ⌘K, and through a More item at the bottom of the rail that appears only while something is hidden. More lists the hidden items, each with Pin to rail. Settings is where the rail is changed (D1, D11). A product may rename Site and Model, insert its items after Site, and hide items by persona. It never renames Library, never reorders the standard items, and never adds section headers. Audit is a mode of Log, not a rail item. The landing defaults to full-page chat. | ADR-223; MVP-00-A1, MVP-00-A2, MVP-00-A3 | acceptance rows |
| DC-04 | **Retired names stay retired.** "Steer" is never a current name (the behaviour lives at Site › Automate; Control is its route `#/control` and its ⌘K name, decision D27). "Book" is never a rail label; the rail item is **Library** (a product's classification tree may still be called its book, inside Library). | PRD §21.4; ADR-223 | CI check in appkit (`designContract.test.ts`); review item |
| DC-05 | **Search is a full-width row under the wordmark plus ⌘K** (on a phone, a magnifier in the top bar), opening the one search modal (appkit `SearchPalette`, the as-built typeahead grouped by name and by contents). Search is not a rail item. Results are chips that open the object's drawer. | PRD §21; MVP-01-A1 to MVP-01-A4 | acceptance rows |
| DC-06 | **Exactly one chat input per screen.** The full-page chat and the chat slide-out are the same chat; the slide-out is not offered on the full-page chat. Every "Ask" box and every ✦ Discuss on a page opens that one chat with the selection attached. On landing the input is centred with action cards beneath; once talking, it drops to the bottom. | PRD §21.3; MVP-02-A6, MVP-25-A7 | CI check in appkit (textarea ratchet); acceptance rows |
| DC-07 | **"+ New", named by its noun,** is the one primary create action on every page except Chat, last on the right of the page header ("+ Add graph", "+ Upload", "+ Invite"), with a ▾ for templates and Import from…. The same action is a search command ("new") and a chat action card, and all three open the owning feature's own form. A role that cannot create sees "Ask…", naming who can. | MVP-25-A1, MVP-25-A2, MVP-25-A3 | acceptance rows; review item |
| DC-08 | **Empty states are launch pads:** one honest sentence saying what would be here, one large next action, two or three starters (a template, a sample labelled "sample", "ask Chat to set it up"). A sample is never served, charted, retrieved or exported as data. | MVP-25-A4, MVP-25-A5, MVP-04-A3; PRD §14.8 | acceptance rows |
| DC-09 | **✦ Discuss, then ⛶ Expand,** in that order in the same top-right corner of a region, card, chart, table or section header. ✦ opens the one chat as a slide-out with the context as removable chips, editable ghost text and two to four action cards; it is absent on the full chat page and on display views. ⛶ fills the screen (`Expandable` / `DisplayMode`), and **Esc**, or ✕ in the same corner, is the one way out. | PRD §21 "Expand and displays"; MVP-25-A7, MVP-25-A10; MVP-20 scope | acceptance rows; review item |
| DC-10 | **One signing card** (appkit) for every signature: it shows exactly what will be signed, never folded; its verb names the act (Sign, Sign receipt, Sign approval, Certify, Co-sign, Seal); it always offers **Sign · Hold · Ask** with the keys **A · H · ?**. A product may add read-back text; it may not fork the card. An MCP or CLI call may draft a signature; only a person signs. No write from ✦ or an action card happens without the one signing or confirm step. | ADR-227; PRD §19.3; P3-A05, P5-A04, MVP-25-A9 | acceptance rows; MVP-20-A8 lint (P0) |
| DC-11 | **Status is a glyph and a word, never colour alone:** ✓ good · ? waiting or unproven · ✕ missed or failed · — absent or stale, plus ⏳ where a person must act. Status colours come from the semantic tokens; a brand sets the accent only and can never set a status token; the accent is the one interactive colour, and a brand's identity colour is never interactive or a status; controls are rounded, with radius, type and shadow from appkit's design tokens, never square; an accent within ΔE 20 of a status colour is refused. Measured and simulated counterparts share a hue, simulated dashed and labelled; held or stale data is a neutral hatch. | ADR-226; PRD §19.3, §19.4; MVP-20-A1 to MVP-20-A4, MVP-09-A2, MVP-03-A3, MVP-03-A4 | acceptance rows; appkit token test (P0) |
| DC-12 | **Serving reads gold or the app database; silver never serves.** Domain data reaches a page, chat, a chart, MCP or an export from a gold object, read directly or published into the app's own database (which also holds application state). Nothing in the app database is copied from silver. A request that needs silver is answered from gold or refused with a reason, and the fix is a new gold object. A record lives with its writer and reaches gold one way; a page reads its own tables, and reads gold only read-only through the gold door, showing "as of" (ADR-241). | ADR-128; ADR-241; PRD §19.7; MVP-02-A4, MVP-11-A1, MVP-11-A4, MVP-11-A6 | acceptance rows (MVP-11-A1 scans served queries); review item |
| DC-13 | **Layering.** A capability that survives removing the domain noun goes in Axiom (UI pieces in appkit); domain content goes in the domain product; a site repository holds configuration only (brand, channel maps, constants, calibration, its overlay of flags). Apply the three questions in `AGENTS.md` ("Which Layer Does This Belong To?") before writing the module. A product composes over appkit's defaults (`composeVerbs`, flags, tokens); it never copies them. | AGENTS.md; roadmap §3, §4; VAR-A1 to VAR-A4 | review item; `tests/test_axiom_names_no_domain.py`; public-mirror guard |
| DC-14 | **Frozen designs win inside their areas.** A product may declare a design frozen (the reference product freezes its experiment workflow item and its control room). Inside that area the frozen screens are the source; a change that would alter one is raised to the owner as a flag, never applied silently. | PRD §19 preamble; roadmap §7; P5-A01 to P5-A04 | review item |
| DC-15 | **One object, one drawer, one home.** Selecting an object anywhere opens its drawer with the fixed sections (verdict, key facts, disclosures, actions, linked chips, the footer with provenance and Discuss). Open goes to its one home page. Chat renders object references as the same chips. | PRD §19.1; MVP-00-A4, MVP-01-A2, MVP-04-A2 | acceptance rows |
| DC-16 | **Four surfaces, one skill.** Every act on a page is also a CLI verb, a chat ability and an MCP tool backed by the same skill, in the same pull request or the next with a stated reason. | ADR-072; MVP-17; MVP-01-A4, MVP-02-A5 | acceptance rows; review item |

## Checks that run in CI, and what stays a review item

| Check | Where | What it fails on | Why it is cheap and reliable |
|---|---|---|---|
| `tests/test_design_contract.py` | this repo | a rule above without an ID or a trace; a cited `MVP-NN-An`, `Pn-An` or ADR that does not exist; a missing `AGENTS.md` pointer to this file; a local path in the in-repo design copy | reads a few Markdown and HTML files |
| `scripts/check_design_assets.py` (run by the test above) | this repo | a design asset over 400 KB, any file over 2 MB, the folder over its budget (12 MB here), a `file://`, `/Users/` or `/private/` path | file sizes and one text scan |
| `frontend/src/designContract.test.ts` | appkit | a rail label "Steer" anywhere; "Book" as a rail label beyond the one recorded debt; a new file rendering a `<textarea>` that is not on the reviewed allowlist (a second chat input) | pure imports and a file scan; known debt is listed by name, so the check only ratchets |
| Public-mirror guard and domain-names guard | this repo | domain or institution names in the public surface or the substrate | existing |

**Review items only** (no reliable cheap test today): DC-01 as-built reuse, DC-02 the
screenshot comparison, DC-07 "+ New" on every page (there is no page registry to scan
yet; MVP-25-A1 becomes a test in P1), DC-08 and DC-09 placement, DC-13 layering
judgement, DC-14 frozen areas. The pull-request template carries each one.

## Superseded design folders

- `docs/assets/design/steer/` is **superseded**: Steer is retired and its behaviour
  is Site › Automate (ADR-224). It is kept as history; never build from it.

## Known debt on main (recorded, not yet fixed)

- appkit's default verbs read Control · Chart · Book · Log (`navDefaults.ts`), which
  predates ADR-223. P0.b (MVP-00) moves them to the standard rail; the appkit check
  lists "Book" as the one allowed debt until then.
- appkit's Charts page carries its own question box (`charts/Ask.tsx`), a second chat
  input. MVP-02-A6 routes it to the one chat; the appkit check lists it as debt.

## Changing this contract

Change a rule only with the owner's decision recorded (an ADR or a dated PRD
section), in a pull request that updates the trace and the checks together.
`tests/test_design_contract.py` must stay green.
