# ADR-207: One serving mode for every surface; a bound node forwards shared reads

**Status:** Proposed (2026-10-27)
**Amends:** [ADR-195](adr-195-a-node-has-a-binding-and-a-sandbox.md) (a node has a binding)
**Related:** ADR-205 (a key belongs to a person; sharing programs, PR #1440), ADR-157
(serving budgets), ADR-168 (where a chat answer comes from), ADR-174 (Postgres is the
runtime database), ADR-131 (there is always a medallion)

## Context

ADR-195 made a data read follow the binding. A laptop that does not hold the medallion
and has a current binding sends `axi data` reads to the bound node's `/data/query`, with
the binding's key, and never opens a database of its own. Three surfaces followed in the
0.70 line:

- **Data reads.** `data_platform/read_target.py`: `resolve_read_target()`, `ask()`, the
  forwarded verbs `FOLLOWS_THE_BINDING` and `KIT_READS`, the one write `SUBMITS`, and
  `bound_key_for(url)`, which offers the key only to the bound node's origin.
- **Chat.** `chat/bound_model.py` (the node's `/v1` model, `choose_default_model`) and
  `chat/bound_tools.py` (the node's data tools). `chat/where.py` reports the result as
  BOUND, LOCAL or LOCAL TEST MODE, on the banner and on every reply.
- **Documents.** `rag/remote_store.py` (PR #1411): the node's `/api/v1/rag/search`, with
  the same key as a bearer. `rag/store_factory.resolve_store_url()` picks it.

The web app did not follow. The local FastAPI node (`axi serve`) serves `/api/v1`, and its
catalog routes (`webapp/api/catalog.py`: sites, channels, series) open this machine's
database. Its chat route builds a bare gateway, so a bound laptop's browser chat answers
with whatever model this machine has. The Charts harness reaches a figure service of its
own. The result is a laptop in two modes at once: the terminal answers from the site, the
browser from the laptop, and nothing on the page says which.

Each surface also resolved the mode on its own. The CLI and chat used
`resolve_read_target()`. Document retrieval used `current_binding()` directly, and a
`rag.database_url` setting won over the binding, so a bound laptop could read the site's
data and ground on the laptop's documents. The test-data mark (`AXIOM_SAMPLE_DATA`) lived
in chat only.

## Options

1. **The web app connects to the site database.** Rejected. It bypasses the gate, the
   site scoping on the key (ADR-205 Rule A), the serving budgets (ADR-157) and the
   gold-only rule, and it puts a database credential on a laptop.
2. **The browser calls the site node directly.** Rejected. The key would reach the
   browser, the site node would need CORS for every laptop, and saved views, drafts and
   the kit sandbox, which are personal and local, would have nowhere to live.
3. **Only hosted.** Use the site's own web app and stop running the web app locally.
   Rejected as the only mode: developing the app, and a personal workspace next to shared
   data, both need a local server.
4. **A bound local node forwards shared reads (chosen).** The browser talks to the local
   node. The local node answers personal state from its own Postgres and forwards shared
   reads to the site node's serving tier, over HTTPS, with the binding's key.

## Decision

### 1. There are two real modes, and one labelled test mode

| Mode | Where | Shared data, documents, model | Personal state |
|---|---|---|---|
| **hosted** | the site's own web app, on the site node | the node itself | the node |
| **bound** | `axi serve` on a laptop with a current binding | forwarded to the bound node | local Postgres |
| **local** | an install with no binding (or one that holds its own medallion) | this machine | local Postgres |
| **test** | any install with `AXIOM_SAMPLE_DATA=1` | invented sample data, always marked | local Postgres |

Hosted means using the site's web app. Bound means developing, or a personal workspace
next to shared data. Test is for rehearsals, demos and tests, and is never unmarked.

The local node never connects to the site's database. Its local Postgres holds personal
state only: saved views, drafts, settings and the kit sandbox.

### 2. One resolver

`axiom.infra.serving_mode.resolve_mode()` is the only function that decides the mode. It
returns a `ServingMode`: `mode`, `site`, `node` (the bound node's host), `face_url`,
`key_site`, the sources of data, documents and model, the `view_as` audience, and the
reason. It is configuration only (no network) and never raises.

Its order:

1. `AXIOM_SAMPLE_DATA` set: **test**. It wins over a binding, because a sample answer
   that looks real is the failure worth preventing.
2. This install holds the medallion (ADR-195 §1) and serves a site: **hosted**.
3. A current binding: **bound** (`read_target.resolve_read_target()` says `binding`).
4. Otherwise: **local**.

Every surface reads it: the CLI data reads (`read_target`), chat (`where.sources_of`,
`bound_model`), document retrieval (`store_factory.resolve_store_url`), the MCP
primitives (which build their stores through the same factory), and the web app
(`GET /api/v1/binding` and the forwarded routes). No surface reads `site-bindings.json`
or the sample-data variable for itself.

Reachability is separate. `serving_mode.probe(mode)` asks the bound node for its
`/api/v1/version`, with a short timeout, and is called by the web endpoint and by
`axi serve --bound` at start. The mode is still `bound` when the node is down: it is
bound and unreachable, never quietly local.

### 3. The forwarding allowlist

A bound node forwards reads from an explicit list, and nothing else:

- the data door verbs already listed: `read_target.FOLLOWS_THE_BINDING` and
  `read_target.KIT_READS`, over `POST /data/query`;
- documents: `POST /api/v1/rag/search` (`RemoteRetrievalStore`);
- the model: the node's OpenAI-compatible `/v1` (`bound_model`);
- the web app's shared reads, `webapp.bound_web.WEB_READS`: the catalog (`sites`,
  `channels`), `series`, `dates`, `revision` and `figure`. Each is answered by the
  node figure lane (below), which reads only through the data door verbs above.

The one write is a kit submission (`read_target.SUBMITS`, `POST /data/submit`), which is
`invoke` at the node's gate and still needs the operator's approval there. Every other
write a local surface makes (a saved view, a draft, a setting) stays local. No route is
forwarded by pattern; adding one is a change to the list and its test.

The figure lane is the node-backed figure source in `scidisplay/node_figure_source.py`
(branch `feat/figure-source-from-a-node`). The web app builds it from the binding (face
URL, `bound_key_for`) and holds day calendars in memory only. Until that module is
installed, a bound install answers the figure routes with a plain 503 naming the missing
lane; it does not fall back to the local database.

### 4. The twelve rules

Each rule has a test (§6).

1. **No mixed mode.** One resolver decides the mode for chat, documents, charts, the CLI
   and MCP. A parity test fails if any surface disagrees.
2. **No silent fallback.** If the bound node is unreachable, the read fails closed with a
   clear error that names the site and the node, and the chip turns red. A local answer
   is never substituted. Anything served from cache says its age (`cached_age_s`).
3. **Test data is always marked**: on the chip, on every figure (a note and a field in
   the response), and in every chat footer (`TEST MODE`, from `where`).
4. **Reads only.** Forwarding uses the explicit list in §3. A write is refused before it
   leaves the machine, except a kit submission, which still needs the operator.
5. **The key never reaches the browser.** The local node attaches it from the vault
   (`read_target._key`, the binding's `token_ref`). No response body, header, URL or log
   line carries it. `axi serve --bound` binds 127.0.0.1 and refuses another host unless
   `--expose` is given, and then warns on every start that the binding's key now answers
   for whoever reaches that port.
6. **Demo scope is visible.** The chip shows whose key it is (`key_site`) and which site.
   A binding may list `view_as` audiences, each a role and a `token_ref` for a key
   minted to that audience (under ADR-205, a key that belongs to a person and carries
   that role's grants). Selecting one (`AXIOM_VIEW_AS`) makes every forwarded read use
   that key, and the chip says "viewing as <role>". The minimal build reads the list
   and the selection; minting the audience keys stays with ADR-205.
7. **Version skew is said, not suffered.** The node advertises its API version and the
   oldest client it supports (`/api/v1/version`: `api`, `min_client`). The local app
   compares and disables only what the node does not serve, with a line saying why.
8. **Not chatty.** Figures are bucketed (the lane picks the bucket for a point target),
   wide windows are read in bounded chunks, the catalog is cached keyed by the site's
   revision, and the node's budgets (ADR-157) answer with their own messages, passed
   through unchanged.
9. **Partner data stays bounded on a laptop.** Forwarded answers are cached in memory,
   bounded by entry count, and cleared when the binding changes. Nothing forwarded is
   written to disk.
10. **Saved views name data by stable identity** (site, feed, channel; and the revision
    they were saved at). When the revision has changed, the view opens with "data
    revised since you saved this".
11. **Documents follow the mode.** Bound means the site corpus plus the community
    corpus, through the node. A local store configured on a bound install is used only
    with an explicit opt-in (`rag.local_documents_when_bound`), and is then labelled as
    this machine's, separately from the site's.
12. **Offline is a state.** With no network, the chip and the endpoint say offline and
    the routes fail closed; personal state still works.

### 5. The chip

A small, unobtrusive chip in the top bar, beside the account menu, reading
`GET /api/v1/binding`:

| State | Label | Dot |
|---|---|---|
| bound | `<site> · <node>` | green |
| hosted | `<site> · hosted` | green |
| local | `local` | grey |
| test | `test data` | amber |
| unreachable or offline | `<site> · unreachable` | red |

Hover or click opens a small panel: where data, documents and the model come from, the
key's site and any `view_as`, the node's version and any skew note, the age of anything
cached, and how to switch (the product's `site bind` and `site unbind` verbs, which write the binding, and `axi serve --bound`). It
is a button with an accessible name that states the mode in words, uses the shell's
theme tokens, and never shows the key.

`GET /api/v1/binding` returns:

```json
{
  "mode": "bound",
  "site": "alpha-site",
  "node": "node.example.org",
  "key_site": "alpha-site",
  "view_as": "",
  "view_as_options": [],
  "sources": {"data": "alpha-site via node.example.org",
              "documents": "node.example.org (site and community corpora)",
              "model": "the node's model at node.example.org"},
  "test_data": false,
  "node_api": {"api": "v1", "version": "1.2.3", "min_client": ""},
  "skew": [],
  "reachable": true,
  "error": "",
  "checked_at": "2026-10-27T12:00:00Z",
  "switch": ["site bind <site>", "site unbind", "axi serve --bound"]
}
```

### 6. Tests

- **Parity.** A fake node is the composed app behind the real authz hook, with a key
  minted the way the node mints one. For bound, local and test configurations, the CLI
  read target, chat's sources and model, document retrieval, the MCP store, and
  `GET /api/v1/binding` all report the same mode and site.
- **Fail closed.** With the node down, the forwarded routes answer 503 naming the site
  and node, the endpoint says `reachable: false`, and the local database is never
  opened.
- **The key never reaches the browser.** Every response of the binding endpoint and the
  forwarded routes, and every log record emitted while serving them, is scanned for the
  key.
- **Writes are not forwarded.** A write verb through the forwarder is refused before any
  HTTP call; the allowlist and the node's served verbs are one list.
- **Test data is labelled.** In test mode the endpoint, every figure response and the
  chat stamp carry the mark.
- **appkit.** The chip renders each state with its accessible name, and never renders a
  key.

## Consequences

- The web app is no longer an exception: `/api/v1` plus the data door is one contract,
  local or remote. A browser on a bound laptop sees the same data, documents and model
  as the terminal beside it.
- A bound laptop depends on the node for every shared read. That is the point: when the
  node is down the page says so, instead of showing the laptop's data as the site's.
- A `rag.database_url` that used to win on a bound laptop now needs an explicit opt-in.
  A laptop relying on that silently will see documents from the node instead, and the
  chip says so.
- The figure routes in bound mode need the node figure lane installed. Until it lands
  they refuse plainly.
- Each new forwarded read is a deliberate list entry with a test, so the allowlist grows
  slowly by design.

## Relation to other decisions

- **ADR-195** gave a node a binding and made the data read follow it. This ADR extends
  "follows the binding" from the data read to every surface, and makes the mode one
  resolved value rather than a habit each surface repeats.
- **ADR-205** decides what a key may read: a key belongs to a person, and access comes
  from grants and sharing programs. This ADR never widens that: every forwarded read is
  judged by the node, on the key, exactly as a terminal read is. The `view_as` audience
  keys are ADR-205 keys, and the chip makes visible whose key is speaking.
- **ADR-168** says where a chat answer comes from; `where` now takes its mode from the
  one resolver.
