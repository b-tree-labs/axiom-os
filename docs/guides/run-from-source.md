# Run Axiom from your own source

For a developer who has a local clone of Axiom and wants `axi` to run that code. This page says
how to tell which code is running, what replaces it by accident, and what needs a restart. The
install commands themselves are in [step 2 of the contributor quickstart](contributor-quickstart.md#2-clone-and-install).
A product built on Axiom publishes its own combined path in its own repository, and that page
comes first if you work in that product.

> **Tested on.** On 2026-11-06, on macOS (Apple silicon, Python 3.13.12), with Axiom 0.70.23 and
> appkit 0.6.0, in a scratch environment with an empty home folder and a throwaway Postgres.
> **Not run:** Windows with WSL, Linux, Python 3.14 and the appkit vite dev server's proxy. The
> statement about a running MCP server keeping old code comes from how Python loads modules and
> from what `axi mcp install` prints. It was not measured here.

## Installed, or running from source

| | Installed | Running from source |
|---|---|---|
| How it got there | `uv tool install axiom-os-lm`, `pipx` or `pip install` | `pip install -e` in a virtual environment |
| What it runs | A released wheel, frozen | The files in your clone, so an edit shows the next time a new process starts |
| `which axi` | `~/.local/bin/axi`, or inside another environment | `<your-clone>/.venv/bin/axi`, while that environment is active |
| How it moves forward | `axi update` | `git pull`, then the install line again if dependencies changed |

## Python

Use Python 3.13. `requires-python` allows 3.11 and later, and CI runs 3.13 on pull requests and
on `main`, with 3.11 and 3.12 on a weekly run. Python 3.14 is not covered by that CI and is not
proven here.

Name the Python you want when you make the environment (`python3.13 -m venv .venv`). If Conda is
active, `python3.13` may resolve to Conda's, and the environment breaks when that Conda environment
is removed. Check with `grep home .venv/pyvenv.cfg`. A new worktree gets no environment of its own,
so check `which python` there too.

## Which code am I running?

Run these in the folder you are working in:

```sh
which axi
python -c "import axiom; print(axiom.__file__)"
axi whoami --software
```

- `which axi` says which command your shell finds first.
- The `python -c` line is the one that settles it. It must print a path inside your clone's `src`.
- `axi whoami --software` shows the checkout, branch and commit of the running `axiom`.

`axi --version` cannot settle it. It prints the version recorded at install time, so a release and
your clone can print the same number, and after a `git pull` that bumps the version it prints the
old one until you install again. `axi whoami --software` then says `its install record says` the old
number. That warning is harmless. Run `pip install --no-deps -e .` to refresh the record.

**A `uv tool` install next to your clone.** Your shell runs whichever `axi` comes first on `PATH`.
Activating your `.venv` puts yours first. To remove the released one so it cannot shadow you:

```text
uv tool list
uv tool uninstall <the name it shows>
```

## How a pin can replace your clone

Nothing protects an editable install. A product or a site that depends on Axiom names a version, and
pip keeps your editable copy only while the version recorded for it satisfies that requirement.
These were run on 2026-11-06:

| Your clone's recorded version against the requirement | What `pip install -e <dependent>` does |
|---|---|
| It satisfies the requirement | Keeps your clone |
| It does not satisfy it | **Replaces your clone with the wheel from the index, without asking.** `--dry-run` shows `Would install axiom-os-lm-...` |
| It does not satisfy it, and you name both on one line (`pip install -e ./axiom -e ./dependent`) | Stops with `ResolutionImpossible`. Nothing changes |

`pip install -U -e <dependent>` kept the clone when the requirement was satisfied. To be sure before
you run an install, add `--dry-run` and read the `Would install` line. If it names `axiom-os-lm`,
your clone is about to be replaced. Install the dependent with `--no-deps` instead, and add any
other dependency it needs by name.

## One environment across worktrees

`pip install -e` ties a package name to one folder. If several worktrees share one environment,
`axi` runs the folder that last installed, not the folder you are standing in. pytest is protected,
because `pythonpath` in `pyproject.toml` and the root `conftest.py` put your own `src` first. Your
shell is not.

For a shell in a shared environment, `python scripts/setup_worktree_envs.py` adds a block to each
worktree's `.envrc` that puts that worktree's `src` first, and allows it with `direnv`. Install
`direnv` first. `--check` reports and changes nothing. The script skips any repository whose
`.envrc` is tracked by git, and it says so; there, set `PYTHONPATH` yourself:

```sh
export PYTHONPATH="$PWD/src"
python -c "import axiom; print(axiom.__file__)"
```

Other choices that work: give each worktree its own `.venv`, or run `pip install --no-deps -e .` in
the worktree you want the environment to follow.

`axi dev up` runs the source of the checkout you stand in, whatever the environment is tied to, and
prints a note if the `axi` you typed runs another one. `--with <path to another src folder>` adds a
second source root ahead of the installed package.

## The hooks

`make hooks` copies `scripts/hooks/pre-push` and `commit-msg` into the clone's shared hooks folder,
the one `git rev-parse --git-common-dir` names, so every worktree of the clone uses them. Two
results follow:

- A copy goes stale. Run `make hooks` again after you pull a change to `scripts/hooks`.
- `.git/hooks/pre-push` is a path only in the main clone. In a worktree `.git` is a file, so that
  line fails with `Not a directory`. In a worktree, run this instead:

```sh
"$(git rev-parse --git-common-dir)/hooks/pre-push" < /dev/null
```

What the hook runs is in
[What runs before a push](working-with-a-coding-agent.md#5-what-runs-before-a-push-and-what-to-do-when-it-is-red).

## appkit from source

`axiom-appkit` is a separate repository and a separate Python package. It carries the browser
bundle inside the wheel (`src/axiom_appkit/static/`), so most Axiom work needs only the wheel:
`pip install axiom-appkit`. Clone it when you change the shell, a page or a component.

```sh
gh repo clone b-tree-labs/axiom-appkit ../axiom-appkit
pip install -e ../axiom-appkit
npm --prefix ../axiom-appkit/frontend install
npm --prefix ../axiom-appkit/frontend run build
```

`npm run build` writes the bundle to `frontend/dist` and to `src/axiom_appkit/static/`. A node serves
`/_appkit/appkit.js` from that folder, so with the editable install a rebuild reaches the browser on
the next page load. Check which copy you have with
`python -c "import axiom_appkit; print(axiom_appkit.__file__)"`.

**The stale bundle trap.** A built bundle looks like a cached page, and clearing the browser does
nothing. Axiom's `receipts` page has its own bundle, committed in
`src/axiom/extensions/builtins/receipts/webui/dist`, and a surface built on appkit includes appkit
as it was on the day it was built. A change to appkit does not reach a surface like that until
you rebuild the surface as well.

## What needs a restart or a rebuild

| You changed | To see it |
|---|---|
| Python in a command | Nothing. The next command is a new process |
| Python behind a running node | `axi dev up --restart`. An edit did not appear without it, and did after it |
| A built bundle on disk | Reload the page. The node reads the file on each request, so no restart |
| A dependency in `pyproject.toml` | `pip install -e ".[all]"` |
| The version in `pyproject.toml` | `pip install --no-deps -e .` |
| A migration | `axi db migrate upgrade` |
| Python behind an MCP server | Restart the agent. See below |

## Your coding agent's MCP server

`axi mcp install` writes the Python it is running from into the agent's settings, so run it with
your environment active:

```sh
axi mcp install --dry-run
axi mcp install --tool claude-code
```

On a test run the entry named `<your environment>/bin/python -m axiom.extensions.builtins.mcp.server`,
with `AXIOM_ROOT` set to the git root of the folder where you ran it. That Python imports the
editable install, so the agent uses the checkout the environment is tied to. It does not follow the
worktree you are standing in: the entry sets no `PYTHONPATH`.

**The stale server trap.** A running server keeps the code it started with. The agent starts the
server once, so an edit, a `git pull` or a new install does nothing until the agent restarts. Before
you blame your code, restart the agent, or see how long its server has run:

```sh
ps -o pid,lstart,command -ax | grep "mcp.server" | grep -v grep
axi mcp status
```

## `axi update` and `axi setup` from source

**Do not run `axi update` in a source environment.** It is written for a released install.

| You run | What it does in a clone | What to do instead |
|---|---|---|
| `axi update` inside a git folder with a `pyproject.toml` | Runs `pip install -e ".[all]"` in **the git root of the folder you stand in**. In a worktree sharing an environment, that moves the environment to the worktree. This was seen happen. It also installs every extra, runs migrations and re-registers background agents | `git pull`, and the install line if dependencies changed |
| `axi update` outside any git folder | Runs an upgrade from the index. When the index had no newer version nothing changed. With a newer version the code path would replace your editable install; that was read from the code and not run | Do not run it there |
| `axi update --check` | Reads and reports, changes nothing | Safe |
| `axi setup --dry-run` | Reads and reports | Safe |
| `axi setup` | On a clone, its `upgrade` step says `running from a source checkout; git keeps it current` and does nothing. Its other steps do change your home folder: the vault, your shell start-up file, Tab completion and each AI app's MCP settings | Run it once if you want those. Run it with your environment active, because it writes that environment's Python into the AI apps |

A command that offers a newer release does not offer it to an editable install: it treats a
checkout as updated by git.

## Local sign-in

`axi dev up` starts a node that always takes a password: the first account is your
`git config user.email`, and its password goes into your vault. It also offers your site's single
sign-on when it can, and it never needs a client secret on your laptop to do it.

**The rule.** If your install is bound to a site node (`axi site bind`) and that site publishes a
development sign-in client, `axi dev up` signs you in with the site's single sign-on by default: no
flag, no vault entry to make. A password is the fallback: when the site has not turned it on, when
the provider refuses, or when you ask for it. An install that is not bound signs in with a password.

**Which sign-in it offers.** In this order:

1. `--sign-in none`: password only, and nobody is asked.
2. A **confidential** client in your vault (an entry with an https `issuer_url`, a `client_id` and the
   client's secret as its value). This is the app owner's setup, and it is unchanged: the node is told
   the entry's name and reads the secret out of the vault itself. A bound site is not asked.
3. On a bound install, the site's **public** client. `dev up` asks the bound node for it with your
   developer key every time it starts (so a changed or withdrawn client is followed), records it in
   your vault as `<site>-sign-in`, and offers it. The summary says so:

   ```text
     Sign in: SSO via login.example.org (from the bound site)
     or with a password as you@example.org
   ```

   A site that has not turned this on answers 404, and `dev up` says
   `this site has not enabled SSO for local development; using password sign-in`. If the site cannot
   be reached, the client recorded from it earlier is used.
4. A site node that does not publish one yet: type it once,
   `axi dev up --sign-in-client <issuer_url> <client_id>`. The issuer must be https; the entry is
   recorded as a public client (no secret) and offered the same way.
5. A public client already in your vault: an entry with `issuer_url` and `client_id` whose
   `client_type` is `public` (or that has no value recorded). The node signs in with authorization
   code and PKCE and no secret. `--sign-in <name>` picks one when the vault holds several.

**The redirect.** A public client returns to `http://localhost/gate/oidc/callback` or
`http://127.0.0.1/gate/oidc/callback`. The node sends the browser back with its own port
(`http://localhost:8912/gate/oidc/callback`), because that is where your node listens; identity
providers ignore the port for a loopback redirect registered on a public-client platform (RFC 8252,
section 7.3). Open the node at `localhost` or `127.0.0.1`, as `dev up` prints it; any other host name
is refused with the address to use instead.

**Which account you land in.** The same principal you are on the site. The site keys an SSO account
on the identity provider's immutable subject (`oid` for Microsoft, `sub` for everyone else), and so
does your local node, so work you do locally is attributed to your real organization identity, with
the same `@handle`. On your own node you also hold the `owner` role, whatever roles the site's app
hands out.

An institution often gives one person two addresses, a sign-in name (`upn` or `preferred_username`,
for example `id123@eid.example.edu`) and a mail address (`email`, for example
`first.last@example.edu`). Both name one account: the gate looks for the token's subject first, then
for an account under either address, and records the other address as an alias. The password account
`dev up` made under either address is the one SSO signs you in to. It is re-keyed to your subject
on the first SSO sign-in and keeps its address, password and role, so the password still works.

**When it fails, the page names the fix.**

| The page says | What to do |
|---|---|
| The site's app registration needs the loopback redirect `http://localhost/gate/oidc/callback`; ask the site admin. | The provider does not know the redirect (for Microsoft, `AADSTS50011`). The site's administrator adds both loopback redirects under the app's "Mobile and desktop applications" platform. |
| The site's app registration does not treat ... as a public client (it asks for a secret) | The redirect is registered under a web platform. It belongs under the public-client platform. |
| Single sign-on for local development works only at a loopback address | Open the URL `dev up` printed. |

### Turning it on for a site (site administrators)

A site node publishes its public development client at `GET /api/v1/site/sign-in`. A developer key
(`*:read`) reads it; nothing anonymous does. The answer is four fields and nothing else:
`issuer_url`, `client_id`, `client_type` (always `public`) and `allowed_redirects` (the two loopback
redirects). It never carries a secret.

It is off until the node's environment sets:

| Setting | Meaning |
|---|---|
| `AXIOM_GATE_OIDC_DEV_CLIENT_ID` | The public client's id. Required; unset means the endpoint answers 404. |
| `AXIOM_GATE_OIDC_DEV_ISSUER` | The https issuer. Optional: by default, the node's own sign-in issuer (`AXIOM_GATE_OIDC_PROVIDER`, with `AXIOM_GATE_OIDC_TENANT` for a tenant issuer). |
| `AXIOM_GATE_OIDC_DEV_CLIENT_PUBLIC_SAFE=1` | Required only when the development client is the same app the node signs in with as a confidential client. Set it once that app's registration has a public-client platform carrying both loopback redirects. Without it, the node publishes nothing rather than hand out the id of an app that holds a secret. |

The app registration needs both `http://localhost/gate/oidc/callback` and
`http://127.0.0.1/gate/oidc/callback` on a public-client ("Mobile and desktop applications")
platform. Authorization code with PKCE does not need the provider's "allow public client flows"
switch.

On a node, the same public shape can be set directly: `AXIOM_GATE_OIDC_CLIENT_TYPE=public` with no
`AXIOM_GATE_OIDC_CLIENT_SECRET*` setting (one set beside it is refused at start-up), and
`AXIOM_GATE_OIDC_IDP_ROLES=0` to ignore the provider's roles claim. `dev up` sets both for you.
`AXIOM_GATE_OIDC_ACCOUNT_ID=email` keys new accounts on the email instead of the subject; `dev up`
does not set it, because that would make you a different principal from the one the site knows.

## Databases and services

Most work needs none of these. The fast tests make temporary folders. You need a Postgres of your
own in two cases: the pre-push hook, which creates and drops a throwaway database on
`AXIOM_PG_TEST_URL`, and a running node, which reads `AXIOM_DB_URL`. Both can point at the one
container from [step 3 of the quickstart](contributor-quickstart.md#3-install-the-hooks-and-a-test-database).
On Apple silicon, Docker printed a platform warning for that image (`linux/amd64`) and ran it under
emulation.

**Keep tests off your dev database.** With `AXIOM_DB_URL` unset, the root `conftest.py` points tests
at an address that refuses connections, so a test that forgot to bind a database fails loudly. But
an `AXIOM_DB_URL` you exported to run a node is honoured, as a chosen scratch server. So do not
leave it exported in the terminal where you test, or clear it for the run:

```sh
env -u AXIOM_DB_URL python -m pytest tests/test_axiom_names_no_domain.py -q
```

## When something goes wrong

| Symptom | Likely cause and fix |
|---|---|
| An edit does nothing | Run the three lines in [Which code am I running?](#which-code-am-i-running). The `python -c` line names the folder really in use |
| A verb you added "does not exist" in a worktree | The environment is tied to another checkout. See [One environment across worktrees](#one-environment-across-worktrees) |
| `axiom` now imports from `site-packages` | A dependent's install replaced your clone. See [How a pin can replace your clone](#how-a-pin-can-replace-your-clone), then `pip install --no-deps -e .` in the clone |
| `ResolutionImpossible` naming `axiom-os-lm` | Your clone's recorded version does not meet a dependent's requirement. Use `--no-deps`, or move your branch forward |
| `its install record says` an old number | Harmless. `pip install --no-deps -e .` |
| `.git/hooks/pre-push: Not a directory` | You are in a worktree. See [The hooks](#the-hooks) |
| The agent's tools run old code | [The stale server trap](#your-coding-agents-mcp-server) |
| A page does not change after an appkit edit | [The stale bundle trap](#appkit-from-source) |

Next: [Working with a coding agent](working-with-a-coding-agent.md). The reading order is in the
[guides index](README.md).
