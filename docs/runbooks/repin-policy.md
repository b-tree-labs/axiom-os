# CI runner and action re-pin policy

Workflows pin `runs-on: ubuntu-24.04` (never `ubuntu-latest`) and pin every
action to a commit SHA. Pins stop silent drift; this page says when and how we
move them on purpose.

## Schedule

- **Re-check on every GitHub default-runner announcement.** GitHub moves
  `ubuntu-latest` on its own timeline; the quarterly watch below catches the
  announcement.
- **Ubuntu 26.04:** plan the move once 26.04 is out of preview *and* `main`
  passes a one-off trial on it. (As of 2026-10-12, 26.04 is generally available
  and `ubuntu-latest` moves to it in November 2026, so the trial is due now.)
- **Hard deadline:** move before GitHub retires 24.04 — historically about two
  years after its successor becomes the default. Treat a 24.04 deprecation or
  brownout announcement as the last call.

## Procedure

1. Branch `ci/runner-<image>` from `main`; change only `runs-on` to the new
   image label (e.g. `ubuntu-26.04`).
2. Run the full test matrix once on that branch (push, or `workflow_dispatch`).
3. Promote (merge) only after every required check is green. Fix real
   breakage in its own PR; never paper over it with `continue-on-error`.
4. Close the tracking issue once `main` runs on the new image.

## Quarterly check

`.github/workflows/runner-image-watch.yml` runs quarterly (and on demand). It
reads open `Announcement` issues in `actions/runner-images` and, if one moves
`ubuntu-latest` or deprecates / browns out / retires 24.04, opens or updates a
single tracking issue (deduplicated by a hidden marker). Otherwise it does
nothing. Logic: `.github/scripts/runner_image_watch.py`
(`DRY_RUN=1` to test locally).

## Dependabot weekly batch

`.github/dependabot.yml` checks the `github-actions` ecosystem weekly (Monday)
and groups every action bump into **one** PR, which keeps SHA pins current
without a PR per action. Review it like any CI change: required checks green,
then merge.
