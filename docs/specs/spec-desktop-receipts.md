# Tech Spec: Desktop Receipts (feature 4)

**Status:** Living. **PRD:** `prd-desktop-receipts.md`.

## 1. Shape — one core, per-OS shells

- Core (`desktop/` builtin): `state.py` writes a single glyph-state
  file (`~/.axi/desktop/state.json`: worst-status, burn fraction,
  feed-unread count) from local receipts + fleet cache, on a timer —
  the ONLY thing shells read. `sudo_journal.py` (linux): arms on the
  Omarchy sudo-window toggle (watch the sudoers.d drop-in their mode
  writes), records via process-accounting + auditd-lite shim,
  attributes by ancestry to the launcher/harness, closes with a
  ledger receipt + `/etc` diff snapshot. Opt-in prompt at first
  window-open; local-only.
- Shells:
  - `shells/waybar/` — a `custom/axiom` module (exec reads
    state.json, emits waybar JSON w/ class per status); click execs
    `xdg-open <receipts url>`. Config snippet installed by the
    plugin; theme colors come from the bridge, not the module.
  - `shells/macos/` — a menu bar item (SwiftBar/xbar plugin script at
    launch — zero-install-friction, revisit native later); reads the
    same state.json; click opens the surface.
- `omarchy-axiom/` (separate small repo for the marketplace):
  manifest.json + install script = AUR/axiom-os install + waybar
  snippet + theme-bridge run + optional fleet enroll + sudo-journal
  opt-in wiring. Signed + attested (PRD R5).

## 2. Theme bridge (PRD R2)

`theme_bridge.py`: parse the active Omarchy theme (their theme files
are simple kv/CSS), map to `axiom-design-tokens` values, emit a
site-override tokens file (the canon's documented override path —
ADR-123 intact: canon unchanged, bridge is a values producer). Ships
with contrast tests (WCAG AA on ink/ground pairs) so a low-contrast
theme degrades gracefully rather than illegibly.

## 3. Reuse ledger

| Reused | For |
|---|---|
| fleet report/status + Mac launchd + node systemd patterns | enrollment + reporters (already built this week) |
| action ledger + receipts | sudo journal records |
| axiom-design-tokens + ADR-123 override path | theming |
| Receipts surface (F0) | click-through destination |
| BudgetBar state (F2) | burn fraction |
| Sigstore/PEP 740 pipeline (ADR-109) | plugin attestation |

New-code budget (PRD strawman #4, capped): state writer, sudo
journal, waybar module (~40 lines), SwiftBar script (~60 lines),
theme bridge, plugin packaging. No new services, no new stores.

## 4. Cross-platform posture

`state.json` is the portability boundary — any shell on any OS renders
from it. Legacy Linux = the waybar module minus Omarchy packaging
(plain systemd timer). Windows tray shell reads the same file (later;
seam named now).

## 5. Phases

- **P1** (gated on F0 shipped): state writer + macOS SwiftBar item —
  our own dogfood ambient within days.
- **P2**: waybar module + theme bridge + plugin packaging; beauty-gate
  review; marketplace submission.
- **P3**: sudo journal (opt-in) + the flagship receipt card;
  unattended-install fleet stanza; launch post (slots after F0's
  screenshot post per the campaign).
