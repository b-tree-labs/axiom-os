# Build your first extension

A hands-on tutorial for anyone building on Axiom, in any domain. You scaffold an extension called
`greeter`, give it one skill and meet that skill on each surface (the command line, MCP, an agent,
the API and the web app), then declare a small logbook, propose an entry as an agent and sign it
as a person. It ends with the test-first loop, the hooks and a pull request, which other guides
already cover and this one links to.

**Before you start.** Do the [contributor quickstart](contributor-quickstart.md) through step 3: a
clone, the three installs, the hooks and the Postgres container. Work in an empty scratch folder
outside the clone; the commands below use `~/scratch`. The quickstart's step 7 is the short version
of parts 1 and 2 (an extension, an agent and an MCP tool); this page goes further, onto every
surface and into a logbook.

> **Tested on.** 2026-11-09, macOS (Apple silicon), Python 3.13.12, Axiom 0.70.28 installed from
> `main` (`51494c399`), appkit 0.6.0 from PyPI, and a throwaway `pgvector/pgvector:pg16` container,
> with an empty home folder and nothing exported but `AXIOM_DB_URL`. Parts 1 and 2 ran as written.
> In part 3, every command ran except signing at the command line (it needs a key in the macOS
> keychain, which the run did not touch), and the person signed through the web app's API instead.
> The logbook template and its doctor check (units F2 and F3) landed after this run and are covered
> by their own tests. Not run: Windows with WSL, Linux, and a browser (the web steps were checked through the API
> the pages read).

## Contents

