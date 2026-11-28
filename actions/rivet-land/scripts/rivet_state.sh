#!/bin/bash
# Hash everything that can change what the lander does, so an unchanged repository
# can skip a full evaluation.
#
# One read (a GraphQL query): every open pull request (head, draft, review decision,
# mergeability, labels, check rollup, updated time, auto-merge) and the open
# "RIVET: needs" issue. Those, the inputs that change behaviour, the config, the
# action's own files and a coarse time bucket go into one SHA-256. The next steps look
# the key up in the Actions cache (lookup-only) and save it after a full run.
#
# Any doubt leaves `key` empty and `lookup` false: the full path runs. This script
# only reads and never exits non-zero.
#
# Env: IDLE_READ_TOKEN (or GH_TOKEN), REPO, EVENT_NAME, ACTION_DIR, RUNNER_TEMP,
#      SKIP_ENABLED, SKIP_MAX_AGE_HOURS, SKIP_KILL, SKIP_LOCK, SKIP_ACT, SKIP_VERSION,
#      SKIP_OWNER, SKIP_CONFIG, SKIP_FORCE, ACTION_REF, GITHUB_OUTPUT. RIVET_NOW overrides the clock in tests.

out="${GITHUB_OUTPUT:-/dev/null}"
SCHEMA="rivet-state-v1"

open_path() {
  echo "key=" >> "$out"
  echo "lookup=false" >> "$out"
  [ -z "${1:-}" ] || echo "RIVET unchanged check: $1; running the full path."
  exit 0
}

[ "${SKIP_ENABLED:-true}" = "true" ] || open_path ""

# schedule and workflow_run look the key up. workflow_dispatch is a person asking for a
# run: it evaluates in full and saves. Anything about one pull request is never skipped.
lookup=false
case "${EVENT_NAME:-}" in
  schedule | workflow_run) lookup=true ;;
  workflow_dispatch) ;;
  *) open_path "" ;;
esac

# force-full: one run that reads no cache. It still computes (and later saves) the key.
[ "${SKIP_FORCE:-false}" != "true" ] || lookup=false

export GH_TOKEN="${IDLE_READ_TOKEN:-${GH_TOKEN:-}}"
[ -n "$GH_TOKEN" ] || open_path "no token"
[ -n "${REPO:-}" ] || open_path "no repository name"
[ -n "${ACTION_DIR:-}" ] && [ -f "${ACTION_DIR}/action.yml" ] || open_path "action files not found"
[ -n "${RUNNER_TEMP:-}" ] || open_path "no runner temp directory"
command -v gh > /dev/null 2>&1 || open_path "gh is not available"

if command -v sha256sum > /dev/null 2>&1; then
  hash() { sha256sum | { read -r h _; echo "$h"; }; }
elif command -v shasum > /dev/null 2>&1; then
  hash() { shasum -a 256 | { read -r h _; echo "$h"; }; }
else
  open_path "no hash tool"
fi

age="${SKIP_MAX_AGE_HOURS:-}"
[[ "$age" =~ ^[1-9][0-9]{0,2}$ ]] || open_path "unusable max age hours"
now="${RIVET_NOW:-$(date +%s)}"
[[ "$now" =~ ^[0-9]+$ ]] || open_path "unreadable clock"
bucket=$((now / (age * 3600)))

owner_name="${REPO%%/*}"
repo_name="${REPO#*/}"
# shellcheck disable=SC2016
query='query($owner:String!,$name:String!,$q:String!){
  repository(owner:$owner,name:$name){
    pullRequests(states:OPEN,first:100){
      totalCount
      pageInfo{hasNextPage}
      nodes{
        number headRefOid isDraft reviewDecision mergeable mergeStateStatus updatedAt
        autoMergeRequest{enabledAt}
        labels(first:50){nodes{name}}
        commits(last:1){nodes{commit{statusCheckRollup{state}}}}
      }
    }
  }
  search(query:$q,type:ISSUE,first:10){nodes{... on Issue{number title updatedAt}}}
}'
# shellcheck disable=SC2016
filter='.data.repository.pullRequests as $p
  | if ($p.totalCount != ($p.nodes | length)) or $p.pageInfo.hasNextPage
    then error("not one page") else . end
  | {prs: ([$p.nodes[] | {n: .number, h: .headRefOid, d: .isDraft, r: .reviewDecision,
        m: .mergeable, s: .mergeStateStatus, u: .updatedAt, a: (.autoMergeRequest != null),
        l: ([.labels.nodes[].name] | sort),
        c: (.commits.nodes[0].commit.statusCheckRollup.state // "NONE")}] | sort_by(.n)),
     issues: ([.data.search.nodes[] | {n: .number, t: .title, u: .updatedAt}] | sort_by(.n))}'

state="$(gh api graphql -f query="$query" -F owner="$owner_name" -F name="$repo_name" \
  -f q="repo:${REPO} is:issue is:open in:title \"RIVET: needs\"" --jq "$filter" 2> /dev/null)" \
  || open_path "could not read the repository state (rate limit or error)"
# gh's built-in jq sorts object keys, so do not assume which key comes first.
[[ "$state" == '{'*'"prs":['*'}' && "$state" == *'"issues":['* ]] || open_path "unreadable repository state"

config_hash="none"
if [ -n "${SKIP_CONFIG:-}" ] && [ -f "$SKIP_CONFIG" ]; then
  config_hash="$(hash < "$SKIP_CONFIG")" || open_path "could not hash the config"
fi
action_hash="$(cat "${ACTION_DIR}/action.yml" "${ACTION_DIR}"/scripts/* 2> /dev/null | hash)" \
  || open_path "could not hash the action files"
[ -n "$action_hash" ] || open_path "could not hash the action files"

digest="$(printf '%s\n' "$SCHEMA" "bucket=${bucket}/${age}" "kill=${SKIP_KILL:-}" \
  "lock=${SKIP_LOCK:-}" "act=${SKIP_ACT:-}" "pin=${SKIP_VERSION:-}" "owner=${SKIP_OWNER:-}" \
  "config=${config_hash}" "action=${action_hash}" "ref=${ACTION_REF:-}" "state=${state}" | hash)" \
  || open_path "could not hash the state"
[[ "$digest" =~ ^[0-9a-f]{64}$ ]] || open_path "unreadable hash"

dir="${RUNNER_TEMP}/rivet-seen"
mkdir -p "$dir" 2> /dev/null && echo "seen" > "$dir/seen" 2> /dev/null || open_path "could not write the cache file"

{
  echo "key=rivet-seen-${digest}"
  echo "lookup=${lookup}"
  echo "path=${dir}/seen"
} >> "$out"
exit 0
