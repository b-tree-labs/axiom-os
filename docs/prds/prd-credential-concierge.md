# PRD: Credential Concierge (program feature 5)

**Status:** Living. **Spec:** `spec-credential-concierge.md`.

## Problem

Credential setup is where adoption dies: our own stranger-path work
proved it, and today's live case (a routine third-party app
registration failing silently for an hour) demonstrated it on a
platform we don't control. Meanwhile the market's consensus security
ask is token substitution — "the agent never sees the credential."
Nobody trusted automates the *issuance walk* (creating the app,
fetching the scoped key) — the step that actually costs users an
evening.

## Users

- A new user connecting their first providers (the 10-minute path).
- An operator rotating credentials across a fleet with receipts.
- Every connector we ship (research sources were the forcing case).

## Requirements

### R1 — Never the password (absolute)
The vault never stores, requests, or types a user password. The
concierge operates INSIDE the user's already-authenticated browser
session, walking official credential-issuance UIs only. What lands in
vault is the derived, scoped, revocable secret — as a SecretRef with
rotation metadata.

### R2 — Recipes, allowlisted and honest
Per-provider recipes exist ONLY for providers with stable developer
consoles, each carrying: the exact pages it may touch (issuance pages
only — never general account settings), a ToS-review note with date,
and expected-failure copy. No generic "drive any site" mode exists.

### R3 — Every step proposed, approved, and receipted
RACI propose→approve per navigation step (batchable to per-recipe
approval after first success); every step screenshot-receipted into
the ledger. The concierge is itself a receipted agent action — the
product dogfooding the product.

### R4 — Manual path stays primary
Every connector's setup renders the paste-two-values path FIRST; the
concierge is the acceleration button beside it. A user who distrusts
browser automation loses nothing.

### R5 — Verification is the exit criterion
A concierge run ends with the connector's real `verify()` (a real
search/call), not with "credentials saved."

### R6 — Platform order
macOS + Omarchy first (Playwright, Apache-2.0, both fine); Windows
follows free (Playwright is cross-platform; the seam is packaging).

## Non-requirements (v1)

- No session-cookie custody or automation-on-user-session for
  providers whose ToS forbids it (X named explicitly).
- No password-manager integration (that's the platform credential
  tool's job where a host provides one).
- No headless mode: the user watches the walk (visibility is a
  feature, not a limitation).

## Red-team

**Steelman.** Setup friction is the #1 adoption killer and the
stranger-path data proves it; the never-sees-credential posture is
verbatim market consensus; recipes + receipts make it the only
AUDITABLE setup flow anywhere; and it compounds every future
connector.

**Strawman.** (1) Browser automation on user accounts = ToS/ban risk
and brittle selectors; today's Reddit form failure shows flows break
in ways automation can't reason about. (2) Security optics: "an agent
drives my logged-in browser" scares exactly the buyers we court.
(3) Provider UI churn = permanent maintenance treadmill for a tiny
team. (4) The concierge failing mid-walk leaves users MORE confused
than the manual path.

**Design responses.** (1)→R2's allowlist (stable consoles only, ToS
reviewed and dated per recipe) + R1's issuance-pages-only scope; a
provider that fights (CAPTCHA walls, silent bounces) gets marked
`manual-only` in its recipe — today's Reddit lesson encoded.
(2)→R3+R4: watched, stepwise-approved, receipted, and always
optional; the pitch is "you approve every click and keep the film,"
which is MORE control than clicking around alone. (3)→recipes are
data (selectors + screenshots), community-fixable, and the treadmill
is bounded by the allowlist's size — growth is deliberate, not
demand-driven. (4)→every recipe defines its abort behavior: on any
divergence, stop, screenshot, hand the user the manual path at the
exact step reached.

## Success criteria / kill test

Success: three recipes shipped (a code host, a comms provider, a
research source), each demonstrated end-to-end on video from a fresh
profile; zero password bytes ever transit the system (asserted by
test on the recipe engine's input types).
Kill: if recipe maintenance exceeds one fix/provider/quarter in
dogfood, the allowlist shrinks instead of the team's patience — and
if it shrinks below three useful providers, the feature folds back
into great manual docs.
