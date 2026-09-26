# ADR-047 — Availability-aware CLI dispatcher

**Status:** Accepted (2026-05-28)

## Context

`axi` commands shell out to external dependencies — `git`, the `gh` and
`glab` CLIs, API tokens, and so on. When a dependency is absent the command
discovers this *mid-run*: `axi release` crashed on its first `git` call in a
non-repo (a bare `RuntimeError`), and RIVET's CI monitor returns nothing
when `gh`/`glab` are missing without telling the operator why. The CLI runs
on varied hosts (developer laptops, CI runners, the agent hosts PLINTH and
RIVET run on), so "the dependency might not be here" is the normal case, not
the exception.

We want commands to declare what they need, and the dispatcher to (a) hide
commands it can't satisfy from help, and (b) refuse to run them with a clear
reason + remedy — rather than crash. This should be uniform across *all*
commands and dependencies, not a one-off `git` check.

## Decision

A small **capability** layer plus a per-command **`requires`** declaration:

- **`infra/capabilities.py`** — a `Capability` (name + probe + description)
  and `Availability` (available / reason / remedy). Built-ins: `git`, `gh`,
  `glab`, `gitlab-token`. Probes are cached per process; extensions may
  `register()` their own. (Probes answer "is it here", never mutate.)
- **`requires` on a command** — `CLICommandDef.requires: list[str]`,
  populated from the AEOS manifest (`[[extension.provides]] requires =
  ["git"]`) and surfaced through `discover_cli_commands`. Core commands use
  the `_SUBCOMMAND_REQUIRES` map in `axiom_cli`. Empty = always available.
- **`infra/cli_gating.py`** — resolves names → unmet `(capability,
  availability)` pairs and formats the reason+remedy block. Unknown
  capability names are skipped (forward-compatible; `axi ext lint` is where
  typos get flagged).
- **Dispatcher** — at dispatch time, a command with unmet requirements
  prints the gating block and exits non-zero (the *disable*). At help/
  completion build time, unavailable commands are omitted unless
  `--show-unavailable` / `AXI_SHOW_UNAVAILABLE` (the *hide*).
- **Repo-presence + git-init offer** — `infra/git_setup.ensure_repo_or_offer
  _init` is the interactive remediation for the special "needs a git repo,
  this filespace isn't one" case: silent when already in a work tree, offers
  `git init` interactively, and prints instructions + fails fast (no stdin
  hang) in automation.

## Consequences

- Commands fail *before* doing work, with an actionable message, on hosts
  missing a dependency — no more mid-run `RuntimeError`s.
- Adding a dependency check is declarative: one `requires` entry in a
  manifest, no bespoke guard code per command.
- Help shrinks to what the host can actually run; `--show-unavailable`
  reveals the rest with the reason each is hidden.
- A new external dependency means registering a `Capability` once; every
  command can then require it by name.
- Trade-off: an unmet probe can't be overridden at dispatch today (you
  install the dependency, or fix the probe). An escape hatch can be added if
  a real need appears.
- This is platform-level (Axiom), domain-agnostic — no consumer-specific
  capabilities live here.
