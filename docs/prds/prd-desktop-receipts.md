# PRD: Desktop Receipts (program feature 4 — Omarchy + macOS shells)

**Status:** Living. **Spec:** `spec-desktop-receipts.md`.
**Design source:** `docs/working/omarchy-axiom-experience-2026-09-21.md`.

## Problem

The receipts stack lives in a terminal and a web page; the people it
serves live on a desktop. Omarchy (1M+ installs, ten harnesses in
core, a 15-minute passwordless-sudo mode, no audit trail anywhere) is
the densest concentration of the audience; macOS is where we and most
harness users actually work. Governance that isn't ambient gets
checked never; a glyph in the bar gets glanced hourly.

## Users

- An Omarchy user running several harnesses with sudo windows.
- A macOS developer running Claude Code/Codex daily (us, first).
- The guerilla channel: the marketplace listing IS distribution.

## Requirements

### R1 — One glyph, ambient truth
A bar presence (waybar module on Omarchy; menu bar item on macOS):
green = all effects proven, amber = something UNPROVEN, red = FAILED,
grey = gone STALE; secondary burn indicator when budgets (F2) are
active. Click → the Receipts surface. The glyph reads local state
files; **zero daemons by default** — it must feel weightless.

### R2 — Theme-native or it doesn't ship
On Omarchy: the theme bridge ingests the user's active Omarchy theme
into `axiom-design-tokens` values — surface and TUIs match the desktop
the way OpenCode does (the culturally load-bearing quality). On macOS:
system light/dark + accent respected. An external Omarchy-native
reviewer approves aesthetics before the marketplace ever sees it (the
beauty gate).

### R3 — The receipted sudo window (Omarchy flagship)
Omarchy's passwordless-sudo mode stays EXACTLY as shipped. With the
plugin, opening the window arms a journal: every command in the window
is attributed (harness, session, process ancestry) and ledgered; on
close, a receipt card — "N commands as root by <harness>; /etc diff
attached." Nothing gated, everything remembered: DHH's
review-the-outcome model, given its outcome record.

### R4 — Fleet enroll in one motion
`omarchy plugin install axiom` offers enrollment (push URL + key);
their unattended-install path (cidata + tailnet auto-join) gains an
optional fleet stanza — a lab of Omarchy boxes lands on one board with
zero ceremony. macOS: the launchd reporter we already run, packaged.

### R5 — Signed, attested, verifiable
The plugin ships with our Sigstore/PEP 740 attestation story — in an
unsandboxed marketplace, we are the artifact whose own provenance can
be verified. Stated quietly in the listing; never as a dig at the
marketplace.

### R6 — Platform order
Omarchy + macOS at launch; legacy Linux (plain systemd, no
waybar/Hyprland assumptions) next; Windows tray shell later. All
OS-specifics live in per-shell adapters over one core.

## Non-requirements (v1)

- No Omarchy core-inclusion lobbying; marketplace only.
- No management of Omarchy's sudo policy (observe, never configure).
- No always-on daemon; PULSE/launchd/systemd timers only.

## Red-team

**Steelman.** Densest possible audience with a native gap; the sudo
receipt is the single best demo the thesis has (freedom untouched,
outcome recorded); the marketplace is free distribution; MIT-clean;
and the macOS shell serves our own dogfood daily.

**Strawman.** (1) DHH dunks on it publicly ("bureaucracy for your
genie") and the channel chills overnight. (2) Optics: we preach
verify-on-install, then ship into an unsandboxed marketplace.
(3) Hyprland/waybar API churn makes the module a maintenance
treadmill. (4) It's a distraction: a tiny team polishing a bar glyph
while enterprise features wait. (5) The sudo journal reads as
surveillance to some users.

**Design responses.** (1)→posture rules are binding (complement with
affection; never @ him, never pitch him; if it's good, that community
surfaces it) and R3's design is dunk-resistant: nothing is gated, so
the only thing to mock is *having receipts*. (2)→R5 turns the
weakness into our differentiator — the attested plugin. (3)→the
glyph is a waybar `custom` module reading files (the most
churn-stable integration point); the surface is a URL. (4)→F4 is
mostly packaging of F0–F2 outputs — the spec's new-code budget is
capped and cited. (5)→journal is opt-in at window-open (one-line
prompt, remembered), local-only, and the user owns deletion; copy
says "your record, not ours."

## Success criteria / kill test

Success: listed on the marketplace with the beauty gate passed; ≥1
sudo-window receipt from a real Omarchy user we don't know; the macOS
menu bar item running on our own machines daily.
Kill (map, refined): if installs show glyph-only usage — no Receipts
opens, no receipts read — deeper integrations (cross-mem onboarding,
drift) don't get built on hope; investigate why the glyph didn't pull
before building more.
