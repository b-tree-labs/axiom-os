# Part 3 agent reference: the developer workflow

The lookup page behind [Part 3: Build a feature](../guides/build-a-feature.md). It is written for
the coding agent that a contributor drives, and for the contributor who wants the detail.
`AGENTS.md` routes agents here. Facts come from `scripts/hooks/pre-push`,
`.github/workflows/ci.yml`, `.axiom/release-lander.toml`, `pyproject.toml` and the `Makefile`; if
this page and those files disagree, the files win, and this page needs a fix.

Use this page to act. Explain to the person in plain words rather than pasting it.

## Contents

1. [Commands](#commands)
2. [The gates](#the-gates)
3. [Test kinds](#test-kinds)
4. [Commits and pull requests](#commits-and-pull-requests)
5. [What needs a person](#what-needs-a-person)
6. [Known traps](#known-traps)
7. [When something goes wrong](#when-something-goes-wrong)
8. [Before every push](#before-every-push)
9. [When the person says](#when-the-person-says)

## Commands

| Task | Command |
|---|---|
| New worktree for a task | `git fetch origin && git worktree add ../axiom-wt-<task> -b <type>/<short-name> origin/main` |
| Install or refresh the hooks | `make hooks` (copies `pre-push` and `commit-msg` into `$(git rev-parse --git-common-dir)/hooks`) |
| Point a shell at this worktree's source | `python scripts/setup_worktree_envs.py` |
| Run one extension's tests | `python -m pytest src/axiom/extensions/builtins/<ext>/tests -q` |
| Run one test | `python -m pytest path/to/test_file.py::test_name -q` |
| Run what CI's unit job collects | `python -m pytest -n auto` (testpaths and `-m` come from `pyproject.toml`) |
| Run the tests a change can affect | `python -m pytest --testmon --testmon-forceselect` (plain `--testmon` selects nothing, because `addopts` carries `-m`) |
| Lint, as CI runs it | `ruff check src/ tests/ conftest.py --select E,F,W --ignore E501` (`make lint` checks `src/` only) |
| Naming guard | `python -m pytest tests/test_axiom_names_no_domain.py -q` |
| Next free ADR number | `python scripts/lint_adr_numbers.py --next` |
| ADR lint | `python scripts/lint_adr_numbers.py` |
| Regenerate the agent context copies | `axi context sync`, then `axi context check` |
| Run the hook by hand, main clone | `.git/hooks/pre-push < /dev/null` |
| Run the hook by hand, worktree | `"$(git rev-parse --git-common-dir)/hooks/pre-push" < /dev/null` |
| Every unit test in the hook | `AXI_HOOK_FULL=1 git push` |
| Main's CI state | `gh run list --branch main --workflow ci.yml --limit 5` |
| Draft pull request | `gh pr create --draft --fill` |
| Request a reviewer, then mark ready | `gh pr edit --add-reviewer <login>`, then `gh pr ready` |
| Merge when checks pass | `gh pr merge --squash --auto` |
| Docs link check | `python -c "from pathlib import Path; from axiom.extensions.builtins.hygiene.doc_hygiene import check_broken_links as c; print(*c(Path('.')), sep='\n')"` |
| Render a diagram | `python scripts/diagrams/<name>.py` (needs Chrome and Pillow; see `docs/assets/diagrams/README.md`) |

## The gates

In the order a change meets them.

### Lint (ruff)

- **Runs:** the hook, and CI's required `Lint` job.
- **Catches:** pyflakes and pycodestyle errors (`E`, `F`, `W`, line length ignored): unused or
  undefined names, syntax errors, whitespace.
- **Common failures:** an unused import an agent left behind; `F821` undefined name after a
  rename; trailing whitespace.
- **Fix:** `ruff check src/ tests/ conftest.py --select E,F,W --ignore E501 --fix`. With a clean
  tree the hook applies `--fix` itself, commits `chore: apply the pre-push repair` and stops.
  Run `git push` again. With a dirty tree it only prints the command.

### Context files

- **Runs:** the hook, and the `Lint` job (`python -m axiom.extensions.builtins.context.cli check`).
- **Catches:** generated copies that no longer match `AGENTS.md`: `.cursor/rules/axiom.mdc`,
  `.junie/guidelines.md`, `.github/copilot-instructions.md`, `GEMINI.md`. `CLAUDE.md` is a symlink
  to `AGENTS.md`. The generator is `src/axiom/extensions/builtins/context/generators.py`.
- **Common failure:** `AGENTS.md` edited without a sync, or a copy edited by hand.
- **Fix:** `axi context sync` (or `PYTHONPATH=src python -m axiom.extensions.builtins.context.cli sync`)
  and commit the copies with `AGENTS.md`. Never hand-edit a copy. Note that the `.mdc` copy is
  not documentation to the hook or CI (only paths under `docs/` or ending in `.md` are), so any
  `AGENTS.md` change runs the real test selection on push and the full unit job in CI.

### Cheap guards

- **Runs:** the hook, before the database and the suite; the same tests run again in the unit job.
- **Checks:** `tests/test_repo_hygiene.py::TestRootDirPolicy::test_no_unexpected_root_dirs` (no
  new top-level folder), `tests/test_mirror.py::test_public_surface_has_no_forbidden_terms` (no
  forbidden term in what the public mirror publishes), and
  `tests/test_axiom_names_no_domain.py::TestNoModuleNamesACustomer::test_not_one` (no customer
  or deployment id anywhere).

### Naming guard

- **File:** `tests/test_axiom_names_no_domain.py`. The full file runs in the unit suite and in the
  hook's document checks.
- **Catches:** a customer or deployment id anywhere in the repository (no debt list), and a
  domain noun in shipped code (a named debt list that only shrinks).
- **Common failure:** an agent copied an example from a product repository it had read.
- **Fix:** rewrite in the platform's terms (`example-site`, "a domain extension"). Never widen
  `DOMAIN_NOUNS`, never add a file to the debt list. If you clean a listed file, delete its line,
  because the guard fails on a listed file that is already clean.

### ADR numbering

- **Runs:** the hook, the hook's document checks (`tests/test_adr_numbering.py`,
  `tests/test_lint_adr_numbers.py`), and the `Lint` job, including docs-only pull requests.
- **Catches:** two ADRs with one number.
- **Fix:** renumber yours with `python scripts/lint_adr_numbers.py --next`. Exit 127 means the
  linter did not run (no interpreter), not a collision. An accepted ADR is never edited; write a
  new one that supersedes it.

### The pre-push hook

Source `scripts/hooks/pre-push`, installed by `make hooks`. In order:

1. **Main's state.** If the last `ci.yml` run on `main` failed, every pushed commit needs a
   `Bypass-Reason:` trailer (see [What needs a person](#what-needs-a-person)).
2. **Stale artifacts.** Clears `__pycache__` left by another branch.
3. **Context files and ruff**, repaired when the tree is clean (above).
4. **Cheap guards** (above).
5. **ADR lint.**
6. **Docs-only push:** if every changed path is under `docs/` or ends in `.md`, it runs the ADR,
   repo-hygiene and naming tests and stops.
7. **Throwaway database.** Creates an empty `axiom_hook_*` database on the server named by
   `AXIOM_PG_TEST_URL` (or a reachable local one), and drops it on exit.
8. **Tests.** The testmon map (`.testmondata`) selects the tests the change can affect. When it
   cannot narrow the run, the hook runs only the test files the branch changed, or none, and
   leaves the full suite to CI. `AXI_HOOK_FULL=1` runs everything. Runs with workers take a
   machine-wide lock (`~/.axi/locks/pre-push-sweep`), so two sweeps never run at once.

Useful variables: `AXI_HOOK_WORKERS` (default 4, `0` serial), `AXI_HOOK_FULL=1`,
`AXI_OVERRIDE_REASON`, `AXI_HOOK_NO_POSTGRES_REASON` (logged; database tests then fail).

**What it cannot see:** `pyproject.toml` deselects `integration`, `federation_lifecycle`,
`install_path` and `classroom_e2e`. A green hook proves the unit suite only.

### CI (`.github/workflows/ci.yml`)

| Job | When | What it proves |
|---|---|---|
| `Changed Paths` | Every run | Decides docs-only (`docs/` or `*.md` only) and draft. Both make later jobs report success without running |
| `Lint` | Every run | Ruff, context files, ADR numbers. **Required** |
| `Smoke` | Code changes | Imports compile, every test file collects, cost-control tests; web-app smoke for web-app paths |
| `Unit Tests (3.13)` | Code changes, non-draft | The unit suite on an empty Postgres; on a pull request, narrowed by the testmon map. **Required** |
| `Container Smoke (stranger path)` | Code changes | The wheel installs and runs in a bare `python:3.13-slim` |
| `Integration Tests`, `Migration Tests`, `Install-Path Tests` | `main`, tags, merge groups only | The deselected suites. A pull request never runs them |
| `Build Wheel`, `Prerelease Wheel Check`, `Publish Release` | After the above; publish on a tag | The release artifacts |

Other workflows on a pull request: `Semgrep`, `pip-audit`, and the RIVET lander. Branch
protection requires only `Lint` and `Unit Tests (3.13)`. There is no merge queue applied to the
repository, so the integration, migration and install-path suites first run **after** the squash
merge. If your merge turns `main` red, fixing it is your next task.

**Common CI failures:** a test that passed locally fails in the full suite (the hook ran a
narrowed selection); a draft shows green because nothing ran. There is no generated report to
regenerate before pushing: the skill-chaining debt report is rendered in CI (the
`skill-chaining-debt` artifact of `Unit Tests`) and never committed (#1774).

### The lander (RIVET)

- **Workflow:** `.github/workflows/rivet-lander.yml`, on every pull request event, hourly and
  every six hours. Config: `.axiom/release-lander.toml`.
- **Merges** a pull request that is green, not a draft, has no pending human review, no
  changes requested, and touches no protected path.
- **Protected paths** (`[human_paths]`): behaviour, pricing, security and auth, secrets,
  migrations, schema, `.sql`, `conftest.py` and test setup, workflows, and dependency manifests
  (`pyproject.toml`, lock files, `package.json`, Dockerfiles, compose files). These wait for a
  person.
- **Approval:** an approved review is not pending, so an approved, green pull request lands.
  `gh pr merge --squash --auto` does the same once checks pass; use either.
- **Timing:** it runs on pull request events and on its schedule, so a green pull request can
  wait up to an hour.
- **So:** request a reviewer before `gh pr ready`. A pending review or a draft holds the lander.
  `AXIOM_LANDER_KILL=1` stops it for everyone; only the owner sets it.

## Test kinds

| Kind | Marker or place | Required when | Runs where |
|---|---|---|---|
| Unit | Next to the code: `src/axiom/extensions/builtins/<ext>/tests/`; cross-cutting in `tests/` | **Every change.** Written first, seen to fail, then made to pass | Hook, CI unit job |
| Database (unit) | Unit tests that use `axiom.infra.db.session_for` | The code reads or writes Postgres | Hook (throwaway database), CI |
| Integration | `@pytest.mark.integration`; `tests/integration/` | The code talks to an external service or needs runtime config | CI on `main` only |
| Migration | `tests/test_migrations_expand_before_they_contract.py`, `axiom.infra.deploy.migration_guard` | You add a migration. Expand only; a drop, rename or NOT NULL ships later, marked `# contract:` | CI on `main` |
| Acceptance | `@pytest.mark.acceptance("ROW-ID")`, or a test named after the row | The ticket names a spec row. See [the acceptance harness](../guides/acceptance-harness.md) | Hook, CI; reported by `axi app acceptance` |
| Contract | A test in the serving repository that mirrors the client, for example `chat/tests/test_dual_drive.py` mirrors appkit's `viewContext.ts` | Your change adds or changes an API another repository calls, or calls one. Example: a new route the web app (appkit) calls needs unit tests for the route and a contract test of the shape appkit reads | Hook, CI |
| Visual | appkit: `npm --prefix tools/visual run visual -- --screen <name>` | You change a screen. See [Implement a design](../guides/implement-a-design.md) | Locally, in appkit |
| Smoke | `tests/test_ci_cost_controls.py`, `tests/test_every_test_file_is_collected.py` | Never written per change; keep test files under `testpaths` | CI `Smoke` |
| Install path | `@pytest.mark.install_path` | Packaging or install changes (needs Docker and network) | CI on `main` |
| Evals | `tests/evals/`, `tests/promptfoo/` | Changes to retrieval, routing or prompts (Part 5) | By hand, CI where wired |

A test file outside `testpaths` in `pyproject.toml` never runs, and
`tests/test_every_test_file_is_collected.py` fails. A test that cannot fail proves nothing: check
it fails before the code exists.

## Commits and pull requests

- **One worktree per branch.** Never two agents in one worktree, never two worktrees on one branch.
- **Branch names:** `<type>/<short-name>`, with a type such as `feat`, `fix`, `docs` or `chore`.
- **Commit subject:** `type(scope): what changed`, for example `docs(guides): ...`. The body says
  why. Sign off with `git commit -s` (CONTRIBUTING requires a `Signed-off-by:` line).
- **Stage by name.** Never `git add -A` or `git add .`.
- **No attribution lines.** No `Co-Authored-By:` for an AI tool, no "Generated with". Check:
  `git log origin/main..HEAD --format=%B | grep -i -E 'co-authored-by|generated with'` prints
  nothing.
- **Author identity:** the contributor's GitHub noreply address; GitHub refuses a push that would
  publish a private email.
- **Pull requests:** open as a draft, describe why and which tests ran, at most three open at
  once, request a reviewer before marking ready, then `gh pr merge --squash --auto`. Do not press
  "Update branch" while checks run; each press costs a full CI run. The branch need not be up to
  date with `main`.
- **Never force push.** To rewrite a pushed branch, publish it again as `<branch>-r2`, open a new
  pull request and close the old one.
- **Docs:** a pull request that changes a subsystem's behaviour updates its spec in the same pull
  request. Diagrams are PNGs drawn with `scripts/diagrams/diagram_kit.py`, never Mermaid.

## What needs a person

Stop and ask the contributor (and they may need the platform owner) before any of these:

- Pushing onto red `main` (a `Bypass-Reason:` trailer via `AXI_OVERRIDE_REASON`). The one
  standing exception is a push that only fixes the failure turning `main` red; `AGENTS.md` lists
  its five conditions.
- `--no-verify`, or `AXI_HOOK_NO_POSTGRES_REASON`. A missing database is never a reason.
- Touching a protected path (above), an accepted ADR, auth, secrets or a migration.
- Merging, or approving, in a repository the contributor does not own.
- Marking a pull request ready without a reviewer requested.
- Tagging, releasing or deploying.
- Removing a container, network, volume or database you did not create, or stopping a service.
- Anything with a secret. Secrets go in `axi vault set <name>` (it prompts); never print one.
- Widening a guard, adding to a debt list, skipping or weakening a test.

## Known traps

| Trap | Symptom | Fix |
|---|---|---|
| Disk nearly full | State snapshot and backup tests fail with `DiskPressureError` or `free space ... floor is ...`. The default floor is 5% of the disk free (`min_free_fraction = 0.05` in `axiom/infra/backup_core.py`) | Free space to above about 5%, then rerun. Not your code |
| Timing tests under load | Tests that measure time (for example `test_a_reconnect_flood_*`) fail on a busy machine | Rerun the test alone. If it passes alone, it was load. Do not mark it flaky without an issue |
| Raw `engine_for()` connection in a test | `relation "..." does not exist` that passes alone and fails under xdist: the pooled connection carries another extension's `search_path` | Use the extension's `session_scope()` or `axiom.infra.db.session_for("<ext>")`, which sets the path per transaction. `tests/infra/test_nothing_opens_a_raw_extension_connection.py` refuses a new caller, and the provider resets the path whenever a connection goes back to the pool (#1778) |
| Hooks are shared across worktrees | Every worktree of a clone uses one hooks folder. Editing `scripts/hooks/pre-push` on a branch does not change the installed hook; running `make hooks` from a branch installs that branch's hook for every worktree | Run `make hooks` from an up-to-date `main` worktree. The hook warns when the installed copy differs from its source |
| `.git/hooks/pre-push: Not a directory` | You are in a worktree, where `.git` is a file | Use the worktree command in [Commands](#commands) |
| Running another branch's code | A new verb "does not exist", or a fix "did not work" | `python scripts/setup_worktree_envs.py`; see [Run from source](../guides/run-from-source.md#one-environment-across-worktrees) |
| appkit `BUILT_FROM` stamp | appkit's `test_the_bundle_was_built_from_the_source_beside_it` fails after any change under `frontend/` (except `*.test.ts`), or after a rebase onto new frontend code | In appkit, `cd frontend && npm run build`, which re-stamps `src/axiom_appkit/static/BUILT_FROM`; commit the bundle and the stamp together. On a rebase conflict in `BUILT_FROM`, rebuild; never hand-merge the hash. Check with `python scripts/stamp_bundle.py --check` |
| Stale MCP server | The agent's Axiom tools run old code | Restart the agent; see [Run from source](../guides/run-from-source.md#your-coding-agents-mcp-server) |
| First push is slow | The hook builds the testmon map | Expected once per clone; later pushes are short |
| Red `main` | The hook refuses the push | Not your change. Tell the contributor; wait, or follow [What needs a person](#what-needs-a-person) |

## When something goes wrong

1. **Read the first failure.** Do not rerun yet.
2. **Decide whose it is.** Run the failing test alone on a clean `main` worktree
   (`git worktree add ../axiom-wt-main-check origin/main`). If it fails
   there too, your change did not cause it.
3. **Match it** against [Known traps](#known-traps) and the gate's common failures above.
4. **If it is yours,** explain the failure to the contributor, then fix the code.
5. **If it is not yours and you cannot fix it,** stop and report the test name and the log.

Retrying without a new idea wastes time and CI money. Two failed attempts at the same fix means
stop and explain.

Next: the [Part 4 agent reference](ship-and-operate-reference.md) (releases, keeping `main`
green, deploy and rollback, operating). Prompts a contributor pastes at each step:
[agent-prompts.md](agent-prompts.md).

See also: [FAQ](faq.md), [Working with a coding agent](../guides/working-with-a-coding-agent.md),
[Contributor quickstart](../guides/contributor-quickstart.md),
[Run from source](../guides/run-from-source.md).

## Before every push

Run these yourself before every push, from the clone's own activated environment; nobody has to
ask. Report each result.

1. Lint as CI runs it: `ruff check src/ tests/ conftest.py --select E,F,W --ignore E501`.
2. The naming guard: `python -m pytest tests/test_axiom_names_no_domain.py -q`.
3. If an ADR changed, `python scripts/lint_adr_numbers.py`; if `AGENTS.md` changed,
   `axi context sync` then `axi context check`.
4. If a skill was added or changed, `python -m pytest tests/infra/test_skill_chain_lint.py -q` and
   `python scripts/skill_chaining_debt.py --check`.
5. The tests the change can affect: `python -m pytest --testmon --testmon-forceselect`.
6. No attribution line: `git log origin/main..HEAD --format=%B | grep -i -E 'co-authored-by|generated with'`
   prints nothing.
7. `main` is green: `gh run list --branch main --workflow ci.yml --limit 3`. If it is red, stop.
8. Push through the hook. Never `--no-verify` without the person's yes.

## When the person says

People run their day with short sentences. Each is an instruction: do all of it, without being
told the detail. In a workspace folder (one folder holding several clones; its `AGENTS.md` lists
them), do each step in every clone. A product built on Axiom may add its own commands to these
steps in its `AGENTS.md`; follow that where it exists.

**"Get me oriented."** Change nothing except fast-forwarding a clean `main`; ask nothing until the end.
1. Environment: activate the clone's `.venv` and load its environment file; `which -a <command>`
   (the clone's own copy first) and `--version`; where the platform is installed editable, that
   it imports from its clone; whether the dev node is up and on which database. In each clone,
   `git fetch -q` and how far `main` is behind `origin/main`; fast-forward only a clean `main`.
2. What changed since the person last worked: commits on `origin/main` since their last commit
   (`git log -1 --author="$(git config user.email)"`) or the last 24 hours; with `gh` signed in,
   their pull requests with review and CI state (`gh pr list --author @me`), and issues assigned
   to them or mentioning them.
3. Unfinished work in every clone: uncommitted changes, stashes, branches ahead of `origin/main`,
   commits not on their upstream (unpushed), open pull requests, extra worktrees.
4. Priorities: the program tools if present (`axi program status`, or the `axiom_program__*`
   tools); the program's tickets folder (its START-HERE and index order, each ticket's needs).
5. Answer briefly: Environment; Changed since <when>; Unfinished (each item, repository, branch);
   then ONE recommendation with why: "Finish <X> first" if anything is unfinished, and
   "then next: <ticket>".

**"Pick up where we left off."** (or "What's next?") First run every "Get me oriented" check
yourself, without reporting them, unless this session already did; then take the item it
recommends. For that item, unfinished or new, give all of these: its contract; the PRD it serves
and the phase or milestone it belongs to (name it); the plan if one exists and where it stands:
what is done, what is in progress (branches, commits, uncommitted edits), which acceptance tests
exist and pass, which pull requests merged. End with the next concrete step. Change no code.

**"Is it ready to build?"** For the current ticket, a path or "missing" for each: design (its
screens folder or design section, or "no screen"); the PRD requirement; an ADR for every
architecture choice (new storage, service, dependency or public interface, or a change across
repositories), or "no architecture choice"; how it is built (acceptance list, repository order,
plan); the reuse map and what it extends. If anything is missing, say exactly what, stop, and
offer to draft it first from `docs/templates/` (a PRD section or an ADR). Write no code.
Otherwise "Ready", with the repositories in merge order and the first step.

**"Start it."** Start or continue the item "Pick up where we left off" names; do not stop to ask
which. Check it is ready, and stop only if something is missing. If the ticket already has a
branch with work on it, continue on that branch; never cut a second branch for the same ticket.
Otherwise, in each repository the ticket names, in order, a worktree on a feature branch from
current `origin/main` ([Commands](#commands)). Then test-first, whether or not any edit already
exists: the acceptance tests where the ticket puts them, shown failing (if an edit made before
the tests already satisfies one, stash it, show the failure, restore it); then the smallest code
that passes, and the tests passing. Commit by file name. Stop at every point the ticket marks for
a person. Do not push.

**"Ready to push?"** [Before every push](#before-every-push), each result reported; push on yes.

**"Open the PR."** Push (checks first), then a draft pull request: title `type(scope): what changed`;
sections Why, What changed, Acceptance (each id and its test), Tests run, Not done. Request the
reviewer, then mark it ready. Show the URL. Do not merge.

**"Address the review."** Every comment, inline ones too: agree or not, and the change proposed.
Wait for yes; then new commits on the same branch (never force push), the checks, push, a reply
per comment naming the commit. Leave resolving threads to the reviewer.

**"Does it match the design?"** Open each screen of the ticket's screens folder and the running
page in the same state, screenshot, and list each difference as bug, missing piece or allowed
deviation. Fix nothing until the person has read the list.

**"Which mode am I in?"** Bare platform, bare product, or product with a site applied: the dev
node's status and address, which checkout and versions run, the site binding (whoami), the
wordmark. Report each fact with its command.

**"I'm stuck."** Diagnose before fixing: the exact command and first error, which checkout and
versions run, the doctor command's output, free disk; the failing test alone on a clean main
worktree; [When something goes wrong](#when-something-goes-wrong) and the FAQ. Give the cause and
the fix. Two failed tries at one fix: stop and explain.

**"Set up my source clone."** / **"Update my setup."** Follow the contributor quickstart, steps 2
to 4 (editable install in the clone's `.venv`, hooks, a test database, the fast tests); keep
anything the product's Parts 1 and 2 installed; check `which -a <command>`. Report what changed.
