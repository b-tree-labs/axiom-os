# Running the web app's three variants

One codebase, three compositions side by side over one shared data platform
(build-out spec, rows VAR-A1 to VAR-A4 and MVP-12-A4):

| Variant | Port | Path behind the front door | Session realm / cookie | App-state tenant |
|---|---|---|---|---|
| `axiom` (bare Axiom) | 8960 | `/v/axiom/` | `axiom-bare` / `axiom_s_axiom` | `variant-axiom` |
| `product` (bare product) | 8961 | `/v/product/` | `product-bare` / `axiom_s_product` | `variant-product` |
| `site` (product + site overlay) | 8962 | `/` | `site` / `axiom_s_site` | the install's own sites |

```bash
axi app variants                      # the records, as resolved here
axi app run --variant product         # one variant, supervised
axi app run --all                     # all three under one supervision loop
axi app run --all --dry-run --json    # the plan; starts nothing
```

Each variant runs as a supervised copy of the one served app
(`axiom.infra.switch.Supervisor`, the same supervisor as `serve --supervise`), so
SIGHUP switches every variant with no refused request. The front door routes the
paths to the ports; the app is told its path in `AXIOM_APP_BASE_PATH`.

## Run it on your laptop

`axi dev up` is the short path. It runs your checkout's node, makes a first account and prints one
URL. `axi app run` is the supervised path a node uses. On a laptop it needs the same account file
and front page, which `axi dev up` sets for you.

**Before you start.** You need an Axiom clone (the
[contributor quickstart](contributor-quickstart.md)), the Postgres container from its step 3, and
the appkit wheel, which holds the page bundle: `pip install axiom-appkit` (to change appkit itself,
see [appkit from source](run-from-source.md#appkit-from-source)). Use an environment that holds Axiom
alone. If a product's web package is installed in the same environment, `axi dev up` lands on that
product's `/app/` page instead of `/receipts/`, because the front door picks the first surface
mounted. Then apply the migrations to that database once. `AXIOM_DB_URL` chooses the database, so point it at your own:

```sh
export AXIOM_DB_URL=postgresql://axiom:axiom@127.0.0.1:55432/axiom_db
axi db migrate upgrade
```

If Postgres is not running, the node still starts, but pages that read the database fail.

`axi dev up` refuses to start when `AXIOM_DB_URL` is not set, rather than falling back to
`localhost:5432`, which may be somebody else's database. Load your env file, or pass the database with
`axi dev up --db-url <url>`. Every run prints the database it uses as `host:port/dbname`.

**Start it.**

```sh
axi dev up
```

```text
  http://127.0.0.1:8912/receipts/

  Sign in as you@example.org
  Database: 127.0.0.1:55432/axiom_db
  Password: axi vault get dev-gate-axiom --reveal | pbcopy
```

The port and the credential name come from your checkout, so yours will differ. The email is your
`git config user.email`. On an install bound to a site that publishes a development sign-in
client, it also prints `Sign in: SSO via <issuer host> (from the bound site)`; see
[Local sign-in](run-from-source.md#local-sign-in). Copy the password with the line it printed, open the URL and sign in. You
should see the appkit shell, with the Axiom wordmark, a rail and a chat. Open `#/log` after the path
to see the Log page. A bare node says `No logbooks are installed on this node`, which is correct: a
logbook comes from an extension.

**Stop it** with `axi dev down`. `axi dev status` says whether it is up.

**The supervised path.** Export the account file and the front page, then run the variant. The
account file is in the folder that `axi dev up` printed as `log:`.

```text
export AXIOM_GATE_USERS_FILE=<that folder>/users.json
export AXIOM_DEFAULT_SURFACE=/receipts/
axi app run --variant axiom
```

Open `http://127.0.0.1:8960/`. Press Control-C to stop it. Without `AXIOM_DEFAULT_SURFACE`, `/` is a
plain list of mounted paths with no styling. That is the node's index, not a fault. Check first that
the port is free with `lsof -iTCP:8960 -sTCP:LISTEN`. The paths in the table above are what a
front door routes; on your laptop you use the port directly.

**The Log flag on a bare node.** Log appears on the rail with no flag, because bare Axiom declares no
`log` flag and an undeclared flag cannot hide it. `AXIOM_FLAG_LOG` and `[variant.axiom.flags]` have
no effect here: `GET /api/v1/flags` returns an empty map. A flag takes effect once an extension
declares it (`docs/specs/spec-flags.md`).

**How a product layers on this.** A product adds its own app and flag declarations, and a site adds
its brand and overrides. Each publishes its own steps in its own repository. Axiom's `product` and
`site` records say which port, realm and cookie to use. They do not bring a product's app with
them: on a bare install, those two variants serve the same pages as `axiom`.

## Overriding a record

A deployment overrides any field in `variants.toml` in its config directory
(`$AXIOM_VARIANTS_CONFIG` names another file). Two variants may not share a port,
path, realm, cookie or tenant.

```toml
[variant.product]
path = "/v/example-product/"
realm = "example-product-bare"
tenants = ["variant-example-product"]

[variant.site.flags]
library = "on"
log = "soon"
```

## What keeps them apart

- **Sessions (VAR-A2).** The realm is signed into the session token and each
  variant reads its own cookie; a session from one realm is refused by the other
  two. The `site` variant also honours a session from the plain install, so
  nobody already signed in is made to sign in again.
- **Data (VAR-A3).** In an isolated variant every caller is a guest of the
  variant's tenant: uploads land in `rag-tenant:<tenant>` (and a per-variant
  private corpus), and that variant's chat and search read only its tenant plus
  the community corpus.
- **Flags (MVP-12-A4).** The variant's flag set is a layer between the
  environment and the node override; isolated variants keep their own stored
  overrides (see `docs/specs/spec-flags.md` §2).
- **CI (VAR-A4).** `app/tests/test_variant_smoke.py` asks each variant's rail
  flags, search, chat, Chart and Library once, and runs in the Smoke job only
  when a web-app path changed.
