<!--
Copyright (c) 2026 The University of Texas at Austin
SPDX-License-Identifier: Apache-2.0
-->

# PRD: `serve` — Axiom's HTTP Serving Substrate

**Status:** Draft (2026-06-25)
**Owner:** Ben Booth
**Layer:** Axiom core (`serve` extension, promoted from `http`)
**Related Specs:** [spec-serve.md](../specs/spec-serve.md)
**Related:** [prd-data-platform.md](prd-data-platform.md), [prd-federation.md](prd-federation.md), [prd-axiom-authz.md](prd-axiom-authz.md), [spec-aeos-0.1.md](../specs/spec-aeos-0.1.md)

---

## 1. Summary

Axiom has accumulated HTTP-serving needs across several extensions, but no
canonical place to serve them from. Today each extension that needs an HTTP
surface builds its own FastAPI app via the `http` extension's `create_app`,
runs it on its own port, and re-implements (or skips) logging, error shaping,
and authentication. Two older subsystems still ship hand-rolled `http.server`
daemons. Federation's A2A transport is specified but unbuilt, so the platform
has no place to actually receive a peer request. Authorization exists as a
decision function but is wired to no route.

`serve` promotes the existing `http` extension into the canonical core HTTP
substrate. It adds a **router registry** so any extension contributes a
router under a path prefix; `serve` composes them into **one FastAPI app run
by one process** behind a single `axi serve` command. Shared middleware
(request logging, error normalization, an authorization seam, a
peer-signature-verification seam) lives in one place. Federation rides this
substrate: its A2A and `/.well-known` routers mount onto the composed app, and
`serve`'s auth middleware calls federation's Ed25519 verifier to authenticate
inbound peers. The path-prefix mount fabric is the internal routing/proxy — no
nginx or ingress is required for a single-node deployment.

`serve` is a `core`-tier library that is always present. Whether a daemon
actually runs, and what gets mounted, is gated by a deployment profile and by
which consumers are installed.

---

## 2. Problem & Motivation

### 2.1 Serving sprawl

| Symptom | Where it lives today | Cost |
|---|---|---|
| Per-extension FastAPI apps | `data_platform/ingest_sink/api.py` (`POST /ingest`), `classroom/classroom_api.py` (`/classroom/*`), `notifications/gateway/routes.py` (`POST /herald/inbound/{vendor}`) — each calls `create_app` and runs its own server | N-port sprawl; N copies of "how do I run uvicorn"; no shared cross-cutting behavior |
| Legacy stdlib servers | `signals/serve.py` (`http.server.BaseHTTPRequestHandler`), `classroom/coordinator_server.py` (mid-migration to FastAPI) | Two serving stacks to maintain; no FastAPI middleware, OpenAPI, or test harness |
| Unbuilt federation transport | `spec-federation.md §2.1` specs an "A2A Server on configurable port (default 8443)" serving `/.well-known/agent-card.json`, `/.well-known/axiom-manifest.json`, and A2A task endpoints — **spec'd, not built**. `vega/federation/gateway.py` is pure-policy with an *injected* signer/verifier and **no transport** | Federation has no way to receive an inbound peer request — the entire protocol has no front door |
| No shared auth on any route | `authz` provides `authz_decide` (`axiom.extensions.builtins.authz.decide:decide`) but it is applied to **no HTTP route** — `create_app` installs no middleware. Federation Ed25519 verification (`vega/federation/identity.py`) is applied to no inbound HTTP. ADR-077 local-principal auth is designed-not-built | Every served endpoint is unauthenticated unless the consumer hand-rolls it |

### 2.2 Why now

The next wave of consumers all need HTTP, and none of them should each
reinvent serving:

- **Federation** cannot ship its A2A transport without an HTTP front door.
- **A domain consumer's web + mobile clients** (a downstream consumer) need a
  FastAPI surface for their UI clients.
- **Agentic conversations in chat channels** (the conversational-agents epic)
  need a stable HTTP API for inbound channel webhooks and agent replies.
- **Authorization** is wired to nothing until there is a middleware seam to
  wire it into.

