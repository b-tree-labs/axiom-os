#!/bin/bash
# Choose what Semgrep scans on a pull request.
#
# Main (push) always scans the whole tree, so main stays free of ERROR findings. A pull
# request scans only the files it adds, modifies or renames: the same rules, the same flags.
# The file list comes from the pull request files API, so no git history is needed.
#
# Outputs (GITHUB_OUTPUT): mode=all | files | secrets | none.
#   all     scan the whole tree (not a pull request, or any doubt)
#   files   scan the files in $RUNNER_TEMP/semgrep-targets.txt, one per line
#   secrets the pull request changes only docs (markdown, reStructuredText, images, pdf):
#           run ONLY the secrets pack over the changed text files in the same list
#   none    nothing to scan (the pull request only deletes files, or only changes images)
#
# Any doubt gives mode=all: the file list cannot be read, is empty or odd, is long, or names a
# file that is not in the checkout. This script only reads and never exits non-zero.
#
# Env: EVENT_NAME, GH_TOKEN, REPO, PR_NUMBER, RUNNER_TEMP, GITHUB_OUTPUT,
#      SEMGREP_MAX_TARGETS (default 500).

out="${GITHUB_OUTPUT:-/dev/null}"

all() {
  echo "mode=all" >> "$out"
  [ -z "${1:-}" ] || echo "Semgrep targets: $1; scanning the whole tree."
  exit 0
}

[ "${EVENT_NAME:-}" = "pull_request" ] || all ""
[ -n "${GH_TOKEN:-}" ] || all "no token"
[ -n "${REPO:-}" ] || all "no repository name"
[[ "${PR_NUMBER:-}" =~ ^[1-9][0-9]*$ ]] || all "no pull request number"
[ -n "${RUNNER_TEMP:-}" ] || all "no runner temp directory"
command -v gh > /dev/null 2>&1 || all "gh is not available"

max="${SEMGREP_MAX_TARGETS:-500}"
[[ "$max" =~ ^[1-9][0-9]*$ ]] || all "unusable target limit"

listing="$(gh api --paginate "repos/${REPO}/pulls/${PR_NUMBER}/files?per_page=100" \
  --jq '.[] | [.status, .filename] | @tsv' 2> /dev/null)" \
  || all "could not list the pull request files"
[ -n "$listing" ] || all "the file list is empty"

targets="${RUNNER_TEMP}/semgrep-targets.txt"
: > "$targets" || all "could not write the target list"
texts="${RUNNER_TEMP}/semgrep-doc-texts.txt"
: > "$texts" || all "could not write the target list"
total=0
kept=0
code=0
while IFS=$'\t' read -r status name; do
  [ -n "$status" ] && [ -n "$name" ] || all "unreadable file list"
  case "$status" in
    added | modified | changed | renamed | copied)
      # --include takes a glob: a name with glob characters could match nothing or too much.
      case "$name" in *[\*\?\[\]\{\}\\]*) all "a changed file name has glob characters" ;; esac
      [ -f "$name" ] || all "a changed file is not in the checkout"
      case "$name" in
        *.md | *.rst) printf '%s\n' "$name" >> "$texts" ;;
        *.png | *.jpg | *.jpeg | *.gif | *.ico | *.webp | *.pdf) ;;
        *) code=$((code + 1)) ;;
      esac
      printf '%s\n' "$name" >> "$targets"
      kept=$((kept + 1))
      ;;
    removed) ;;
    *) all "unknown file status" ;;
  esac
  total=$((total + 1))
  [ "$total" -le "$max" ] || all "more than ${max} files changed"
done <<< "$listing"

if [ "$kept" -eq 0 ]; then
  echo "mode=none" >> "$out"
  echo "Semgrep targets: the pull request only deletes files; nothing to scan."
  exit 0
fi
if [ "$code" -eq 0 ]; then
  # Only docs changed. A secret in a markdown file is still a secret: scan the text files
  # with the secrets pack alone. Images and pdf files are not text and are not scanned.
  if [ -s "$texts" ]; then
    cp "$texts" "$targets" || all "could not write the target list"
    echo "mode=secrets" >> "$out"
    echo "Semgrep targets: docs only; the secrets pack over $(wc -l < "$texts" | tr -d ' ') text file(s)."
  else
    echo "mode=none" >> "$out"
    echo "Semgrep targets: the pull request changes only images or pdf files; nothing to scan."
  fi
  exit 0
fi
echo "mode=files" >> "$out"
echo "Semgrep targets: ${kept} changed file(s)."
exit 0
