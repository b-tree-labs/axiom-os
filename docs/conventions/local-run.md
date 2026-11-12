# Local-Run Convention

How any Axiom-family repository runs on a developer's machine: one command, the
same in every repo, live enough to iterate on what you see, and safe beside
every other checkout. The engine is `axi dev` (the `dev` extension); a repo adds
only what is different about it, in an optional `dev.toml`.

## The targets

Every repo's Makefile carries the same names and wraps `axi dev`:

| Target | Does |
|---|---|
| `make dev` | Starts the app. Idempotent. Prints ONE URL to open and how to sign in. |
| `make dev-status` | Whether it is up, and its URL. Exit 0 up, 1 down. |
| `make dev-down` | Stops only what `make dev` started. |
| `make dev-check` | Starts it, proves the hot-reload loop, stops it. Non-zero on any miss. |
| `make dev-logs` | Prints the log paths and tails them. |
| `make dev-shot` | Writes screenshots to `./.dev/shots/` (gitignored). |
| `make dev-db CMD=status\|migrate\|reset\|seed` | This worktree's schema (`reset` needs `YES=1`). |

## Rules

1. **The engine is the platform's.** A repo never re-implements node start-up,
   sign-in, ports or lanes; it declares what is different about it.
2. **The lane owns every port and the database name.** A checkout holds a lane
   (`axi lane`): `lane.front` is the hot-reloading web page, `lane.api` is the
   node, `lane.database` is its own database. A repo never chooses, hard-codes,
   scans for or kills a port, and never runs a server by hand on a standing
   node's port. If the band is full, `dev up` says so and points at `axi lane`.
3. **State lives under the engine's dev home**, `<state>/dev/<lane>/`: pid and
   URL records, logs, `schema.json`. `status` and `down` work from any shell and
   signal only the pids recorded there (process groups).
4. **Sign-in goes through the vault.** No password in an env file, argv, a log or
   the terminal.
5. **Prerequisites are checked first**, one line each with the next command.
6. **A repo runs its own config.** `[dev].config_dir` is exported to the node as
   `AXIOM_CONFIG_DIR` for this run only; nothing is copied into the user's state.
7. **Scripts**: logic over ten lines lives in `scripts/dev/<name>.sh`, safe to
   re-run, no secrets on stdout.
8. **Documented identically**: a README section "Run it locally" and a link from
   the repo's agent-instructions file.
9. **Proven**: `make dev-check` passes on a clean checkout.

### Fast visual iteration

10. **Hot reload by default.** The web server runs in dev mode against the live
    node; the one URL printed is the hot-reloading page. A change to a component,
    stylesheet, token or brand file shows in about a second, with source maps.
11. **Brand and tokens are live too**, in light and dark.
12. **`dev-shot`** writes light and dark screenshots with stable file names.
13. **`dev-check` proves the loop**: it changes one token in a temporary override,
    asserts the dev server serves it within 3 seconds with no restart, and puts it
    back. A slow or rebuilding loop fails.
14. **Data for looking at screens**: `dev db seed` loads clearly marked sample data.

### Bare Axiom has no web page

Axiom itself declares no `[web]` section, so there is no hot-reload loop to run
here: its visual iteration lives in the component kit's `make dev`. In a repo
with no `[web]`, `make dev-check` is a node-only smoke: the node comes up,
`/gate/login` and `/app/` answer 200, the schema has no ahead or diverged drift,
and it stops. It says "no web server declared: hot reload is checked in the repo
that has one".

### Schema is worktree-aware

15. **One database per lane**, created if absent and migrated for THIS checkout
    with the platform's own runner (`axiom.infra.db.provision_extension` over every
    extension that ships migrations), in a child process with the checkout's source
    first on the path. `<home>/schema.json` records the migrated head per
    extension and the commit.
16. **Drift is detected, never papered over.** Behind: migrated forward. Ahead (a
    revision this checkout does not have) or diverged (several heads): `dev up`
    stops, names the extension and revisions, and offers `dev db reset`. Nothing is
    dropped automatically.
17. **`dev db` verbs**: `status`, `migrate`, `reset` (drops and recreates only the
    lane's own database, refuses any other name, needs `--yes`), `seed`. The
    database server comes from `--db-url` or `$AXIOM_DB_URL`; the lane's database
    name replaces its path, so the shared or production database is unreachable
    from a dev lane. With neither named, `dev up` looks for a server in this order:
    an `AXIOM_PG_TEST_URL` that answers, then the shared dev server below, then
    (Docker present) starts it, else prints one-line fixes. The implicit
    `localhost:5432` is never a candidate. A lane with no database of its own (`unmanaged`) is refused
    with the `axi lane claim --replace` fix.

A clean machine needs no setup: `dev db server up|down|status` manages ONE local
Postgres (pgvector) container for all lanes, with a stable name, a named volume,
a host port reserved through the lane registry, and its password in the vault
(never printed, never on a command line). `dev down` leaves it running (lanes
share it); `dev db server down` stops it and keeps the data.

### Tenants

18. **`[[tenant]]` declares sites the one local node serves.** The node is bounded
    to those sites with `AXIOM_SERVED_SITES` (`axiom.infra.site_scope`).
19. **Personas are real gate accounts.** Each tenant gets one account per persona,
    bound to the tenant's site, holding a role the gate already has (a persona is
    only a name for a role; an unknown role is refused). Passwords are in the
    vault. `axi dev as <tenant>[:persona]` prints the tenant's URL and the vault
    command for the password; `--open` opens the URL.
