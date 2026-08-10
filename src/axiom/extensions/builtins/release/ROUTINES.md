# RIVET Agent Routines

## Heartbeat (every 5 minutes)

1. Check CI pipelines (GitHub Actions + GitLab CI)
2. If failure detected:
   a. Match against known failure patterns
   b. If match found: suggest fix
   c. If no match: record new pattern template for learning
3. Local-main sync: fetch each workspace repo; fast-forward clean,
   non-diverged default branches; surface diverged/dirty ones (never touch)
4. Check PyPI versions match latest tags
5. In developer mode: check for unpushed commits

## On Push Event

1. Record the push (repo, branch, commit)
2. Wait for CI to start (30s timeout)
3. Monitor until complete
4. If green: suggest tag if on main
5. If red: diagnose, match patterns, suggest fix

## On Tag Event

1. Build wheel
2. Verify wheel installs cleanly in temp venv
3. Publish to PyPI (if configured)
4. Verify PyPI propagation (pip index versions)
5. Handoff to Tidy: "v{version} deployed, begin monitoring"

## Pre-Push Checks

Run all prevention commands from failure pattern DB:
- Python 3.11 compat
- Package name consistency
- Dependency availability
- Test markers on git-dependent tests

## Handoff Protocol

RIVET -> Tidy: after deploy, Tidy takes ownership of health
RIVET -> PRESS: after tag, PRESS generates release notes
Tidy -> RIVET: if post-deploy health fails, RIVET investigates
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