A single composed substrate retires the sprawl, gives federation its
transport, and gives `authz` exactly one place to attach.

---

## 3. Goals & Non-Goals

### 3.1 Goals

- **G1** — One canonical core HTTP substrate (`serve`), promoted from `http`,
  that every HTTP-serving extension uses.
- **G2** — A router registry: any extension registers a router/sub-app under a
  path prefix; `serve` composes them into one app.
- **G3** — One composed FastAPI app run by one process via `axi serve`.
- **G4** — Auto-discovery of routers from installed extensions' AEOS `service`
  manifests — installing an extension that serves HTTP is enough to mount it.
- **G5** — Shared middleware centralized in `create_app`: structured request
  logging, error normalization (one error envelope), an authorization seam,
  and a peer-signature-verification seam.
- **G6** — `serve` is the HTTP substrate for federation: federation mounts its
  A2A + `/.well-known` routers onto the `serve` app and `serve`'s auth
  middleware calls federation's Ed25519 verifier for inbound peers.
- **G7** — The path-prefix mount fabric *is* the internal routing/proxy — no
  external reverse proxy required for single-node deployment.
- **G8** — Deployment-profile + tier gating decides whether a daemon runs and
  what is mounted, with an isolation escape hatch (an extension MAY run its own
  server on a separate port).

### 3.2 Non-Goals

- **NG1** — `serve` is not an API gateway product, ingress controller, or
  load balancer. The mount fabric is internal routing, not a multi-host edge.
- **NG2** — `serve` does not replace the in-process LLM gateway
  (`src/axiom/llm/gateway.py`). That stays an in-process router; `serve` only
  defines a seam for an HTTP front (built later).
- **NG3** — `serve` does not define authorization policy. It calls
  `authz_decide`; policy lives in the `authz` extension.
- **NG4** — `serve` does not host MCP. MCP servers (memory, classroom) remain
  stdio transports and are out of scope.
- **NG5** — `serve` does not implement the federation A2A handlers or the
  LLM HTTP front in this cut — those are seams (see §7).

---

## 4. Consumers

### 4.1 Current (mount in this cut)

| Consumer | Path prefix | Source today |
|---|---|---|
| Data Platform IngestSink | `/ingest` | `data_platform/ingest_sink/api.py` (its own `create_app`) |
| Classroom API | `/classroom` | `classroom/classroom_api.py` (its own `create_app` + `ThreadedServer`) |
| Notifications/HERALD inbound | `/herald` | `notifications/gateway/routes.py` (`POST /herald/inbound/{vendor}`) |

### 4.2 Legacy (planned migration)

| Consumer | Path prefix | Source today |
|---|---|---|
| Signals serve | `/signals` | `signals/serve.py` (stdlib `BaseHTTPRequestHandler`) |
| Classroom coordinator | `/classroom/coordinator` | `classroom/coordinator_server.py` (mid-migration to FastAPI) |

### 4.3 Planned (seam now, build later)

| Consumer | Path prefix | Notes |
|---|---|---|
| Federation A2A + well-known | `/a2a`, `/.well-known/*` | Mounts onto `serve`; auth middleware calls federation Ed25519 verifier (§7.1) |
| LLM gateway HTTP front | `/llm` | In-process gateway gains a mounted HTTP router (§7.2) |
| Domain consumer web + mobile | (consumer-defined) | Downstream FastAPI surface on the consumer's own `serve` app |
| Agentic conversations in channels | (epic-defined) | Inbound channel webhooks + agent-reply HTTP API on `serve` |

---

## 5. Requirements

### Epic: Router Registry & Composition

| ID | Requirement | Priority |
|----|-------------|----------|
| SRV-001 | An extension registers a router/sub-app under a path prefix via a registration API | P0 |
| SRV-002 | `serve` composes all registered routers into one FastAPI app via `create_app` | P0 |
| SRV-003 | Auto-discover routers from installed extensions' AEOS `service` manifests | P0 |
| SRV-004 | Duplicate / conflicting path prefixes are detected and fail loudly at compose time | P0 |
| SRV-005 | Mount the three current consumers (ingest_sink, classroom, herald) on the composed app | P0 |
| SRV-006 | Registration order is deterministic (sorted by prefix) so the composed app is reproducible | P1 |

