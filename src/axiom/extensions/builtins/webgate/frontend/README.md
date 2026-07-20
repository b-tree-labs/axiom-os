<!-- Copyright (c) 2026 The University of Texas at Austin -->
<!-- Copyright (c) 2026 B-Tree Labs -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# webgate frontend — Axiom's default web auth UI

A brand-neutral, theme-aware sign-in UI for the `webgate` forward-auth gate.
Vite + React 18 + Tailwind 3, plain JSX, fully self-contained (system fonts,
no CDN / Google Fonts / external requests). It talks only to the gate's
cookie-session JSON seam — no tokens, no bearer headers, nothing in
localStorage but the theme choice.

Because it lifts from a portable component set (theme tokens, `.btn` system,
the `?next=` sanitizer), the screens stay reusable by any product — rebrand by
changing the `productName` passed to `<AxiomWordmark>` (a single swap point) and
the accent tokens in `src/index.css`.

## Develop

```bash
npm install
npm run dev
```

`npm run dev` starts Vite on **http://localhost:5273** and proxies the gate
endpoints (`/gate/session`, `/gate/me`, `/gate/logout`) to a locally running
backend at **http://127.0.0.1:8799** so the real login flow works end-to-end.
Point the proxy elsewhere with `AXIOM_GATE_TARGET=http://host:port npm run dev`
(or edit `vite.config.js`). Start the gate first — e.g. via `axi serve` — so
`POST /gate/session` has something to authenticate against.

## Build

```bash
npm run build
```

Emits a static bundle to `dist/`. Preview the production build with
`npm run preview`.

## Packaging (follow-up, not built here)

`dist/` is intended to ship inside the Axiom wheel and be served by `axi serve`
(the gate mounts the login UI at its own path and hands off to `/gate/*` for
auth). Wiring the build artifact into the wheel and the serve mount is a
separate packaging task.

## What's here

```
src/
  main.jsx                     entry + keyboard/pointer input-mode a11y hook
  App.jsx                      router: /, /login → LoginPage; /forgot → ForgotPassword
  index.css                    theme tokens (light/dark) + .btn system + focus rings
  contexts/ThemeContext.jsx    system|light|dark, persisted under `axiom_theme`
  components/
    ThemeToggle.jsx            sun/moon toggle
    AxiomLogo.jsx              AxiomMark + AxiomWordmark (productName prop = swap point)
    pages/
      LoginPage.jsx            email/password, show-hide, remember-me, forgot link
      ForgotPasswordPage.jsx   admin-managed reset message + back link
  lib/authClient.js            login/me/logout over /gate/* (cookie-session)
  utils/deeplinkUtils.js       sanitizeRelativeUrl / getSafeNextFromSearch
```
