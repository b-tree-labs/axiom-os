# Tech Spec: Credential Concierge (feature 5)

**Status:** Living. **PRD:** `prd-credential-concierge.md`.

## 1. Shape

Extends the `connect` extension (setup is its domain):
- `concierge/engine.py` — recipe interpreter over Playwright
  (Apache-2.0; the one new dependency, allowed) using a persistent
  browser context (the user's profile, headed always). Input types
  are structurally password-free: the engine's action vocabulary is
  {navigate(allowlisted url), click(ref), read(field), fill(field,
  NON-SECRET literal)} — there is no fill-secret action, so PRD R1 is
  a type-level property with a test, not a policy.
- `concierge/recipes/<provider>.toml` — allowlisted URLs, steps,
  expected markers, abort behaviors, ToS note + review date,
  `manual_only` flag. Recipes are data; the engine refuses a recipe
  whose URLs stray off its own allowlist.
- `concierge/receipts.py` — per-step ledger entries with screenshot
  refs; run receipt links the resulting SecretRef + verify() result.
- Vault handoff: captured secrets go straight to the secrets
  extension as SecretRefs (keychain/OpenBao per existing providers)
  with `issued_via`, `rotate_hint`, `recipe_version` metadata; env
  materialization only at connector runtime (existing pattern).

## 2. Flow

`axi connect install <provider> --concierge` → RACI propose (the
step list, human-readable) → approve → headed walk with per-step
receipts → capture → vault → connector config write → `verify()`
(real call) → run receipt. Any divergence: abort per recipe, emit the
manual-path handoff card naming the exact step reached (PRD
strawman #4).

## 3. Reuse ledger

| Reused | For |
|---|---|
| connect ConnectorDescriptors + SetupSpec urls | recipe targets + needs |
| secrets extension providers | custody (SecretRef) |
| RACI propose→approve flow (hygiene pattern) | step gating |
| action ledger | step + run receipts |
| connectors' `verify()` | exit criterion |
| Receipts surface | run-receipt rendering (a concierge run is a task receipt — F1's shape) |

New: engine, recipe format, three launch recipes. Playwright dep
(pinned, licensed-checked in CI once the license gate ships).

## 4. Cross-platform

Playwright is mac/linux/windows; profiles differ per browser/OS —
the engine takes an explicit profile path (never guesses), and the
launch platforms document Chrome-profile discovery for macOS +
Omarchy only.

## 5. Phases

- **P1**: engine + password-free type test + one recipe (a code
  host: stable console, lowest ToS risk); headed run receipted end
  to end on our own machines.
- **P2**: RACI wiring + abort/handoff cards + two more recipes;
  rotation metadata surfaced in `axi secrets`.
- **P3**: Receipts-surface rendering of run receipts; the demo video
  (fresh profile → connected + verified in under two minutes) — a
  campaign artifact gated like all numbers.
