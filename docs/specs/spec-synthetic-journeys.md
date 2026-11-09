# Spec: Synthetic Journeys

**Owner:** Ben Booth (B-Tree Labs) • **Status:** Draft (phase 1 implemented) • **Last updated:** 2026-10-06
**PRD:** none yet • **Key ADRs:** [ADR-169](../adrs/adr-169-a-live-surface-is-checked-by-a-scripted-user-journey.md), ADR-119, ADR-056

## Overview

`src/axiom/extensions/builtins/synthetic/` runs a scripted user journey against a live web surface over HTTP
and reports `pass`, `fail` or `unproven` per step. It is the check for failures a server cannot see in its
own health: a cacheable page that bypasses an access gate, an asset served as a login page, a redirect that
loops. The cause that prompted it is encoded as a named cache policy and reproduced in the tests against a real
server.

Status of each part: **phase 1 (this spec's contracts) is implemented**; phases 2 to 4 are designed, not built.

## Contracts

### Journey file (TOML)

```toml
[journey]
name = "gated-ui"
base_url = "https://app.example:3001"
timeout_s = 10          # default 10
verify_tls = true       # default true

[journey.session]
cookie_env = "SYNTH_SESSION_COOKIE"   # env var holding "name=value"; the value is never in the file

[[step]]
name = "signed-out visitor is sent to sign in"
path = "/"
headers = { Accept = "text/html" }
[step.expect]
status_in = [302, 303, 307]
location_matches = "/login"

[[step]]
name = "signed-in page must be revalidated"
path = "/"
auth = "session"
[step.expect]
status = 200
content_type = "text/html"
cache_policy = "revalidate"
assets_not_html = true
```

Unknown keys are errors, so a typo cannot become a check that never runs. A step with `auth = "session"`
requires `cookie_env`. `method` defaults to GET. Redirects are never followed: the redirect is the observation.

### Expectations

| Key | Meaning |
|---|---|
| `status`, `status_in` | exact status, or one of a list |
| `content_type` | prefix match on `Content-Type` |
| `header_present`, `header_absent`, `header_matches` | presence, absence, or regex per header |
| `location_matches` | regex on `Location` |
| `cache_policy` | `revalidate`, `no-heuristic`, `long-lived` (below) |
| `body_contains`, `body_not_contains` | substring checks |
| `assets_not_html` | up to five same-origin scripts and stylesheets named by the page must not come back as HTML or as a redirect |
| `max_latency_ms` | the step's time limit |

### Cache policies (RFC 9111)

- `revalidate`: the response carries `no-store`, `no-cache`, or `max-age=0`, so a browser must ask before reuse.
- `no-heuristic`: the response is not eligible for heuristic freshness. Heuristic freshness applies when a
  cacheable status has `Last-Modified` and no `Cache-Control` freshness directive and no `Expires`.
- `long-lived`: an explicit `max-age` of at least one day (for hashed assets).

### Result

```
{journey, base_url, status: pass|fail|unproven, started_at, duration_ms,
 steps: [{name, status, detail, evidence: {status, elapsed_ms, headers, note}}]}
```

`evidence.headers` is an allowlist (`cache-control`, `last-modified`, `etag`, `expires`, `content-type`,
`location`, `vary`, `age`). Cookie and `Set-Cookie` values are never recorded. A journey takes its worst step:
`fail` over `unproven` over `pass`; an empty journey is `unproven`.

### Skill and CLI

`synthetic.run` takes `path` (a TOML file) or `journey` (a dict), optional `base_url` and `env`. `ok` means the
skill ran; the verdict is `value.status`. `axi synthetic run <file> [--base-url U] [--json]` exits 0 only on
`pass`, 1 on `fail`, 3 on `unproven`, 2 on usage errors, so it is usable directly as a deploy gate.

## Design

1. One `httpx.Client`, no redirect following, the session sent as a `Cookie` header per step that asks for it.
2. Each step is evaluated by pure predicates over the response; the runner holds no application knowledge.
3. The runner never raises: network errors and runner faults become `fail` with the exception type and message.
4. A missing credential is `unproven` and names the variable, so "nothing was checked" is visible.

### The tests

A real HTTP server shaped like a gated single-page app reproduces the incident: a page shell with
`Last-Modified` and no `Cache-Control` fails the `revalidate` step with the heuristic named in the reason,
while the signed-out redirect and the API 401 steps pass (the gate was never the problem). The same journey
passes against the fixed server (negative control). Also tested: missing credential gives `unproven`; a `fail`
outranks `unproven`; an unreachable target is a `fail` with the reason; an asset served as HTML is caught; cookie
values never appear in a result; unknown keys are rejected; exit codes.

### Limits

The HTTP runner proves what the server says, not what a browser does with it. For the incident class that is
the cause, not the symptom: the response headers are the thing a browser's cache obeys. Behaviour that only a
browser shows (a service worker, script execution, rendering) needs the driver (phase 3).

## Phases

1. **Implemented here:** extension, loader, runner, cache predicates, skill, CLI, tests.
2. **Designed:** a `synthetic` fleet report kind (effect-checked, stale when silent), alert on a standing
   failure through `notifications.alert` keyed `synthetic:<journey>:<first failing step>`, and a schedule block
   so the journey runs on an interval. The schedule loop is hosted by the data-platform orchestrator today, so a
   host without it needs an OS timer (`axi schedule install`).
3. **Designed:** an optional browser driver behind the existing `browser` extra: cold cache, stale cache, and
   expired cookie scenarios in a real browser. Absent driver gives `unproven`, never `pass`.
4. **Designed:** a deploy gate (post-deploy and pre-switch) and blue/green release, its own ADR.

## Decisions

- ADR-169: three outcomes, HTTP-first, cache behaviour as a named check, never raises, no secrets recorded, exit codes as a gate.

## Open questions

- Where deployed sites keep their journeys (owner: each site; the extension ships examples only).
- The alert recipient and escalation for a standing failure (owner: Ben Booth; no escalation ladder exists in the notifications layer today).
- Whether the schedule host should be a small standalone loop instead of the data-platform orchestrator (owner: implementer, phase 2).
