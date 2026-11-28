# ADR-289: A version-only release commit on a green parent is tagged without rerunning the suite

**Status:** Accepted (2026-11-25). Approved by the owner on 2026-11-25 in chat, after the 0.70.48 cut waited about an hour on two queued full suites.
**Related:** ADR-257 (the loop: guards, smoke, revert), ADR-285 (RIVET decides on content), `docs/specs/spec-cicd-and-deployment.md`, issue #2441 item 10.

## Context

The rule was: a tag needs a completed green main run on that exact commit. A release commit changes the project version and the pins that follow it, and nothing else. On 2026-11-25 the 0.70.48 cut waited about an hour: main's CI queued the full unit suite for a revert commit and then for the release commit, and the release commit differs from its parent only in four version strings. Repeating about 34,000 tests on it re-proves its parent. The wheel build, the prerelease wheel check and the publish checks are what a release adds, and they still run.

## Decision

A tag of a commit may skip the unit suite, and the jobs that skip with it, when all of these hold:

1. The commit has exactly one parent, and that parent has a completed green run of `ci.yml` on `main` (a push run, status success).
2. Against the parent, every changed file is `pyproject.toml` or one of the release pin files (`actions/rivet-land/README.md`, `action.yml`, `caller-example.yml`), each modified in place: nothing added, deleted, renamed, copied, type-changed or mode-changed.
3. `pyproject.toml` changed exactly one line, `version = "X.Y.Z"`, to a higher plain `X.Y.Z`; the parsed `[project].version` is the old value in the parent and the new value in the commit. Every changed line in a pin file is that file's version pin and differs from the line it replaces by exactly the old version becoming the new one.

The check reads the diff's content, not file names (`release/version_bump.py`). It runs in the existing "Tag or main already tested" job, which already sets the `skip` that the unit, integration, install-path and migration jobs obey. The build, the prerelease wheel check and the publish job do not depend on that skip and run as before.

Any doubt takes the normal path: a merge commit, a diff the parser does not fully understand, a line it did not expect, a failed fetch of the parent, a failed query to GitHub. The command always exits 0 and prints `qualifies=true|false`, so a crash cannot read as a yes.

## Consequences

- A release is as fast as its wheel build and publish checks when its parent is green, instead of a full suite behind whatever else is queued.
- The rule "a tag needs a green run on that exact commit" is relaxed for exactly one shape of commit. A code change hidden in a version bump does not qualify; the negative cases are tests (a changed source file, code on the version line, a dependency pin, an extra line in a pin file, a pin that does not follow the release version, a version that does not rise, a merge commit, a mode change, a binary or no-newline marker).
- The real 0.70.48 release (parent 0d27ff808, release commit 3b6346985) qualifies; none of the other 39 newest commits on main did.
- Extending the list of files a bump may touch is a change to `PIN_FILES` and an ADR amendment, not a workflow edit.

## Not decided here

Whether a bump may land without its parent's run having finished (it may not), and whether other mechanical commits (changelog only, docs only) deserve the same path (docs-only already skips the suite through the `changes` filter).
