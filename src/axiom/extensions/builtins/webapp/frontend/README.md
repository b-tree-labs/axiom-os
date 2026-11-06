# webapp frontend — reference UI + the framework contract

The Axiom `webapp` backend is **framework-agnostic**. This directory holds the
*reference* frontend (a Vite multi-page React app), but nothing in the backend
depends on it. Any frontend that honors the contract below is a drop-in
replacement — swap this directory, leave `/api/v1` untouched.

## The contract (what any frontend must do)

1. **Consume `/api/v1` only.** All data comes from the versioned JSON API. No
   server-side coupling to the Axiom node; the frontend is a separate deploy.
2. **Authenticate with a bearer token.** `POST /api/v1/auth/login` → access +
   refresh JWTs; send `Authorization: Bearer <jwt>`; refresh via
   `POST /api/v1/auth/refresh`. (Same path the mobile app uses.)
3. **Emit a static build** to `dist/` (multi-page: one HTML document per page),
   servable by any static host (nginx / CDN / object store).
4. **Register its origin** for CORS on the API (allowlist), since web and API
   are different origins under the split model.

Meet those four and the backend does not care whether the frontend is React,
Vue, Svelte, Astro, or server-rendered templates.

## Reference implementation (planned)

- **Vite** multi-entry (one HTML per page) + **React** + a shared nav/shell —
  a multi-page app with cohesive navigation, deliberately **not** a SPA.
- Axiom's own brand + style tokens (its style guide, not a borrowed one).
- Plain `fetch` API client with bearer + refresh handling.
- Deploy: static build → separate host (nginx `try_files` per page), origin
  CORS-allowlisted against `/api/v1`. Mirrors the "split" serving model.

## Not this repo's job

The frontend build is not packaged into the Axiom wheel (split model). It is
built and deployed on its own; only its **origin** needs to be registered with
the API for CORS.
