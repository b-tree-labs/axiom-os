# `tests/` — Cross-Cutting Tests Only

Extension-specific tests live colocated with their extensions at
`src/axiom/extensions/builtins/{ext}/tests/`.

This directory is for tests that span multiple extensions or test
platform-level concerns.

## Structure

```
tests/
  conftest.py        # Shared fixtures (available to all tests)
  cli/               # CLI framework tests
  e2e/               # Multi-extension end-to-end tests
  extensions/        # Extension framework tests
  integration/       # Cross-extension integration tests
  orchestrator/      # Platform orchestrator tests
  repo_sensing/      # Repo sensing tests
  review/            # Review workflow tests
  setup/             # Setup wizard tests
```

## What belongs here

- Tests that exercise multiple extensions together
- Tests for platform infrastructure (`infra/`)
- Tests for the CLI framework itself
- End-to-end integration tests
- Shared test fixtures in `conftest.py`

## What does NOT belong here

- **Single-extension tests** → `src/axiom/extensions/builtins/{ext}/tests/`
- **Test data or fixtures specific to one extension** → same

## AI Agent Policy

When writing tests for a specific extension (sense, publisher, chat, etc.),
place them in that extension's `tests/` directory, not here. Only create
test files here if they genuinely test cross-cutting concerns. Import shared
fixtures from `tests/conftest.py`.
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
