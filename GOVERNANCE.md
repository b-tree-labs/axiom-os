# Governance

This document explains how decisions get made in Axiom, so contributors know what
to expect and how to have a say.

## Model

Axiom currently uses a **maintainer-led** model. A small group of maintainers is
responsible for the project's direction, reviews, and releases. As the community
grows we expect to formalize this further (and this document will evolve with it).

## How decisions are made

- **Most changes** happen through the normal PR flow: open a PR, a maintainer
  reviews, CI must pass, a maintainer merges.
- **Substantial or cross-cutting changes** (new core primitives, breaking
  changes, anything affecting the platform's shape) should start as an
  **issue or an ADR** so the approach can be discussed before code is written.
  Architecture Decision Records live in [`docs/adrs/`](docs/adrs/).
- **Disagreements** are resolved by discussion aiming for consensus; where
  consensus isn't reached, maintainers make the call and record the reasoning.

## What we optimize for

These values guide what gets accepted:

- **Domain-agnostic core.** Axiom is a substrate; it must not hardcode a specific
  institution, vertical, or deployment. Domain specifics belong in extensions or
  consumer products.
- **Deterministic safety.** Authorization and policy decisions are made by
  deterministic code, never by LLM output. Models advise; code decides.
- **Small, composable core; capability via extensions.** We prefer a new
  extension over growing the core.
- **Tested and reproducible.** TDD, green CI, reproducible builds, signed
  releases.

## Becoming a maintainer

Maintainers are recognized for **sustained, high-quality contribution** — good
PRs, helpful reviews, thoughtful issue triage, and good judgment about scope.
There's no application form: do the work, engage constructively, and existing
maintainers will invite you. Maintainer responsibilities include reviewing PRs,
helping triage, and upholding the [Code of Conduct](CODE_OF_CONDUCT.md).

## Code of Conduct & security

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md). Security
issues follow [SECURITY.md](SECURITY.md). Both are maintainer-enforced.

## Changes to this document

Governance changes are themselves proposed via PR and discussed openly.
