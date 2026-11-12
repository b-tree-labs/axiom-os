# ADR-169: A live surface is checked by a scripted user journey, and unproven is not a pass

**Status:** Proposed (2026-10-06)
**Related:** ADR-119 (fleet console, effect-checked status), ADR-056 (skills), ADR-157 (serving safeguards);
[spec-synthetic-journeys.md](../specs/spec-synthetic-journeys.md). Blue/green releases (a later ADR) build on this.

## Context

A deployed web surface can be healthy by every server-side measure and still be unusable for a person. In the
incident that prompted this, a new user reached a gated chat page and saw "500: Internal Error" with no way
to sign in. The gate, the application server and the proxy all answered 200; no log line was an error. The
cause was in the browser: the application's page shell was served with a `Last-Modified` date and no
`Cache-Control`, so the browser reused its cached copy without asking the server (heuristic freshness,
RFC 9111). With no valid session, the cached app called an API, received 401, and drew an error page. The
access gate was never consulted.

Health endpoints cannot see this. What would have is a check that behaves like a visitor: ask for the page
signed out, ask signed in, read what the server says about caching, and fetch the page's assets. Axiom has
probes (a model coherence probe, a readiness healthcheck, an install-time endpoint probe) and a fleet console
that already refuses to call something green without evidence, but nothing that walks a user path over HTTP.
Browser automation exists only as an optional extra, so a check that requires a browser cannot be the default.

## Decision

1. **A new builtin extension, `synthetic`,** runs a declared journey (a TOML file of steps with expectations)
   against a live base URL and returns a structured result. It is domain-agnostic: it knows HTTP, not the
   application.
2. **Three outcomes, never two:** `pass`, `fail`, `unproven`. A step that cannot be run (a credential is not
   supplied, a driver is not installed) is `unproven`, never `pass`. The journey takes the worst step. A gate
   requires `pass`.
3. **The default runner is HTTP only,** using the base dependency `httpx`, and says so: it proves what the
   server says (status, redirects, cache headers, content types, assets), not what a browser does with it.
   A browser driver is an optional addition behind the existing `browser` extra and, when absent, yields
   `unproven` for the steps that need it.
4. **Browser cache behaviour is a first-class check,** stated as predicates from RFC 9111
   (`revalidate`, `no-heuristic`, `long-lived`), so the incident's class (a response a browser may reuse without
   asking) fails a step by name.
5. **The checker never raises.** A network error, a bad response, or a runner fault becomes a failed result
   with the reason. A checker that crashes hides the outage it exists to report.
6. **Secrets are never recorded.** Session credentials come from named environment variables; cookie and
   `Set-Cookie` values are never written to a result.
7. **The command is a gate:** exit 0 only when every step passed, 1 on a failure, 3 when nothing failed but a
   step was unproven, 2 for usage errors.
8. **Results flow to existing surfaces:** a new fleet report kind `synthetic` (effect-checked, stale when
   silent), alerts through the notifications alert path keyed by journey and failure signature, and scheduling
   through the existing schedule substrate. These are phase 2.

## Options considered

- **Extend the health endpoints.** They answer for the server, which was healthy. Rejected.
- **A browser-only check (Playwright).** Proves the most, but it is not installed by default, is heavy to run
  on a node, and would make the safe default "no check". Kept as the optional driver.
- **Reuse the fleet-promotion canary.** It attests that a release is safe to promote across nodes and has no
  HTTP logic; overloading the name would confuse the two. Rejected; the new extension feeds the same console.
- **Treat a skipped step as a pass.** The failure mode this ADR exists to prevent. Rejected.

## Consequences

- A class of silent, browser-side failures becomes a named, scheduled, alertable check.
- The HTTP runner cannot prove browser behaviour; until the driver exists, the cache check proves the headers
  are right, which is the cause, not the symptom. The spec states this limit.
- Every site writes its own journeys. The extension ships the mechanism and examples, not site knowledge.
- Follow-up: schedule, fleet report and alert wiring (phase 2); the browser driver (phase 3); a deploy gate
  and blue/green release (phase 4, its own ADR).
