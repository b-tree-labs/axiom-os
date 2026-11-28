#!/bin/bash
# Decide, before anything is installed, whether RIVET has any work.
#
# Idle means: the open pull request count was read, it is exactly zero (drafts
# count), and no "RIVET: needs" issue is open (the full run is what closes it).
# Every other outcome, including every failure, is "not idle": the full path
# runs exactly as it always did. This script only reads; it never exits non-zero.
#
# Inputs (env): IDLE_READ_TOKEN (or GH_TOKEN), REPO, EVENT_NAME, GITHUB_OUTPUT, GITHUB_STEP_SUMMARY.

out="${GITHUB_OUTPUT:-/dev/null}"

not_idle() {
  echo "idle=false" >> "$out"
  echo "RIVET idle check: $1; running the full path."
  exit 0
}

# Only the open-set events can be idle. A pull request event, a review or a
# /rivet command is about one pull request and always runs.
case "${EVENT_NAME:-}" in
  schedule | workflow_dispatch | workflow_run) ;;
  *)
    echo "idle=false" >> "$out"
    exit 0
    ;;
esac

export GH_TOKEN="${IDLE_READ_TOKEN:-${GH_TOKEN:-}}"
[ -n "$GH_TOKEN" ] || not_idle "no token"
[ -n "${REPO:-}" ] || not_idle "no repository name"
command -v gh > /dev/null 2>&1 || not_idle "gh is not available"

# Zero or a positive integer, nothing else. Anything odd is doubt.
is_count() { [[ "$1" =~ ^(0|[1-9][0-9]*)$ ]]; }

prs="$(gh pr list --repo "$REPO" --state open --limit 1 --json number --jq 'length' 2> /dev/null)" \
  || not_idle "could not count open pull requests (rate limit or error)"
is_count "$prs" || not_idle "unreadable pull request count"
[ "$prs" = "0" ] || not_idle "$prs open pull request(s) found"

needs="$(gh issue list --repo "$REPO" --state open --limit 30 --search 'RIVET: needs in:title' \
  --json title --jq '[.[] | select(.title | startswith("RIVET: needs"))] | length' 2> /dev/null)" \
  || not_idle "could not read the needs issue (rate limit or error)"
is_count "$needs" || not_idle "unreadable needs-issue count"
[ "$needs" = "0" ] || not_idle "a RIVET needs issue is open and the full run closes it"

echo "idle=true" >> "$out"
line="RIVET lander: no open pull requests; nothing to evaluate."
echo "$line"
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  echo "$line" >> "$GITHUB_STEP_SUMMARY"
fi
exit 0
