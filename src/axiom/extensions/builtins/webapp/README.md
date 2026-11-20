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

## The app database and gold (W13)

The app database (`session_for("webapp")`) holds exactly two kinds of table:
tables **published from gold** for what pages need fast, and **application
state** (saved views, channel labels). Silver never serves, directly or through
this database.

`catalog/publish.py` is the registry. A `Publication` names one gold object
(registration refuses anything outside the `gold` schema), the app table, a
declared freshness and its invalidation (`gold_change`, `schedule`, `both`).
Each publish records the gold object, the gold version the rows reached (gold's
revision, `gold.signal_catalogue.computed_at`, else the rows' watermark) and the
time in `publication_state` (MVP-11-A4). `refresh` publishes only when gold's
revision moved; `freshness` reports the lag, and past the declared freshness the
catalog API's `as_of.note` says "as of <time>, <lag> behind" for the page to
show (MVP-11-A5). `rebuild` drops the published rows and publishes them again:
the same rows, application state untouched (MVP-11-A6). The one publication
today is `site_catalog_channel` from `gold.signals`, refreshed by the
`webapp.project` skill (`full`, `rebuild` params).

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
