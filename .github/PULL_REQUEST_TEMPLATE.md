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
