# RIVET Agent Routines

## Heartbeat (every 5 minutes)

1. Check CI pipelines (GitHub Actions + GitLab CI)
2. If failure detected:
   a. Match against known failure patterns
   b. If match found: suggest fix
   c. If no match: record new pattern template for learning
3. Read PR checks and the trunk of each repo on the one watch list
   (`agents/rivet/watched-repos.toml`), and report what changed
4. Check PyPI versions match latest tags
5. In developer mode: check for unpushed commits

A heartbeat concludes `green`, `red` or `blind`. Blind means nothing was
observed, or a target did not resolve. It is never green, and it exits 2.

The heartbeat is watch-only by default: it reads and reports. Setting
`release.heartbeat_watch_only` false also lets it fast-forward clean default
branches, close stale issues, propose landings, and call a model for a failure
narrative. It runs from any directory and needs no repository.

## Forge-side watch (every 30 minutes)

Runs in the forge, not on this machine, so it survives the local watch dying.

1. Read main's newest CI verdict and the start of any red spell
2. Red or blind: open or update the one incident
3. Green: close the incident this job adopted
4. A forge that cannot be read fails the job and writes nothing

It cannot see the local heartbeat. A local sweep watches it back.

## Main red

An urgent alert goes out at once with the incident and the culprit, then again
every `red_main_repeat_hours` until green. A new spell alerts at once.

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