### Epic: `axi serve` Command

| ID | Requirement | Priority |
|----|-------------|----------|
| SRV-010 | `axi serve` starts the composed app in one process (thin wrapper over a skill fn per ADR-056) | P0 |
| SRV-011 | Default bind `127.0.0.1`; host/port configurable via flags + config | P0 |
| SRV-012 | Keep the uvicorn signal-handler guard so the CLI owns Ctrl-C / SIGTERM | P0 |
| SRV-013 | `axi serve --list` prints the composed route table (prefix → extension) without starting | P1 |
| SRV-014 | `axi serve --profile <name>` selects the deployment profile that gates what is mounted | P1 |

### Epic: Shared Middleware

| ID | Requirement | Priority |
|----|-------------|----------|
| SRV-020 | Structured request logging middleware (method, path, status, latency, request id) | P0 |
| SRV-021 | Error-normalization middleware producing one consistent error envelope | P0 |
| SRV-022 | Authorization seam: optional middleware that calls `authz_decide` per request | P0 (seam), P1 (default-on) |
| SRV-023 | Peer-signature-verification seam: optional middleware that calls federation's Ed25519 verifier | P0 (seam), later (build) |
| SRV-024 | Middleware order is fixed and documented (logging → error → peer-sig → authz → route) | P1 |

### Epic: Federation Substrate

| ID | Requirement | Priority |
|----|-------------|----------|
| SRV-030 | Federation mounts its A2A + `/.well-known` routers via the router registry (SEAM in this cut) | P1 (seam) |
| SRV-031 | `serve`'s peer-sig middleware calls federation's verifier to authenticate inbound peers (SEAM) | P1 (seam) |
| SRV-032 | The deployment profile decides whether the A2A router is mounted | P1 |
| SRV-033 | Peers call each other's `serve` endpoints directly — no central reverse proxy | P1 |

### Epic: Tiers & Deployment

| ID | Requirement | Priority |
|----|-------------|----------|
| SRV-040 | `serve` is `core` tier — always present as a library | P0 |
| SRV-041 | Whether a daemon runs is gated by deployment profile + installed consumers | P1 |
| SRV-042 | One composed process by default; an extension MAY run its own server (profile=server, separate port) for isolation | P1 |

### Epic: Install & Diagnose

| ID | Requirement | Priority |
|----|-------------|----------|
| SRV-050 | `fastapi` + `uvicorn` declared as an optional dependency/extra (e.g. `axiom-os-lm[serve]`) | P0 |
| SRV-051 | A loud diagnose when the serve extra is missing (mirror the extraction-deps install gap) | P0 |
| SRV-052 | `serve` is discovered and started on install when the deployment profile calls for a daemon | P1 |

---

## 6. Tiers & Deployment Profiles

| Tier | Meaning for `serve` |
|---|---|
| **core** | `serve` is always present as a library (`create_app`, registry, runner). No daemon implied. |

| Profile | Daemon? | Typically mounted |
|---|---|---|
| **library** | No | Nothing — extensions call `create_app` programmatically (tests, embedding) |
| **server** | Yes | Composed app: all installed consumers under their prefixes |
| **server (isolated)** | Yes (extra process) | A single extension (e.g. data-platform) on its own port for isolation |

