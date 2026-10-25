# ci_repair

Name the mechanical repair for a CI failure, and how to resubmit it.

The skill does not push. A harness that has the Axiom MCP tool runs the
commands. The resubmit is a push or a rerun, never an update through the
host API.

## Inputs

- `log` — the failing check's log
- `check` — the check name, when there is one

## Output

A pattern, the commands, and `resubmit`.
