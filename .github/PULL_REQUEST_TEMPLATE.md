<!--
Thanks for contributing to Axiom! Keep PRs focused on one concern.
See CONTRIBUTING.md for the house rules.
-->

## What & why

<!-- What does this change and why? Link any related issue (e.g. "Closes #123"). -->

## How it was tested

<!-- Commands you ran; new/updated tests. TDD is the house rule. -->

## Checklist

- [ ] Tests added/updated and `pytest` passes
- [ ] `ruff check src/ tests/` is clean
- [ ] Commits are signed off (`git commit -s` — DCO)
- [ ] No secrets, PII, personal paths, or domain/institution hardcoding
      (the mirror guard, `tests/test_mirror.py`, enforces this)
- [ ] Docs/CLI help updated if behavior changed
- [ ] Loosens auth, validation, sandboxing, redaction, or telemetry? → @benjaminbooth sign-off
- [ ] Adds network calls, `eval`/`exec`, or subprocess/shell? → @benjaminbooth sign-off
- [ ] Adds or upgrades a dependency? → @benjaminbooth sign-off, and pip-audit is green
- [ ] Fix for a `from-public` issue: our own failing test was written first; no reporter code, patches, or scripts were pasted
- [ ] No private names, prices, customers, or internal links in anything the public mirror ships

### Web app and model loop (skip if this PR touches neither)

See `docs/design/README.md`, the design contract.

- [ ] Design section cited (for example "design §chart, PRD §21.2")
- [ ] Acceptance-row IDs implemented, listed here, and each carried by a test marker
- [ ] As-built reuse stated: the component from `docs/design/reuse-catalog.md` this extends (or why the row says "missing")
- [ ] Screenshots attached beside the matching mockup (light theme; phone width where the page changes)
- [ ] Rules DC-01 to DC-22 hold; any conflict with a frozen design is raised to the owner, not applied
