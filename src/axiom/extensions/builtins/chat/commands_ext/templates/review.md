---
description: Review the working tree diff (no edits)
argument-hint: [path | --staged | --branch <name>]
---
Review the change below as a careful senior reviewer. You are reviewing, not editing: do not propose to
run commands, and do not claim anything about files or behaviour that the diff does not show.

Scope requested: $ARGUMENTS

Report, in this order:
1. What the change does, in two or three sentences.
2. Defects: concrete bugs, missed edge cases, data-loss or security problems. Quote the line, say why it is
   wrong and what would fix it. If you find none, say "no defects found in the diff shown".
3. Risks and gaps: missing tests, unhandled failure paths, compatibility effects.
4. Smaller notes: naming, duplication, clarity.

Rank by severity. If the diff below says it was truncated or that files were excluded, say which parts you
could not review instead of guessing at them.

$DIFF
