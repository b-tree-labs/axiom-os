# Prompts for your agent

Short prompts to paste into your coding agent, one per step of the loop in
[Your first week](../guides/your-first-week.md). They work in any program and any domain. Replace
each `<placeholder>` before you paste. A product built on Axiom may extend this page with its own
repositories, guards and commands; if it does, use that version.

Every prompt points the agent at the repository's own rules rather than restating them, so the
prompts stay right when the rules change. The rules are in `AGENTS.md`, the
[Part 3 agent reference](developer-workflow.md) and the
[Part 4 agent reference](ship-and-operate-reference.md).

## Set up my source clone

```text
Set up my source clone. Keep anything my product's Parts 1 and 2 installed (its command, my site
binding and key, my data kit); uninstall nothing. Check `git config user.name` and `user.email`
(my GitHub noreply address, for commits). Clone beside my other repositories and follow the
contributor quickstart, steps 2 to 4: the editable install in the clone's .venv, the hooks,
a test database, the fast tests. Then check `which -a <command>`: the clone's .venv copy must
come first. Report each step's result.
```

## Start my session

```text
Start my session, and do the upkeep yourself. In my clone: fetch and fast-forward main if it is
clean (never discard my work); if setup files changed, rerun the setup. Activate the clone's
.venv and load its environment file; if a newer release of a dependency I install as a package
is out, upgrade it in the .venv. Check `which -a <command>` (the clone's first; leave other
installs alone) and `--version`. Report what is running: `dev status` and its database, the site
binding (`site whoami`), and my keys (names only). Read AGENTS.md, and my ticket <ticket> if I
name one. Tell me the state, anything stale or wrong, and the first step. Change no code.
```

## Update my setup

```text
Update my setup: fetch and fast-forward main if clean, rerun the setup (the quickstart's steps,
or the product's setup command), upgrade released dependencies in the .venv, reload the
environment, check `which <command>` and `--version`, and report what changed.
```

## Which mode am I in?

```text
Which mode am I running: the bare platform, the bare product, or the product with a site
applied? Check, do not guess: the dev node's status and the address it printed, which checkout
and which installed versions the running command comes from, the product's site binding
(whoami), and the wordmark on the page. Remember that a site binding changes which node's data I
read, not the look. Report each fact with the command that showed it, then your answer.
```

## Brief me on a ticket

```text
Brief yourself for <ticket>. Read the contract whole: its acceptance list, its screens folder
(each screen's README and shots), and the reuse map or design catalogue it names; list every
existing component, route, skill or table you will extend, and build nothing that already exists.
List the repositories it touches, in the order their pull requests must merge. Rules: write the
acceptance tests first, tagged with their ids; one worktree per branch; stage files by name;
no Co-Authored-By or "Generated with" lines; run each repository's naming guard before pushing.
Stop and ask me at every point the ticket marks for a maintainer, and before anything in
"What needs a person". Reply with your plan; change nothing yet.
```

## Write the acceptance tests first

```text
Write the acceptance tests for <ticket> and nothing else: one test per acceptance row, named or
tagged with its id, in the place the ticket names. Run them and show me each one failing, with
the reason. A test that passes before the code exists proves nothing: if one does, tell me why
and fix the test. Do not write any implementation yet.
```

## Check my change against the design

```text
Check my change against the design screens for <ticket>. Open each screen in its folder, take a
screenshot of the running page in the same state, and compare them side by side: layout, words,
states (empty, loading, error), and the components used. Follow the repository's guide for
implementing a design. List every difference, and say for each whether it is a bug, a missing
piece, or a deliberate deviation the ticket allows. Fix nothing until I have read the list.
```

## Before I push

```text
Before I push, run the checks the pre-push hook and CI will run, and report each result:
lint as CI runs it; the naming guard; the ADR lint if an ADR changed; `axi context check` if
AGENTS.md changed; if a skill was added or changed, `tests/infra/test_skill_chain_lint.py` and
`python scripts/skill_chaining_debt.py --check` (regenerate the report if stale); the tests my
change can affect; then the hook by hand. Check that no commit carries an attribution line:
`git log origin/main..HEAD --format=%B | grep -i -E 'co-authored-by|generated with'` prints
nothing. Never use --no-verify. If main is red, stop and tell me.
```

## Open the pull request

```text
Open a draft pull request for this branch. Title: `type(scope): what changed`. Body:
## Why  (the ticket and the problem, in two or three sentences)
## What changed  (one line per file group)
## Acceptance  (each acceptance id and the test that proves it)
## Tests run  (the commands and their last lines)
## Not done  (what the ticket asks that this leaves out, and why)
No attribution lines. Then request <reviewer> as reviewer, and only then mark it ready.
Show me the URL. Do not merge.
```

## Respond to review

```text
Read every review comment on pull request <number>, including inline ones. For each, say:
the comment, whether you agree, and the change you propose, or why not. Wait for my yes. Then
make the changes as new commits on the same branch (never force push), run "Before I push",
push, and reply to each comment with what changed and the commit. Do not resolve threads the
reviewer opened; let them.
```

## I'm stuck: diagnose

```text
I'm stuck: <what I ran, and what I expected>. Diagnose before fixing. Collect: the exact command,
the first error in full, which checkout and versions are running, the doctor command's output
(the product's dev doctor where one exists; `axi doctor` otherwise), and free disk space.
Run the failing test alone on a clean main worktree to decide whether it is ours. Match it
against "Known traps" and "When something goes wrong" in the agent reference, and the FAQ.
Tell me the cause and the fix you propose. Two failed tries at one fix means stop and explain.
```
