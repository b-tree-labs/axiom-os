# webapp — web + mobile API extension

Serves a versioned **`/api/v1`** JSON API on the shared HTTP substrate (the
`http` builtin). One backend feeds two clients: a browser web app and the
imminent native mobile app. The frontend is **split** from the backend — built
and hosted separately — so the API is the single contract both clients consume.

## Architecture

- **Backend = pure API.** `mount.py:mount_spec()` returns a `MountSpec` for
  `/api/v1`; `axi serve` composes it onto the one FastAPI app. No static/SPA
  serving happens in the Axiom node.
- **Auth (planned).** Human/web auth comes from the shared `axiom.webauth`
  module (JWT access+refresh + `X-API-Key`), enforced per-route via FastAPI
  dependencies. Extracted and generalized from a reference implementation.
- **Persistence (planned).** `axiom.infra.db.session_for("webapp")` —
  schema-per-extension (ADR-052). Per-extension Alembic migrations.
- **Frontend.** Framework-agnostic; the reference UI is a Vite multi-page
  React app. See [`frontend/README.md`](frontend/README.md).

## Layout

```
webapp/
  axiom-extension.toml   # AEOS manifest: kind="service" /api/v1 mount
  mount.py               # MountSpec factory
  api/routers.py         # /api/v1 router assembly (health, version, → auth, resources)
  persistence/           # (planned) session_for models + repository
  migrations/            # (planned) per-extension Alembic
  frontend/              # reference Vite MPA (React); hosted separately
  tests/
```

## Run

```bash
axi serve --profile server          # composes /api/v1 onto the one app (:8787)
curl localhost:8787/api/v1/health   # {"status":"ok","service":"webapp"}
```

## Tests

```bash
pytest src/axiom/extensions/builtins/webapp/tests/ -q
```
