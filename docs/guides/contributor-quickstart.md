# Contributor quickstart: change Axiom

For a graduate researcher who will change Axiom itself. About two hours, mostly waiting. This
page is step 1 of the [reading order](README.md#reading-order-for-a-new-developer). The next steps
are [Run Axiom from your own source](run-from-source.md), which covers which code is running, and
[Working with a coding agent](working-with-a-coding-agent.md), which covers worktrees, the
test-first loop, reviewing a diff and landing a pull request. Those topics live there and this page
links to them. The house rules, DCO sign-off and extension conventions are in
[CONTRIBUTING.md](../../CONTRIBUTING.md) and are not repeated here. Each step gives the command, what you should see (abbreviated, from
macOS, Python 3.13, axiom 0.70.6; versions updated for 0.70.8) and what to do if it fails.

## 1. Get access

You need this only to change Axiom itself. Changing a consumer layer or a site repository does not:
those install Axiom from PyPI.

Ask the platform owner to add your **GitHub username** to `b-tree-labs/axiom-os-private`. A
deployment's own configuration lives in that deployment's separate site repository; ask that
repository's administrator, not this one. Accept the emailed invitation, then run `gh auth login`
and `gh auth status`, which should say `Logged in to github.com account your-username`. A later
"repository not found" means the invitation is not accepted yet.

## 2. Clone and install

Use Python 3.13 ([why](run-from-source.md#python)). Three installs, the same three CI runs:

```sh
gh repo clone b-tree-labs/axiom-os-private
cd axiom-os-private
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"
pip install -e packages/axiom-tests
pip install -e "packages/axiom-ext-data-platform[duckdb]"
axi --version
```

```text
axi 0.70.8
```

If it fails: the second and third lines are not optional. With only `.[all]`, the hook stops at
collection with `No module named 'hypothesis'` and `No module named 'duckdb'`. Set your commit
identity to your GitHub noreply address, or GitHub refuses the push without saying why:
`git config user.email "ID+username@users.noreply.github.com"`, and your name with
`git config user.name "Your Name"` (`-s` signs off with it). To prove that `axi` runs your clone,
see [Which code am I running?](run-from-source.md#which-code-am-i-running).

## 3. Install the hooks and a test database

```sh
make hooks
```

```text
  installed pre-push
  installed commit-msg
hooks installed to .git/hooks
```

The pre-push hook runs the CI-equivalent sweep and needs a Postgres to create a throwaway
database in. Start a local one with Docker (the image CI uses) and point the hook at it:

```sh
docker run -d --name axiom-test-pg -e POSTGRES_DB=axiom_db -e POSTGRES_USER=axiom \
  -e POSTGRES_PASSWORD=axiom -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
export AXIOM_PG_TEST_URL=postgresql://axiom:axiom@127.0.0.1:55432/axiom_db
```

If it fails: `port is already allocated` means pick another host port. The hook creates and drops
its own `axiom_hook_*` database and never touches an existing one. A missing database is not a
reason for `--no-verify`.

## 4. Run the fast tests and the layer guard

Run the hook by hand, as `git push` would. This path works in the main clone;
[in a worktree it differs](run-from-source.md#the-hooks). The first run in a clone maps which tests
touch which lines, so it runs them all:

```sh
.git/hooks/pre-push < /dev/null
```

```text
✓ pre-push: context files in sync
✓ pre-push: ruff passed
✓ pre-push: cheap guards passed
✓ pre-push: ADR numbers unique
✓ pre-push: throwaway database ready (dropped when the hook ends)
24449 passed, 489 skipped, 10 xfailed in 1061s
```

The lint and guards take under a minute; the unit suite runs on 4 workers and took about 18
minutes. Later pushes run only the tests your change can affect, and a push that touches only
documents runs the document checks; [what the hook does](working-with-a-coding-agent.md#5-what-runs-before-a-push-and-what-to-do-when-it-is-red)
has the detail. In the run captured here 6 tests also failed, all environmental (see below), so read the
list before you suspect your change. The one guard to know:

```sh
python -m pytest tests/test_axiom_names_no_domain.py -q
```

```text
9 passed in 60s
```

If it fails: `docker compose config` saying `unknown flag: --no-path-resolution` means an old
Docker Compose; update Docker Desktop. The two `test_a_reconnect_flood...` tests are timing
tests and fail on a busy laptop; rerun them alone. A long clone path wraps a help line and fails
`test_help_points_at_the_onoff_readme`, so clone somewhere short. A guard failure that names your
file means you wrote a domain consumer's name or a domain noun into the platform; say it in the
platform's own terms (step 6). A failure on a clean `main` means `main` is red, and the hook then
demands a `Bypass-Reason:` trailer (see `AGENTS.md`).

## 5. Branch, change, push, open a pull request

Make a worktree for the task, so that it cannot mix with other work:

```sh
git fetch origin
git worktree add ../axiom-wt-fix-a-guide -b docs/fix-a-guide origin/main
cd ../axiom-wt-fix-a-guide
```

Edit one file, then commit it:

```sh
git add docs/guides/the-guide.md
git commit -s -m "docs(guides): say what changed and why"
```

Name the files you add; never `git add -A`. `-s` adds the sign-off CONTRIBUTING requires (it
printed a `Signed-off-by:` trailer). Push and open a draft pull request (not run for this guide):

```sh
git push -u origin docs/fix-a-guide
gh pr create --draft --fill
```

Then ask the platform owner to review it. Before you mark it ready, read
[Landing a pull request](working-with-a-coding-agent.md#8-landing-a-pull-request): in this
repository a lander can merge a green pull request that nobody has reviewed.

If it fails: `could not read Username for 'https://github.com'` means git has no credentials; run
`gh auth setup-git`. Open the log for a red check before retrying, and name its category.

## 6. The rules that will bite

**Which layer?** Before writing a module, ask the three questions in `AGENTS.md` ("Which Layer
Does This Belong To?"): does a capability remain when you strip the domain noun; who notices when
it is wrong; does it read a consumer's own artifact.

**CLI verbs are thin wrappers over skills (ADR-056).** See
[CONTRIBUTING's house rules](../../CONTRIBUTING.md#house-rules). The logic lives in
`<ext>/skills/<verb>.py`, registered through `axiom.infra.skills.SkillRegistry`. Reference:
`data_platform/cli.py` and its `skills/`.

**Tests go before the code.** [The test-first loop](working-with-a-coding-agent.md#4-the-test-first-loop).

**Secrets go in the Axi vault.** [What never to do](working-with-a-coding-agent.md#9-what-never-to-do)
has the commands.

## 7. Add an agent and a skill in an extension, and see it in MCP

Needs axiom 0.70.7 or later: the fix that lets an extension outside the builtins tree join the
agent roster is not in 0.70.6, so `git pull` your clone first if it is older. Run this
in an empty scratch folder, outside the clone:

```sh
axi ext init pingpal --template compound
cd pingpal
mkdir -p pingpal/agents/pingpal
printf '# PINGPAL\nReply "pong".\n' > pingpal/agents/pingpal/persona.md
```

The scaffold already carries a skill, `pingpal.hello`. Append the agent to `axiom-extension.toml`:

```toml
[[extension.provides]]
kind = "agent"
name = "pingpal"
entry = "pingpal.commands.placeholder:cli"
persona = "pingpal/agents/pingpal/persona.md"
description = "pingpal test agent"
```

and add the same entry point under `[project.entry-points."axiom.agents"]` in `pyproject.toml`:
`pingpal = "pingpal.commands.placeholder:cli"`. Then:

```sh
pip install -e .
axi ext lint
axi ext publish --yes
axi ext install pingpal
axi mcp list-tools | grep -E 'pingpal|agents__'
```

```text
axi ext lint: pingpal: OK (Bronze — layout + manifest ok); chain level B claimed, C achieved
Published pingpal 0.1.0 to file:///.../.axiom/registry
Installed pingpal 0.1.0 from file:///.../.axiom/registry (signature verified)
axiom_agents__address  [registry]
axiom_agents__ask  [registry]
axiom_agents__roster  [registry]
axiom_pingpal__hello  [registry]
```

The skill is an MCP tool now. Confirm the agent is on the roster:

```sh
python -c "from axiom.extensions.builtins.connect.agent_router import discover_agents; print('pingpal' in discover_agents())"
```

```text
True
```

`axi agents ask pingpal "ping"` would send the persona and request to the configured
language-model provider and print its answer; it was not run for this guide.

If it fails: `publish` stops with `manifest_pyproject_alignment` when the `pyproject.toml` entry
point is missing. `False` above, or `install` saying the extension is not found, means an
axiom older than 0.70.7. `publish` creates a signing key under `~/.axiom/keys`; back it up if you
keep that identity. Remove the scratch extension afterwards with `pip uninstall -y pingpal`.

Next: [Working with a coding agent](working-with-a-coding-agent.md), then the rest of the
[reading order](README.md#reading-order-for-a-new-developer).
