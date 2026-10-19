#!/usr/bin/env bash
# Point this worktree's tooling at this worktree's source.
#
# The workspace shares one virtualenv across every worktree, so `pip install -e`
# anchors `axiom` to whichever checkout ran it. Every other worktree then
# imports that one: `axi` in your branch runs somebody else's code, and the
# symptom is a verb that "does not exist" or a fix that "did not work".
#
# pytest is already handled — the root conftest exports the same roots
# `pyproject.toml` declares, so subprocess tests exercise their own checkout.
# This is the interactive half, for a shell.
#
# Usage, from a worktree's .envrc (which is gitignored, hence this file):
#
#     source_up_if_exists
#     source ./scripts/worktree-env.sh
#
# Or by hand:  source scripts/worktree-env.sh

_axiom_worktree_root="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"

for _axiom_root in "${_axiom_worktree_root}/src" "${_axiom_worktree_root}/packages/axiom-tests/src"; do
  [ -d "${_axiom_root}" ] || continue
  case ":${PYTHONPATH}:" in
    *":${_axiom_root}:"*) ;;
    *) PYTHONPATH="${_axiom_root}${PYTHONPATH:+:${PYTHONPATH}}" ;;
  esac
done
export PYTHONPATH

unset _axiom_root _axiom_worktree_root
