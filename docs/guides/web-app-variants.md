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
