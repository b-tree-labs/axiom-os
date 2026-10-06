<!-- Copyright (c) 2026 The University of Texas at Austin -->
<!-- Copyright (c) 2026 B-Tree Labs -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# @axiom/design-tokens

The design tokens behind Axiom's web surfaces, shipped three ways from one
source: a CSS file (`tokens.css`), the same tokens as data (`tokens.json`,
generated), and a Tailwind preset (`tailwind.preset.js`). Lifted out of the
webgate login UI so that webgate and every later product surface (chat,
dashboards, extension UIs) draw from one palette and reskin from one place.

## What a token is

A token is a named design decision stored as a CSS custom property, for
example `--theme-accent: #bf5700`. Markup and component classes reference the
*name* (`var(--theme-accent)`, or the Tailwind class `bg-theme-accent`) and
never the value, so a product changes its look by changing values in one file
and every consumer follows without touching markup.

The names are kept identical to the portable component set, so auth markup
lifts between products unchanged in both directions; only the values are
Axiom's (a neutral gray palette with a burnt-orange accent). Three families:

| family | examples | varies by theme |
| --- | --- | --- |
| themed palette | `--theme-page-bg`, `--theme-text-primary`, `--theme-border`, `--theme-accent`, `--theme-nav-hover` | yes |
| short aliases used by the base document | `--page-bg`, `--panel-bg`, `--text-primary`, `--border-color`, `--accent` | yes (they point at the palette) |
| shared | `--app-font-sans`, `--button-radius`, `--button-radius-compact`, `--focus-ring`, `--focus-border` | no (declared once on `:root`) |

## The three theme states

`tokens.css` declares the palette three times, once per state of the
`data-theme` attribute on `<html>`:

| state | selector | when it applies |
| --- | --- | --- |
| unstamped root | `:root` | nothing has set `data-theme` yet: the base/fallback palette, which is **dark** |
| dark | `[data-theme='dark']` | the product stamps `<html data-theme="dark">` |
| light | `[data-theme='light']` | the product stamps `<html data-theme="light">` |

The base is dark on purpose. A page that has not yet run its theme bootstrap
paints the dark palette, so a dark-mode user never sees a white flash; webgate
stamps the saved or OS theme in an inline script before first paint (see its
`index.html`). Products that want a different default should stamp the
attribute, not edit `:root`.

## The light-last rule

All three selectors have equal specificity (`:root` is a pseudo-class and
`[data-theme='...']` is an attribute selector: both score `0,1,0`). The
cascade breaks that tie by source order, so **the light block must be the last
one in `tokens.css`**. Put it earlier and light mode silently renders the dark
palette. `scripts/build_tokens.py` refuses any other block order, and the
tests assert it.

## Zero external requests

Axiom runs on air-gapped sites. The token layer therefore contains nothing
that can reach the network: no `@import`, no `@font-face`, no external `url`
references, no Google Fonts, no CDN. Typography is a self-hosted system stack
(`--app-font-sans`). The build script rejects any at-rule or fetching value in
`tokens.css`, and the tests scan every shipped file in this package plus the
consuming product's CSS.

## Consuming the tokens

Today the package is consumed **by path** from inside the monorepo; the same
three files publish unchanged to a registry later (`files` in `package.json`).

**1. CSS.** Import the token file first, before Tailwind's directives or any
rule of your own (CSS requires `@import` to precede other statements; Vite and
postcss-import inline it into the bundle, so the shipped stylesheet stays
self-contained):

```css
/* webgate: src/index.css */
@import '../../../../../../../packages/axiom-design-tokens/tokens.css';

@tailwind base;
@tailwind components;
@tailwind utilities;
```

**2. Tailwind.** Use the preset and keep `content` and `plugins` in the
product config:

```js
// webgate: tailwind.config.js
import forms from '@tailwindcss/forms';
import tokens from '../../../../../../packages/axiom-design-tokens/tailwind.preset.js';

export default {
  presets: [tokens],
  content: ['./src/**/*.{js,jsx,ts,tsx}', './index.html'],
  plugins: [forms],
};
```

The preset maps `theme.extend.colors.theme.*` to `var(--theme-...)` (so
`bg-theme-panel`, `text-theme-muted`, `border-theme-subtle`, ... follow the
active theme at runtime), plus the brand-neutral status colours, the system
`fontFamily.sans`, and the `borderRadius` scale.

**3. Data.** `tokens.json` carries the same tokens for non-CSS consumers:

```json
{
  "shared": { "--app-font-sans": "...", "--button-radius": "0.625rem", "...": "..." },
  "themes": {
    "base":  { "--theme-page-bg": "#0d1017", "...": "..." },
    "dark":  { "--theme-page-bg": "#0d1017", "...": "..." },
    "light": { "--theme-page-bg": "#eef0f3", "...": "..." }
  }
}
```

The three theme maps have identical key sets. The resolved token set for a
theme is `shared` merged with `themes[<theme>]`. Values are the raw CSS text,
so aliases stay as `var(--theme-page-bg)` and the focus ring stays as its
`color-mix(...)` expression.

## What stays with the product

Only tokens live here. Component classes stay in the consuming product for
now: webgate's `src/index.css` keeps the `.btn` system and sizes, `.input-theme`,
the `.bg-panel` / `.text-theme-*` / `.border-theme*` utilities, the typography
roles, the base `body` rules, the form-control focus retargeting, and the
`html[data-input-mode='keyboard']` focus-ring gating. Lifting a shared
component set is a later design-system step.

## Regenerating and checking

`tokens.json` is generated; never edit it by hand.

```bash
python3 scripts/build_tokens.py          # rewrite tokens.json from tokens.css
python3 scripts/build_tokens.py --check  # verify; exit 1 on drift (CI)
python3 -m pytest tests                  # or, from the repo root: packages/axiom-design-tokens/tests
```

Both are stdlib-only Python 3 (pytest for the tests). The tests cover: the
build check passes; every `var(--...)` used by webgate's CSS and by the preset
is declared in `tokens.css`; the light block comes after the dark block; the
JSON theme maps share one key set; and nothing in the package or webgate CSS
can trigger a network request.

## Layout

```
packages/axiom-design-tokens/
  tokens.css              source of truth: :root, [data-theme='dark'], [data-theme='light']
  tokens.json             generated from tokens.css (shared + themes.{base,dark,light})
  tailwind.preset.js      Tailwind preset: colors.theme.* -> var(--theme-*), status, font, radii
  package.json            @axiom/design-tokens 0.1.0, no dependencies
  scripts/build_tokens.py generator + --check
  tests/test_tokens.py    invariants, consumer wiring, zero-external-requests scan
```