20. **Side by side**: each tenant is addressed as `http://<tenant>.localhost:<port>`
    (`*.localhost` is loopback), so cookies are separate per tenant.

### Kinds, topologies and faults (ADR-255)

21. **Kinds.** `axi dev up --kind local|site|edge [--home <node>] [--node <name>]`. A
    `site` node (the default) serves a site's people and holds its data; a `local` node
    is one person's; an `edge` collects from local sources and sends outward,
    store-and-forward, to its `home`, with no inbound exposure. DAQ is what an edge does.
    `--home self --site S` enrols an edge with this checkout's own node; `--home-url URL
    --home-key NAME` points one at a home on another machine.
22. **Capabilities, not more kinds**: `serve`, `collect`, `ingest`, `send`, `fetch`,
    `accept-binds`. A relay is a `site` node whose only capability is `ingest`.
23. **Topologies are small graphs.** `make dev TOPOLOGY=<name>` brings every node up, each
    on its own lane `<checkout-lane>.<node>` (ports; a database when it serves), with its
    own state, keys file, bronze and outbox under its dev home. Generic: `solo`, `team`,
    `collect`, `relay`, `multi-tenant`; a deployment declares its own in `dev.toml`.
    `dev status` lists every node and the health of each link; `dev down` stops all.
24. **The platform's own parts.** A serving node is the platform node; a relay is the
    ingest edge (`serve --profile ingest-edge`) with its outbox; an edge is the DAQ
    producer and transmitter with a labelled sample source (`source_class = simulated`,
    `tags.sample`); a `fetch` link is `data.edge_pull` on a timer. Keys are real issued
    keys: an edge's is minted into its home's keys file, bound to its site, ingest only;
    a fetching node's reads only the relay's export. Tokens live in the vault and reach a
    child process through its environment only.
25. **Faults**: `axi dev fault <edge> source-offline|home-down|relay-down|slow-link|clock-skew
    [seconds]`, and `clear`, so held, late and unknown data can be watched catching up.
26. **Proved locally.** `make dev-check TOPOLOGY=<name>` sends N readings from each edge,
    stops each home mid-stream, starts it again, and asserts every reading arrives at the
    node holding that site's data exactly once, and that an edge's key cannot write another
    site's data (403). A topology with no edge checks that each serving node answers.

## `dev.toml`

All sections are optional. Placeholders `{port}` (the lane's front port) and
`{node_url}` (the node on the lane's api port) are filled in; a repo never names
a port.

```toml
[web]
cwd = "path/relative/to/root"
command = "npm run dev -- --host 127.0.0.1 --port {port}"
ready_path = "/app/"            # polled until 200; the printed URL is front + this
env = { VITE_API_TARGET = "{node_url}" }

[dev]
config_dir = "path"             # exported as AXIOM_CONFIG_DIR for this run only
seed = "dotted.module:function" # run by `dev db seed`, with the lane's database
shots = "command"               # run by `dev shot`; gets DEV_URL, DEV_NODE_URL, DEV_SHOTS_DIR

[personas]                      # persona = a role the gate has
visitor = "guest"

[[tenant]]
id = "alpha"                    # the host label: alpha.localhost
site = "alpha-site"             # the rights boundary (defaults to id)
config = "../alpha-site"        # a site checkout; else under $AXI_DEV_SITES
flags = { example = "on" }
brand = "alpha"
```

```toml
[[topology]]                    # a named topology (ADR-255); generic ones need no declaration
name = "relay"
[[topology.node]]
name = "probe"                  # lowercase, digits, '-'
kind = "edge"                   # local | site | edge
site = "alpha"
capabilities = ["collect", "send"]
home = "gateway"                # implies a `send` link
channels = ["sample.temperature"]
rate_hz = 5
[[topology.node]]
name = "gateway"
kind = "site"
capabilities = ["ingest"]       # a relay
[[topology.node]]
name = "hub"
kind = "site"
capabilities = ["serve", "fetch"]
tenants = ["alpha"]             # the sites this node hosts
config = "path"                 # optional: AXIOM_CONFIG_DIR for this node
[[topology.link]]
kind = "fetch"                  # send | fetch | bind
from = "hub"
to = "gateway"
```

Default personas map to gate roles: `operator` to `operator`, `experimenter` to
`creator`, `manager` to `admin`, `visitor` to `guest`, `auditor` to `viewer`.

Without `[web]` the printed URL is the node's. `axi dev status --json` reports the
lane, `front`, `api`, `urls`, whether the web server is up, and the database block.
