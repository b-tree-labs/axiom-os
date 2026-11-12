# Working with a coding agent

For a new developer who will drive a coding agent to build features in Axiom. You do not need to
be an expert programmer. You do need to stay accountable: the agent types, and you shepherd,
test and own every line. Any agent works. Claude Code appears below as one example, not an
assumption. Read this after the [contributor quickstart](contributor-quickstart.md); the full
order is in the [guides index](README.md).

## 1. Install Axiom and connect your agent

Do the [contributor quickstart](contributor-quickstart.md) through step 3 first. On Windows, work
inside WSL and keep your clones under your Linux home (`~`), not under `/mnt/c`.

Connect your agent to Axiom's tools, with your clone's environment active so that the agent uses
your code ([what it writes, and the stale server trap](run-from-source.md#your-coding-agents-mcp-server)).
Preview the change first, because the second command writes to your agent's configuration:

```sh
axi mcp install --dry-run
axi mcp install
```

`axi mcp install` finds the agent programs on this computer and writes each one's settings. Add
`--tool <name>` to target one program. MCP is how an agent calls Axiom's skills as tools. If
`axi mcp` says `No module named 'mcp'`, your install lacks that extra: `pip install -e ".[mcp]"`.
Restart the agent afterwards, because it reads its tools when it starts.

### Other MCP servers to connect

Axiom's MCP server is the one your agent needs. A few others make development smoother, and some
should stay off. Every MCP server you add can act with the credentials you give it, so connect
only what the work needs.

| Server | Status | What it is for |
|---|---|---|
| Axiom (`axi mcp install`, above) | Required | Data reads through gold, the program's priorities, CI and release status, the vault. Calls carry your identity and the node's access rules. |
| GitHub (the official GitHub MCP server) | Recommended | Pull requests, reviews, issues and CI logs as tools. Give it a fine-grained token limited to the repositories you work in. The `gh` command does the same job without MCP, so either is fine. |
| Context7 | Recommended | Current documentation for libraries and frameworks, so the agent does not code from what it remembers. |
| Playwright or Chrome DevTools | When you work on screens | Opens and screenshots pages so the agent can check a change it made to a screen. See [Implementing a design](implement-a-design.md). |
| A raw database or Postgres server | Do not connect | It reads tables directly and skips gold and the access rules. Data goes through Axiom. |
| Servers that need a broad personal token (email, calendar, cloud administration) | Do not connect for development | Far more reach than writing code needs. |

## 2. Tell the agent the rules: AGENTS.md and context

`AGENTS.md` at a repository's root is the rule book for people and agents. Other tools get
generated copies, such as `GEMINI.md` and `.github/copilot-instructions.md`. Edit only
`AGENTS.md`, then regenerate the copies:

```sh
axi context sync
axi context check
```

`axi context init` scaffolds an `AGENTS.md` in a repository that has none. Axiom already has one,
so you will not need it here. A repository's own `AGENTS.md` always beats your personal agent
settings. If your agent proposes something that contradicts it, stop and point at the rule.

## 3. One worktree per branch

A worktree is a second folder on a second branch of the same clone. It keeps two pieces of work
from mixing. Give every task its own worktree, and never make two worktrees for one branch:

```sh
git fetch origin
git worktree add ../axiom-wt-my-task -b fix/short-description origin/main
cd ../axiom-wt-my-task
```

If two worktrees share one virtual environment, `axi` runs whichever checkout installed it, not
the folder you are standing in. [One environment across worktrees](run-from-source.md#one-environment-across-worktrees)
has the fixes. After the pull request merges, remove the worktree with
`git worktree remove ../axiom-wt-my-task`.

Never start in someone else's worktree, and never let two agents share one.

## 4. The test-first loop

This is the loop to hold your agent to. It is a house rule, not a preference.

1. **Say what you want in a sentence.** Name the behaviour and the extension it belongs in.
2. **Ask for the failing test first, and nothing else.** A CLI verb needs a test that runs the
   command in a subprocess and checks its output.
3. **Run the test yourself and watch it fail.** Read the failure. It must fail for the reason you
   expect. A test that has never failed proves nothing.
4. **Ask for the smallest code that passes.** Logic goes in a skill function, not a command-line
   handler.
5. **Run it again and watch it pass.** Then run the extension's tests:
   `python -m pytest src/axiom/extensions/builtins/<ext>/tests -q`.
6. **Commit, then repeat.** Small commits are easier to review and easier to undo.

If a test keeps failing, stop the agent, ask what it thinks is happening and read the real error
yourself. Retrying without a new idea wastes time and money.

## 5. What runs before a push, and what to do when it is red

`git push` runs `scripts/hooks/pre-push` on your computer. Install it with `make hooks`. It does
these things, in order:

| Step | What it proves |
|---|---|
| Checks `main` on GitHub | `main` is not already red |
| Context files and ruff | Generated files match `AGENTS.md`, and lint is clean. It may repair them, commit the repair and stop; run `git push` again |
| Cheap guards | No stray root folder, no customer name, no forbidden term in the public surface |
| ADR number lint | No two ADRs share a number |
| Document checks | A push that touches only documents runs these and stops here |
| The tests your change can affect | Uses a map, `.testmondata`, of which tests touch which lines |

The map is why your first push in a clone can take many minutes and later pushes are short.
When the map cannot narrow the run, the hook runs only the test files your branch changed and leaves the full suite to CI. `AXI_HOOK_FULL=1 git push` runs every unit test on your machine. If you run the map by hand, write
`python -m pytest --testmon --testmon-forceselect`. Plain `--testmon` selects nothing here,
because `pyproject.toml` sets `-m` in `addopts`, and testmon turns its selection off when it sees
`-m`.

When the hook is red:

1. **Read the first failure.** Do not rerun yet.
2. **Decide whose it is.** Run the failing test alone on a clean `main` worktree. If it fails
   there too, your change did not cause it.
3. **If it is yours,** fix the code and push again. Ask your agent to explain the failure first.
4. **If `main` is red,** the hook refuses until the commit has a `Bypass-Reason:` trailer. Tell the
   platform owner. Add the trailer only when you can write down why this push must go now.
5. **If the database step fails,** start the Postgres container from the quickstart. A missing
   database is never a reason to skip the hook.
6. **If it is not yours and you cannot fix it,** stop and report the test name and the log.

A green hook proves the unit suite only. Integration, migration and install-path tests run on
`main` after the merge, so a green pull request can still turn `main` red. If yours does, fixing it
is your next task (`AGENTS.md`, "What the pre-push gate cannot see").

## 6. Naming guards

Axiom is a platform for any domain, so its files must not name a domain, a customer or a
deployment. Run the guard before you push:

```sh
python -m pytest tests/test_axiom_names_no_domain.py -q
```

If it names your file, rewrite the text in the platform's own terms. Use a generic example such
as `example-site`. Never widen the guard, and never add your file to its debt list. Agents copy
names from whatever they have read, so check this on every change. A repository built on Axiom
has its own guard: read its `AGENTS.md`.

## 7. How to review an agent's diff

An agent's mistakes look as confident as its good work, so your review is the safety step.

1. **Read it.** Run `git diff origin/main...HEAD` and read every line, including the tests.
2. **Run it.** Run the tests. Then use the feature the way a person would: the command, the page
   or the tool. Passing tests are not the same as working.
3. **Ask why.** For each change, ask why it is needed and what else was considered. Ask what would
   break if the line were removed. Vague answers usually mean a guess.
4. **Explain every line.** If you cannot explain a line to a colleague, you are not ready to open
   the pull request. Ask until you can, or remove the line.

Look hard for these common faults:

- A test that cannot fail, such as one that checks a mock instead of the real behaviour.
- A skipped, deleted or weakened test or guard, used to make something pass.
- A function, flag or file that does not exist. Open it, or run `--help`.
- Work you did not ask for: drive-by cleanups, renames or new dependencies.
- A value hard-coded where configuration, the vault or a gold object belongs.
- A generated file edited by hand, or a change to an accepted ADR. Accepted ADRs are immutable.

## 8. Landing a pull request

Open every pull request as a draft: `gh pr create --draft --fill`. Write why the change exists,
which tests you ran and what you tried by hand. Keep at most three open at once. Do not press
"Update branch" while checks are running; each press costs a full CI run.

| Repository | What gates a merge | Who merges |
|---|---|---|
| Axiom | `Lint` and `Unit Tests (3.13)` must be green. No review is required by branch protection. There is no merge queue | **RIVET's lander can merge for you.** See below |
| `axiom-appkit` | No branch protection today. Its `AGENTS.md` lists the checks and the design contract | The owner. The lander only reports |
| A repository built on Axiom | Its own rules, in its own `CONTRIBUTING.md` | Read that file. Do not assume this table |

**RIVET's lander in Axiom.** RIVET is the agent that handles CI and releases. A workflow merges any
pull request that is green, is not a draft, has no pending human review, has no changes
requested and touches no protected path. It runs after every merge to `main` and every six hours.
Protected paths are listed under `[human_paths]` in `.axiom/release-lander.toml`: for example auth,
migrations, `conftest.py`, workflows and dependency manifests. A pull request touching one waits
for a person. So in Axiom, **marking a pull request ready can land it without anyone reading it.**
Before you press "Ready for review", request a human reviewer with
`gh pr edit --add-reviewer <login>`. A pending review holds the lander, and so does a draft.

A draft also skips the unit suite, so mark it ready to run the suite. Once it is reviewed,
`gh pr merge --squash --auto` merges when the checks pass, so you never need to watch CI. Your
branch does not have to be up to date with `main`.

There is no merge queue: `.github/rulesets/main-merge-queue.json` describes one, but it is not
applied to the repository. Integration, migration and install-path suites run on `main` after the
squash merge, so a pull request green on everything visible can still turn `main` red. A red run
there is yours to fix first. Read it as information, not as a flake.

## 9. What never to do

- **`git add -A`.** It sweeps in whatever is lying around. Name each file.
- **Push to `main`.** Every change goes through a pull request.
- **Merge in someone else's repository.** Wait for its owner, even if you have the button.
- **Put a secret in a file, a command line, an environment variable, a script or the macOS
  Keychain, or in a transcript.** Use `axi vault set my-credential-name`, which prompts for the
  value. `axi vault list` prints names and metadata, never values. If one leaks, run
  `axi vault exposed`; deleting the message does not fix it.
- **Remove a container, network or volume you did not create.** Ask first, even when it looks
  abandoned. Do not stop running services either.
- **Use `--no-verify` without a written reason.** The reason goes in the pull request. A failure
  you do not understand is not a reason.
- **Force push.** If a branch needs rewriting, publish it again on a new branch with `-r2` on the
  end of its name, open a new pull request and close the old one.
- **Let an agent do any of these for you.** Read each command that touches shared state first.

## 10. No attribution lines

Commits carry no `Co-Authored-By:` line for an AI tool, and commits and pull requests carry no
"Generated with" line. You are the author, and you are accountable. Many agents add these by
default, so turn that off once.

- **Claude Code:** in `~/.claude/settings.json`, set `"attribution": {"commit": "", "pr": ""}`.
- **Aider:** pass `--no-attribute-co-authored-by`, or set it in `.aider.conf.yml`.
- **Any other agent:** look in its settings for attribution or co-author, and tell it the rule.

Check before you push. This should print nothing:

```sh
git log origin/main..HEAD --format=%B | grep -i -E 'co-authored-by|generated with'
```

To fix a commit you have not pushed, run `git commit --amend`.

Next: building the logbook feature? Read [Before you build on logbooks](README.md#before-you-build-on-logbooks).
