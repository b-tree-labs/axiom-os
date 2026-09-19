# Spec — In-enclave web search (provider configs)

Status: Accepted (2026-06-27)
Related: [spec-ec-client-capability.md](spec-ec-client-capability.md)

In-enclave web search/fetch gives a routed model web access transparently (the
ingress injects `web_search`/`web_fetch`, executes them in the enclave, and
loops results back into the turn — the client never sees the calls). EC is not
air-gapped; only controlled *content* must not egress, enforced by classifying
the query before it leaves (withheld fail-closed if export-controlled).

## Providers

All five are preconfigured in `axiom.web.search`. Default is the no-key option
(DuckDuckGo) so it works immediately; keyed providers activate when their key is
present. Select with `axi settings set web.search.provider <name>`.

| Provider | Key needed | Key env | Vault name | Endpoint (egress) |
|---|---|---|---|---|
| `duckduckgo` | no | — | — | `lite.duckduckgo.com` |
| `tavily` | yes | `TAVILY_API_KEY` | `web-search:tavily` | `api.tavily.com` |
| `brave` | yes | `BRAVE_API_KEY` | `web-search:brave` | `api.search.brave.com` |
| `exa` | yes | `EXA_API_KEY` | `web-search:exa` | `api.exa.ai` |
| `serpapi` | yes | `SERPAPI_API_KEY` | `web-search:serpapi` | `serpapi.com` |

Key resolution order: `<PROVIDER>_API_KEY` env → vault `web-search:<provider>` →
vault `<provider>`. A keyed provider with no resolvable key falls back to the
no-key default (graceful, never a hard error).

## Configuring a keyed provider

Vault the key (real terminal — `getpass` needs a TTY):

```bash
.venv/bin/python -c "from axiom.infra.connections import store_credential; import getpass; \
  store_credential('web-search:tavily', getpass.getpass('tavily key: '))"
```

Then make it the default (optional):

```bash
axi settings set web.search.provider tavily
```

## Verifying

```bash
AXIOM_ROOT="$PWD" .venv/bin/python mcp-test/websearch_smoke.py
```

Reports PASS / EMPTY / SKIP (no key) / FAIL per provider, plus the EC
query-guard check.

## EC posture

The search **query/URL** egresses to the chosen provider. The model and all
controlled content stay in-enclave. The query is classified before egress and
**withheld** (fail-closed, including on classifier outage) if it carries
export-controlled terms — analogous to a developer not typing controlled
specifics into a browser search box.
