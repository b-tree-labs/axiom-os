# Contributor quickstart: change Axiom

For a graduate researcher who will change Axiom itself. About two hours, mostly waiting. The
house rules, DCO sign-off and extension conventions are in [CONTRIBUTING.md](../../CONTRIBUTING.md)
and are not repeated here. Each step gives the command, what you should see (abbreviated, from
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

CI runs Python 3.13 on pull requests (`.github/workflows/ci.yml`); `requires-python` is 3.11. Use
3.12 or 3.13. Three installs, the same three CI runs:

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
`git config user.name "Your Name"` (`-s` signs off with it).

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

Run the hook by hand, as `git push` would:

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
minutes. In the run captured here 6 tests also failed, all environmental (see below), so read the
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

```sh
git switch -c docs/fix-a-guide
```

Edit one file, then commit it:

```sh
git add docs/guides/the-guide.md
git commit -s -m "docs(guides): say what changed and why"
```

Name the files you add; never `git add -A`. `-s` adds the sign-off CONTRIBUTING requires (it
printed a `Signed-off-by:` trailer). Push and open the pull request (not run for this guide):

```sh
git push -u origin docs/fix-a-guide
gh pr create --fill
```

Then ask the platform owner to review it.

**Merge rules.** Branch protection requires `Lint` and `Unit Tests (3.13)` green and one approving
review; your branch does not have to be up to date with `main`. Auto-merge is enabled for the
repository, so once the review is in, `gh pr merge --squash --auto` merges when the checks pass; do
not sit and watch CI. There is no merge queue: the integration, migration and install-path suites
run on `main` after the squash merge, so a pull request green on everything visible can still turn
`main` red. A red run there is yours to fix first; read it as information, not a flake. Never push
to `main`; it is squashed.

If it fails: `could not read Username for 'https://github.com'` means git has no credentials; run
`gh auth setup-git`. Open the log for a red check before retrying, and name its category.

## 6. The rules that will bite

**Which layer?** Before writing a module, ask the three questions in `AGENTS.md` ("Which Layer
Does This Belong To?"): does a capability remain when you strip the domain noun; who notices when
it is wrong; does it read a consumer's own artifact. Axiom never names a domain consumer or a
customer, in code, tests or docs. Use labeled generic examples.

**CLI verbs are thin wrappers over skills (ADR-056).** The logic lives in
`<ext>/skills/<verb>.py` as `(params, ctx) -> SkillResult`, registered through
`axiom.infra.skills.SkillRegistry`; the argparse handler only calls it. Then the same capability
works from the CLI, from a peer agent and from MCP. Reference: `data_platform/cli.py` and its
`skills/`.

**Tests go before the code.** Write the failing test, watch it fail, then implement. A CLI verb
needs a subprocess-level test that asserts on stdout.

**Secrets go in the Axi vault**, never a file, environment variable, script, argv or the macOS
Keychain. The value comes from a prompt or standard input:

```sh
axi secrets set my-credential-name
axi secrets list
```

`set` prompts (not run for this guide); `list` prints names and metadata, never values.

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

Next: [CONTRIBUTING.md](../../CONTRIBUTING.md), then the AEOS spec in `docs/specs/`.