1. [Scaffold the extension](#1-scaffold-the-extension)
2. [One skill on every surface](#2-one-skill-on-every-surface)
3. [A small logbook](#3-a-small-logbook)
4. [Test first, hooks and a pull request](#4-test-first-hooks-and-a-pull-request)
5. [What does not exist yet](#5-what-does-not-exist-yet)

## 1. Scaffold the extension

An extension is found in a project's `.axi/extensions/` folder, so scaffold straight into it:

```sh
mkdir -p ~/scratch/.axi/extensions && cd ~/scratch
git init -q .
axi ext templates
axi ext init greeter --dir .axi/extensions --owner example-lab --description "Greets people by name"
pip install -e .axi/extensions/greeter
cd .axi/extensions/greeter
axi ext lint
axi ext test
```

```text
compound   Canonical AEOS §5.1 compound layout — all seven capability subdirectories, ... (default)
...
axi ext lint: greeter: OK (Bronze — layout + manifest ok); chain level B claimed, C achieved
...
======================== 9 passed, 2 skipped in 0.09s =========================
```

`axi ext init` takes a purpose-named extension (no type suffix). The scaffold writes
`axiom-extension.toml` (the manifest), `pyproject.toml`, a `greeter/` package with a placeholder
command and a first skill, `greeter.hello`, and a `tests/` folder. Read `AGENTS.md` inside it once.
The [AEOS spec](../specs/spec-aeos-0.1.md) explains each manifest block.

If it fails: `axi ext init` refuses to overwrite an existing folder, so pick another name or delete
the old one. `git init` matters only for part 2's web step.

## 2. One skill on every surface

A skill is a function registered with a `SkillSpec`. Its `surfaces` say where it appears: `"cli"`,
`"mcp"` and `"agent_tool"`. The API and the web app do not read that tuple; an extension serves
them with a `kind = "api"` block (see the table in
[Before you build on logbooks](README.md#before-you-build-on-logbooks)).

### Test first

Write the test before the skill, in `tests/unit_tests/test_greet.py`:

```python
from axiom.infra.skills import SkillRegistry

from greeter.skills import bind


def _registry() -> SkillRegistry:
    registry = SkillRegistry()
    bind(registry)
    return registry


def test_greet_says_the_name():
    result = _registry().invoke("greeter.greet", {"name": "Ada"}, None)
    assert result.ok
    assert result.value == {"message": "Hello, Ada!"}


def test_greet_without_a_name_is_refused():
    assert not _registry().invoke("greeter.greet", {}, None).ok


def test_greet_is_on_the_cli_mcp_and_agent_surfaces():
    assert set(_registry().spec("greeter.greet").surfaces) == {"cli", "mcp", "agent_tool"}
```

`axi ext test` now fails three times, with `KeyError` for the missing skill. That is the failure
you want to see.

### The skill

Append the function to `greeter/skills/impl.py`:

```python
def greet(params: dict[str, Any], ctx: Any):
    """Greet one person by name. A read: it changes nothing."""
    from axiom.infra.skills import SkillResult

    name = str(params.get("name") or "").strip()
    if not name:
        return SkillResult(ok=False, errors=["name is required, e.g. name=Ada"])
    return SkillResult(ok=True, value={"message": f"Hello, {name}!"})
```

Register it in `greeter/skills/__init__.py`: import `greet` beside `hello`, and add a second
`register_skill` call inside `bind`:

```python
    registry.register_skill(
        SkillSpec(
            name="greeter.greet",
            fn=greet,
            description="Greet a person by name.",
            inputs={"name": "str"},
            surfaces=("cli", "mcp", "agent_tool"),
            side_effects=False,
            idempotent=True,
            risk="read",
            requires=(),
        ),
        mutating=False,
    )
```

`inputs` is what MCP clients and agents see as the tool's parameters; without it the tool takes
no arguments. Add the skill's documentation as `greeter/skills/greet/SKILL.md` (copy the `hello`
one), and project the spec into the manifest beside the `greeter.hello` block:

```toml
[[extension.provides]]
kind = "skill"
name = "greeter.greet"
entry = "greeter.skills.impl:greet"
path = "greeter/skills/greet"
description = "Greet a person by name."
risk = "read"
side_effects = false
idempotent = true
requires = []
```

`axi ext test` passes: `12 passed, 2 skipped`.

### Command line

A command is a thin wrapper over the skill (ADR-056), and it calls the skill through
`invoke_capability`, the one chokepoint where authorization and audit apply to every surface.
Create `greeter/cli.py`:

```python
"""``axi greeter``: a thin command over the greeter skills (ADR-056)."""

from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="axi greeter", description="greet people by name")
    sub = parser.add_subparsers(dest="verb", required=True)
    greet = sub.add_parser("greet", help="greet a person by name")
    greet.add_argument("name")
    greet.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import CLI_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    from greeter.skills import bind

    registry = SkillRegistry()
    bind(registry)
    ctx = SkillContext(
        registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("greeter")
    )
    result = invoke_capability(
        registry, "greeter.greet", {"name": args.name}, ctx, surface=CLI_SURFACE
    )
    if not result.ok:
        print("\n".join(result.errors), file=sys.stderr)
        return result.exit_code
    print(json.dumps(result.value) if args.json else result.value["message"])
    return 0
```

Point the placeholder `cmd` block in the manifest at it (`entry = "greeter.cli:main"`, and a real
`description`), and the same line under `[project.entry-points."axiom.commands"]` in
`pyproject.toml`. Then, from `~/scratch`:

```sh
axi greeter greet Ada
axi greeter greet Ada --json
axi greeter greet ""
```

```text
Hello, Ada!
{"message": "Hello, Ada!"}
name is required, e.g. name=Ada
```

The last one exits 1.

### MCP

Nothing more to write: `"mcp"` in `surfaces` makes the skill a tool.

```sh
axi mcp list-tools | grep greeter
axi mcp inspect axiom_greeter__greet
```

```text
axiom_greeter__greet  [registry]
axiom_greeter__hello  [registry]
name:        axiom_greeter__greet
description: [platform] Greet a person by name.
...
    "name": {
      "type": "string",
```

An MCP client calling `axiom_greeter__greet` with `{"name": "Ada"}` gets
`{"ok": true, "value": {"message": "Hello, Ada!"}, ...}`; this was checked with the `mcp` Python
client against `axi mcp serve`. To call it from your own coding agent, connect it with
`axi mcp install` ([how](working-with-a-coding-agent.md#1-install-axiom-and-connect-your-agent))
and restart the agent.

### An agent

`"agent_tool"` in `surfaces` offers the skill to chat and to agents inside Axiom, as the tool
`greeter__greet`. Chat offers only the namespaces you name:

```sh
axi settings set chat.tool_namespaces greeter
axi chat --tools
```

**Not working yet.** On 0.70.28 the list printed by `axi chat --tools` does not include
`greeter__greet` for an extension in `.axi/extensions/`, although the setting is recorded and the
projection itself produces the tool. Chat's skill registry is not filled from project extensions.
Until that is fixed, use MCP (above) to see an agent call the skill. The quickstart's step 7 shows
how an extension adds an agent of its own to the roster.

### API

An extension serves part of `/api/v1` with a `kind = "api"` block. Create `greeter/api.py`:

```python
"""``GET /api/v1/greeter/greet``: the web face of ``greeter.greet``.

No ``from __future__ import annotations``: FastAPI reads the handler's
annotations when the route registers, and ``Request`` is imported inside.
"""

import logging
from typing import Any


def register_routes(router: Any, *, subpath: str = "/greeter") -> None:
    from fastapi import Request

    from axiom.infra.paths import get_user_state_dir
    from axiom.infra.skill_dispatch import WEB_SURFACE, invoke_capability
    from axiom.infra.skills import SkillContext, SkillRegistry

    from greeter.skills import bind

    @router.get(subpath + "/greet", tags=["greeter"])
    def greet(request: Request, name: str = "") -> dict:
        registry = SkillRegistry()
        bind(registry)
        ctx = SkillContext(
            registry=registry, state_dir=get_user_state_dir(), logger=logging.getLogger("greeter")
        )
        result = invoke_capability(registry, "greeter.greet", {"name": name}, ctx, surface=WEB_SURFACE)
        return {"ok": result.ok, "value": result.value, "errors": list(result.errors)}
```

and declare it in the manifest:

```toml
[[extension.provides]]
kind = "api"
subpath = "/greeter"
entry = "greeter.api:register_routes"
description = "GET /api/v1/greeter/greet?name=Ada"
```

This route answers anyone who can reach the node. A route that reads anything private checks the
session first, as `attest/api.py` does with its `who(request)`.

Start a node from `~/scratch` with [Running the web app's three variants](web-app-variants.md#run-it-on-your-laptop)
(`axi db migrate upgrade`, then `axi dev up`), sign in, and open:

```text
http://127.0.0.1:<port>/api/v1/greeter/greet?name=Ada
{"ok":true,"value":{"message":"Hello, Ada!"},"errors":[]}
```

Without `name`, the same route returns `"ok":false` and the error.

### The web app

**There is no extension page yet.** An extension cannot add a page to the rail on 0.70.28: the
manifest has no page kind, and appkit has no place to mount one. What a person sees in the browser
today is the API response above, and, once chat offers the tool, chat in the web app. A logbook is
the exception: the Log page hosts any logbook an extension declares, which part 3 uses.

## 3. A small logbook

A logbook is a declared set of signed records of what people did. Read
[Before you build on logbooks](README.md#before-you-build-on-logbooks) first: it says what a
logbook is, how it differs from the Log page and from the system log, and why an agent may propose
an entry but never sign one.

This part has two paths. **The template** is the short route: unit F2 ships a domain-free example
logbook (`docs/guides/examples/example_logbook/`), and unit F3 adds a `logbook` template to
`axi ext init` and a logbook check to `axi ext doctor`. **By hand** writes the same kind of file
yourself, and ran for this page.

### Declare it

**The template.**

```sh
cd ~/scratch
axi ext init visitors --template logbook --dir .axi/extensions
axi ext doctor .axi/extensions/visitors
```

`axi ext templates` lists `logbook`. The scaffold is the compound layout plus
`logbook/visitors.toml`, a copy of F2's example with its id and name changed, which uses only keys
the loader enforces; the manifest carries a `kind = "logbook"` block that validates; `axi ext lint`
passes on day one; and `axi ext doctor` reports `logbook.declared` with the logbook's id and types,
or a sentence saying what to fix when the file is missing, does not load, names a different id, or
reuses an id another extension declares. F2's example has an interval, a check with an observed
value, a note and a cadence, with roles `member` and `lead`.

**By hand.** Add a logbook to `greeter` yourself. Create `logbook/visitor_log.toml` beside the
manifest:

```toml
# Who visited, who hosted them, and the badge they carried.
# An agent may propose a visit; only a person enters the badge and signs.
[logbook]
id = "visitor_log"
version = "1"
display = "Visitor Log"
assurance = { posture = "attested" }

[[type]]
id = "VISIT"
meanings = ["authored"]
roles = ["member", "lead"]
fields = [
  { id = "visitor", type = "text", label = "Visitor", required = true },
  { id = "host", type = "text", label = "Host", required = true },
  { id = "badge", type = "text", label = "Badge number", observe = true, required = true },
]

[[type]]
id = "NOTE"
meanings = ["authored"]
roles = ["lead"]
fields = [{ id = "text", type = "text", label = "Note", required = true }]
```

and declare it in the manifest:

```toml
[[extension.provides]]
kind = "logbook"
id = "visitor_log"
file = "logbook/visitor_log.toml"
display = "Visitor Log"
description = "Who visited, who hosted them, and the badge they carried"
```

`observe = true` on `badge` means software never supplies it: the person reads it and enters it
(ADR-144). The floor is `attested`, which a password sign-in reaches; a node with single sign-on can
raise it to `sso`. Then:

```sh
axi attest logbook validate .axi/extensions/greeter/logbook/visitor_log.toml
axi attest logbook list
axi ext lint
```

```text
valid: visitor_log v1 (2 types)
visitor_log  v1  Visitor Log  (floor attested)
  VISIT: authored; roles member, lead
  NOTE: authored; roles lead
```

`validate` lists anything the file declares that is not yet enforced; this one declares nothing of
the kind. A logbook is per site, and signing needs a node identity and roles. On a laptop, invent
the site:

```sh
axi federation init --owner you@example.org --name my-laptop --profile leaf
axi attest role grant @you:example-site member --site example-site
```

The handle is the one the web app gives you when the node runs with `AXIOM_SITE=example-site`: the
part of your sign-in email before `@`, then the site. Grant the role to the handle the signer will
use.

### Propose an entry as an agent

**Available when L1 lands.** Unit L1 adds a propose skill (`attest.propose`), the only write skill
on MCP and chat, and the tray where a person sees what agents proposed.

**Today.** There is no public surface for an agent to propose, so call the service from Python, as
an integration would. Save this as `propose_visit.py` in `~/scratch`:

```python
from axiom.extensions.builtins.attest import service

draft_id = service.create_draft(
    site_id="example-site",
    logbook="visitor_log",
    entry_type="VISIT",
    meaning="authored",
    content={"title": "Visit", "fields": {"visitor": "Ada Lovelace", "host": "Grace Hopper"}},
    origin="agent:greeter",
    for_principal="@you:example-site",
)
print(draft_id)
```

`python propose_visit.py` prints the draft id. Add `"badge": "V-1"` to the fields and run it again:
it is refused, `agent:greeter offered ['badge']; the logbook marks them observe, so only the person
may enter them (ADR-144)`.

### Sign it as a person

A person completes the draft and signs it, after reading what they sign.

**At the command line:** `axi attest sign <draft-id> --field badge=V-042` shows the entry and its
digest and asks `sign, hold or ask?`. Answer `sign`. On 0.70.28 the command line signs only at the
`attested` posture, which means `AXIOM_IDENTITY_POSTURE=attested` and a personal key the command
creates in the macOS keychain; with the default `open` posture it refuses, `posture 'open' cannot
sign`. A logbook whose floor is `sso` cannot be signed at the command line at all. This step was not
run for this page, because it writes to the keychain.

**In the web app:** run the node with `AXIOM_SITE=example-site`, sign in, and open the Log page
(`#/log`). The page reads the same API this page used to sign, in this order:
`PUT /api/v1/attest/drafts/<id>` with the badge, `POST .../drafts/<id>/present`,
`POST /api/v1/attest/presentations/<id>/respond` with `"answer": "sign"`, `POST /gate/grants` for a
signing grant, and `POST /api/v1/attest/sign`. That sequence ran, and returned the signed record:
`origin` `agent:greeter`, signer `@you:example-site` with role `member`, posture `attested`.

### See it

```sh
axi attest show --site example-site --logbook visitor_log
axi attest verify --site example-site --logbook visitor_log
```

```text
#    1  2026-11-09T22:15:02Z  VISIT authored  Visit  by @you:example-site
OK: 1 records; head #1 340d4317...
  no anchor covers this logbook yet
```

The same record is on MCP (`axiom_attest__show`, `axiom_attest__verify`) and the API
(`GET /api/v1/attest/visitor_log/records?site=example-site`). There is no MCP tool that signs.

**The Log page** lists every logbook the node holds, so Visitor Log appears there with its entry.
**Available when W1 lands:** the designed logbook switcher, empty states and "+ New entry".

**The Timeline** is the panel at the foot of each screen, and the cross-source view in Log, that
shows what happened across logbooks and the platform. **Available when J2 lands** (it needs J1, the
journal). Until then, a signed entry is seen on the Log page and through `axi attest show`.

## 4. Test first, hooks and a pull request

The skill above was written test first. The loop, and how to hold an agent to it, is
[The test-first loop](working-with-a-coding-agent.md#4-the-test-first-loop). The rest of the path
to a merged change is written once, elsewhere:

- [What runs before a push](working-with-a-coding-agent.md#5-what-runs-before-a-push-and-what-to-do-when-it-is-red)
  and the [naming guards](working-with-a-coding-agent.md#6-naming-guards).
- [How to review an agent's diff](working-with-a-coding-agent.md#7-how-to-review-an-agents-diff)
  and [landing a pull request](working-with-a-coding-agent.md#8-landing-a-pull-request).
- [Branch, change, push, open a pull request](contributor-quickstart.md#5-branch-change-push-open-a-pull-request)
  and the [house rules](../../CONTRIBUTING.md#house-rules).

Where your extension lives decides where the pull request goes: a platform capability in Axiom, a
domain's extension in that domain's repository. Ask the three questions in `AGENTS.md` ("Which
Layer Does This Belong To?") before you branch. If you have joined a program, its tickets name your
first change.

## 5. What does not exist yet

Each of these was checked on 0.70.28. Remove a line when the thing lands.

| Surface or step | State | What to use meanwhile |
|---|---|---|
| A page of the extension's own in the web app | No page kind in the manifest; nowhere in appkit to mount one | The API, and the Log page for a logbook |
| The skill in chat (`agent_tool`) | Declared and projected, but chat does not load project extensions' skills | MCP |
| `axi ext publish` with a `kind = "api"` block | Refused: `manifest_pyproject_alignment: ... api (unknown capability kind)` | Keep the extension in `.axi/extensions/`, as here |
| `axi dev up` in a home with no vault | Stops before making the account if the password cannot be stored (a cancelled keychain prompt, or a host with no keychain), and names the fix | `export AXIOM_FOREIGN_SECRETS_BACKEND=file` for development (a file only you can read), then `axi dev up` again |
| An agent proposing through a skill | Unit L1 | `service.create_draft` from Python |
| Signing at the command line at the `sso` floor | The command line reaches `attested` at most | The web app's signing |
| The Timeline | Units J1 and J2 | The Log page and `axi attest show` |