The profile decides whether the daemon runs and which routers mount (e.g.
whether federation's A2A router is included). Profiles never grant a route a
capability it didn't register — they only narrow what is exposed.

---

## 7. Build Now vs. Later Seam

### 7.1 In scope now (build)

- Promote `http` → `serve`; keep `create_app`, `ThreadedServer`, `run_server`,
  and the signal-handler guard.
- Router registry + compose + conflict detection (SRV-001..006).
- `axi serve` command + `--list` (SRV-010..013).
- Shared middleware: structured logging, error normalization, and the
  **authz seam** (SRV-020..022, SRV-024).
- Mount the three current consumers: ingest_sink, classroom, herald
  (SRV-005).
- The serve extra + missing-deps diagnose (SRV-050, SRV-051).
- A documented migration plan for the two legacy stdlib servers (SRV in §4.2;
  not the migration itself).

### 7.2 Seam only (define, do not build)

- **Federation A2A router mount** + the peer-signature-verification middleware
  that calls `vega/federation` Ed25519 verification (SRV-023, SRV-030, SRV-031).
- **LLM gateway HTTP front** as a mounted `/llm` router over the in-process
  gateway (§4.3).
- **Deployment-profile / tier runtime gating** of which daemon runs and what
  mounts (SRV-032, SRV-041, SRV-052) — the profile vocabulary is defined now;
  the runtime gating is later.

---

## 8. Success Criteria

| Criterion | Target |
|---|---|
| Single composed app serves all three current consumers | `axi serve --list` shows `/ingest`, `/classroom`, `/herald` |
| One process by default | A standard `server`-profile node runs exactly one uvicorn process |
| Auth attachable in one place | Enabling the authz seam guards every route without per-consumer code |
| Federation has a front door (seam) | The A2A + `/.well-known` mount points and verifier call site are defined and testable with a stub verifier |
| No external proxy for single node | Reaching `/ingest` and `/classroom` requires only `axi serve` — no nginx |
| Missing-deps failure is legible | Starting without the serve extra prints an actionable diagnose, not an import traceback |

---

## 9. Decision & Con-Mitigation Plan

**DECIDED (2026-06-26): consolidate all Axiom HTTP serving under one engine.**
The chat HTTP API folds into a `/chat` mounted router (former Q2 resolved); the
authz seam defaults **on when `authz` is installed**, off otherwise (former Q1
resolved). Consolidation is the direction *because* it centralizes auth, audit,
logging, TLS and gives federation its substrate — but the build MUST mitigate
the cons of a single engine, not ignore them. The required mitigations
(detailed in spec §14.1):

- **Coupled blast radius** → per-mount **fault isolation** (a bad router is
  skipped/contained, never crashes the composed app; per-mount health).
- **Conflated trust zones** → per-mount **bind/trust-zone** (loopback vs.
  LAN/peer sockets; EC-sensitive surfaces never share a public bind) — load-
  bearing given export-control posture.
- **Coupled deploy + resource contention** → `deployment_profile` lets an
  extension run an isolated process/port for independent release + scaling.
- **Dependency weight** → serve is a core *library*; the *daemon* + fastapi/
  uvicorn are gated by profile + installed consumers (headless pays nothing).

### Remaining open questions

1. Per-prefix vs. global middleware beyond the `requires_authz` flag (deferred).
2. `server (isolated)` profile: shared registry + subset mount (default) vs.
   private registry.

---

## Cross-references / docs to update

These docs should reference the `serve` extension; none are modified by this PRD.

| Doc | Should reference `serve` for |
|---|---|
| `prd-data-platform.md` | IngestSink (`POST /ingest`) mounts on `serve` rather than running its own app/port |
| `adr-079` (§8.4.1) | The ingest endpoint transport is decoupled — `serve` provides the transport, ingest provides the router |
| `spec-federation.md` | The "A2A Server" (§2.1) rides the `serve` substrate: A2A + `/.well-known` mount via the router registry; inbound peer auth via `serve`'s peer-sig middleware → federation verifier |
| `adr-049` | PLINTH status/heartbeat surfaces are served via `serve` (one composed process), not a bespoke server |
| `adr-031` | `serve` is an exemplar of extension self-containment for a `core` service + cmd |
| `spec-aeos-0.1.md` | `serve` is the canonical example of a `service` (composed HTTP app) + `cmd` (`axi serve`) pairing |
| `docs/working/install-scenarios.md` | `serve` is discovered and started on install per the deployment profile; the serve extra + diagnose |
| Conversational-agents-in-channels epic | The chat/channel HTTP API mounts on `serve` (inbound channel webhooks + agent-reply API) |
