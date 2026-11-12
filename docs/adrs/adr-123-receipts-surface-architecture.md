# ADR-123: Receipts Surface Architecture — one token canon, one appkit shell, one auth seam

**Status:** Accepted (2026-09-21 — founder acceptance same day; D2 reworded
same day per founder direction: the posture is the appkit shell with
configurable chrome, not "a SPA")

## Context

The Receipts program (feature 0: the fleet console web UI) is the first
platform-owned product surface built on appkit. The substrate survey
(2026-09-21) found the ground unsettled in three ways that would each
become permanent by accident the moment code lands:

1. **Two design-token vocabularies**: appkit's `--ground/--ink/--accent`
   family (377-line tokens.css, light-default) and the platform's
   `axiom-design-tokens` package (`--theme-*` + aliases, dark-default,
   air-gap-guaranteed, CI drift check, pytest invariants). Both define
   `--accent`; loading both makes stylesheet order silently decide the
   brand color. appkit's served copy (`/_appkit/tokens.css`) is a stale
   86-line truncation missing five components' styles.
2. **Three frontend postures**: appkit as SPA component library; webgate
   as a node-served React SPA with brand injection (the only shipped,
   working exemplar); a webapp README mandating a split-deployed MPA
   "deliberately not a SPA" — for a directory containing only that
   README.
3. **An unresolved auth seam**: a browser behind webgate holds a cookie
   session while `/api/v1` routes authenticate per-route (fleet reads
   narrow by bearer identity; ingest requires bearer).

## Decision

1. **`axiom-design-tokens` is the single token canon.** It is the
   mature artifact (drift-checked, air-gapped, tested, dark-default with
   the documented light-last cascade). appkit consumes it: appkit's
   token names become a thin compatibility mapping onto `--theme-*`
   variables, its hardcoded accent goes token-driven, and the served
   copy is generated from the canonical file with an equality test so a
   truncated copy can never ship again. Component classes (`.axk-*`)
   stay in appkit; token *values* live in one place.
2. **Platform product surfaces are appkit-shell applications served
   the webgate way.** The surface IS the appkit framework — AppShell +
   NavRail + WorkbenchLayout — with chrome as *configurable options*:
   a full product surface runs with the rail and shell on; a stripped
   single-view presentation (an embed, a kiosk board, a screenshot
   page) is the same app with those options deselected — a
   configuration, never a separate architecture, and "SPA" is not the
   identity of anything. Serving mechanics follow the shipped webgate
   exemplar: built with appkit components, Vite `base:"/<surface>/"`,
   served from the node behind the gate with brand injection,
   server-rendered fallback optional. The webapp `frontend/` README's
   MPA mandate is retired for platform surfaces in the same PR that
   lands the first one (specs move with behavior). Split-deploy
   remains valid for consumer-hosted apps; it is no longer the
   platform-surface rule.
3. **The auth seam closes on the read side**: the `/api/v1` routes a
   browser surface consumes accept the webgate cookie session via the
   existing `chain_resolvers(session, bearer)` seam, applied per-route.
   Ingest and every write remain bearer-only. Site scoping continues to
   derive from the resolved credential, never the client.
4. **Net-new UI components are born in appkit**, not in the consuming
   surface — status pill, evidence row, stat tile, node card land as
   appkit exports with `Extraction-Exempt` provenance headers (they are
   new, not extractions), so the second surface reuses instead of
   reinventing. The Receipts app itself lives in the platform tree and
   consumes appkit as a build dependency (file/workspace dependency
   until the npm publish lands).

## Consequences

- Dark-default becomes the platform surface default (appkit's
  light-default yields); tenant brands override via tokens, as the
  token package already documents.
- appkit's open branches must land in a safe order: the npm-publish
  branch adds components; two later branches DELETE shipped components
  (−1,904 lines). This ADR does not adjudicate those merges but records
  the hazard: a deletion branch merged after main silently destroys
  shipped work — the appkit owner must sequence explicitly.
- The webgate cookie becomes a load-bearing platform contract (it
  already was in practice; now it is named).
- Two hand-synced token copies become zero; four hardcoded palettes in
  the tree (http static page, classroom webui, webgate inline shell)
  become migration debt with a named owner (TIDY sweep item).

## References

Substrate survey 2026-09-21 (session record); `packages/axiom-design-tokens/README.md`;
`webgate/api/routers.py` (the posture exemplar); appkit
`docs/soilmetrix-appkit-migration.md`; Receipts program plan
(`docs/working/receipts-program-plan-2026-09-21.md`).
