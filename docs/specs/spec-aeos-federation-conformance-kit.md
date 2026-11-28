# AEOS Federation Conformance Kit

**Status:** Draft   •   **Owner:** Ben Booth   •   **Last updated:** 2026-11-23

**Implements:** [AEOS 1.0](spec-aeos-1.0.md) §18.6 and §19   •   **Decisions:** [ADR-276](../adrs/adr-276-agents-federate-over-a2a-1-0-with-one-signing-rule-one-address-and-a-gate-on-everything-received.md)

**Wire spec under test:** [spec-agent-accord-protocol](spec-agent-accord-protocol.md)

---

## Overview

An implementation claims AEOS federation conformance by passing this kit. The
kit is two things: golden signed fixtures, which are language-neutral JSON, and
a suite of checks over them that runs from Python and from Go against the
implementation under test. The first outside user is an independent messaging
extension written in Go and Python; Axiom's own federation code is the second.

The design choice that matters: **the fixtures are the contract, not either
runner.** A Go and a Python implementation agree because both reproduce the
same bytes, not because one was tested against the other.

## 1. Scope

In scope: canonicalisation, signing and verification (§3 of the wire spec),
Agent Card content (§2), addresses (§5), delegation tokens, messages,
conversations, workloads and receipts (§6 to §8), the inbound pipeline's
refusals (§9), and peer-content-as-data (§7).

Out of scope: trust-graph scoring, transport security (a transport is tested by
its provider's own suite), and performance.

## 2. Levels

| Level | An implementation that passes it can say |
|---|---|
| **F1 Sign** | It produces and verifies AEOS signatures and canonical bytes. |
| **F2 Card** | F1, and it publishes and consumes conformant Agent Cards and resolves addresses. |
| **F3 Exchange** | F2, and it exchanges messages, conversations and workloads, with receipts and idempotency. |
| **F4 Govern** | F3, and its inbound pipeline refuses what it must and treats peer content as data. |

A claim names the level and the fixture version: "AEOS federation F3, fixtures v1".

## 3. Fixtures

Location: `tests/aeos_conformance/fixtures/v<N>/` in the reference repository,
published with each AEOS release. Version 1 holds:

| File | Checks | Level |
|---|---|---|
| `keys.json` | the test keys and pinned node ids | all |
| `canonical.json` | RFC 8785 under the AEOS restrictions, including the refusals | F1 |
| `signed/*.json` | card, message, delegation, workload offer, receipt chain | F1 to F3 |
| `negative.json` | hash-as-signature, wrong object type, tampering, unpinned `kid`, `alg = none`, instruction in content | F1, F4 |

Fixture versions are immutable. A change adds `v<N+1>/`; a runner states which
versions it runs.

Planned for v2 (needs the reference endpoints): an address-resolution table
(unique, unknown, ambiguous), an idempotency pair (same body, different body),
a transfer signed by both stewards, a child workload whose parent is canceled,
and a card missing each required AEOS field in turn.

## 4. The suite

### 4.1 Offline checks (F1, F2)

Run against a library, with no network:

1. For each accepted canonical vector, the implementation's canonical bytes
   equal `canonical_hex`.
2. For each refused vector, the implementation refuses to canonicalise it for
   signing.
3. Signing each `signed/*` object's body with the matching test key yields the
   same signature (Ed25519 is deterministic).
4. Each `signed/*` object verifies with the pinned keys; each negative vector
   with `expect = "refuse"` is refused.
5. The card has every field the wire spec §2.3 requires.

### 4.2 Live checks (F3, F4)

Run against a node under test, through the `loopback` transport, with the kit
acting as node-b:

1. Fetch `/.well-known/agent-card.json`; it verifies and pins.
2. Send `signed/message.json`'s shape, signed by node-b; it is accepted once,
   and a second delivery with the same `messageId` is a no-op.
3. Offer a workload; the node answers `submitted` (allowed or held) or
   `rejected` with a reason, and its receipts form a valid chain.
4. Send each negative vector; each is refused with a reason (unknown keys may be
   dropped silently, wire spec §9).
5. Send `neg-instruction-in-content` to an agent; the node records the message,
   and the only effects allowed are that record and, at most, an intent through
   its gate. The kit observes this through the node's receipts.

### 4.3 Runners

| Runner | Form |
|---|---|
| Python | a pytest plugin, `aeos-conformance`, parameterised over a fixture directory and an adapter object the implementation supplies (`canonicalize`, `sign`, `verify`, and for live checks a base URL) |
| Go | a package with `go test` entry points over the same fixtures and an adapter interface with the same four methods |

Both runners read the same files and report the same check ids, so results from
two languages can be compared line by line. CI in the reference repository runs
both runners over the reference implementation and over a pinned build of each
registered outside implementation, exchanging fixtures in both directions: a
Go-signed object must verify in Python and the reverse.

## 5. Reporting

A run produces `aeos-conformance-report.json`: implementation name and version,
level claimed, fixture version, and pass or fail per check id. A claim of
conformance cites a report from the reference CI or from the implementer's CI,
with the fixture version.

## 6. Status

| Item | Status |
|---|---|
| Fixtures v1 | In tree |
| Python runner | Not built |
| Go runner | Not built |
| Reference implementation passing F1 | No: boundary signing still uses per-object forms (ADR-276, follow-up 2) |
