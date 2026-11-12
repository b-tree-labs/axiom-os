# Part 3: Build a feature, and the developer workflow

For a new contributor who works through a coding agent. You do not need to be an experienced
programmer. Reading this page takes about ten minutes. Then you build.

**You end with** your first real change merged.

**Before you start.** Parts 1 and 2 (use the system, then add data) are in your product's own
onboarding. For this part you need a clone with the hooks installed and a test database: the
[contributor quickstart](contributor-quickstart.md), steps 1 to 3.

**In this order:** read this page, do the hello world (section 3), then take your first ticket
(section 11) through the loop (section 2). [Your first week](your-first-week.md) turns this into a
day-by-day plan, and [Prompts for your agent](../reference/agent-prompts.md) has short prompts to
paste at each step.

This is Part 3 of five: 1 use the system, 2 add data, 3 build a feature,
[4 ship and operate](ship-and-operate.md), [5 the agentic layer](the-agentic-layer.md).

**Your agent reads a different page.** Tell your agent to read `AGENTS.md`. It sends the agent to
the [Part 3 agent reference](../reference/developer-workflow.md), which has every command, every
check and every fix. This page is for you.

## Contents

1. [How the pieces fit](#1-how-the-pieces-fit)
2. [The loop](#2-the-loop)
3. [Hello world: your first extension](#3-hello-world-your-first-extension)
4. [What am I running?](#4-what-am-i-running)
5. [Run the tests on your computer](#5-run-the-tests-on-your-computer)
6. [Kinds of tests](#6-kinds-of-tests)
7. [AGENTS.md](#7-agentsmd)
8. [The checks your change must pass](#8-the-checks-your-change-must-pass)
9. [Pull requests](#9-pull-requests)
10. [After you merge](#10-after-you-merge)
11. [Your first ticket](#11-your-first-ticket)

## 1. How the pieces fit

![Four boxes. Axiom, the platform, and appkit, the web UI, each have an arrow labelled used by pointing to a distribution, a product for one domain. The distribution has an arrow labelled configured by pointing to a site, one organization's install.](../assets/diagrams/how-the-pieces-fit.png)

*Figure: The platform and the web UI are used by a distribution, which each site configures.
Source: `scripts/diagrams/onboarding_diagrams.py`.*

- **Axiom** is the platform. It works for any field of work.
- **appkit** is the web UI that Axiom and the products share.
- **A distribution** is a product for one domain, built on both.
- **A site** is one organization's install of a distribution. It holds configuration and content,
  not code.

Where a change goes:

- Something every domain would want (data, skills, commands, an API): **Axiom**.
- A screen or a web component every product could use: **appkit**.
- Something only one domain wants: **that distribution**.
- One organization's settings and content: **that site**.

If you are not sure, ask your reviewer before you branch.

## 2. The loop

Every change follows the same eight steps:

1. **Branch.** Make a new worktree on a new branch (a second folder of the same clone).
2. **Write the test first.** Ask your agent for a test that fails, and nothing else.
3. **Watch it fail.** Run it. Read why it fails.
4. **Make it pass.** Ask for the smallest code that passes. Run the test again.
5. **Push.** `git push` runs a check on your computer first, called the pre-push hook. If it
   refuses because `main` (the shared branch) is red, that is not your change: wait, and tell
   your reviewer.
6. **Open a pull request.** This asks for your change to be added.
7. **CI runs.** GitHub runs the checks again, and more.
8. **Merge.** After a person approves it and the checks pass, the change joins `main`.

In practice you *say* each step to your agent ("start my session", "brief me on the ticket",
"write the acceptance tests first", "before I push", "open the pull request") and it runs the
commands; [Prompts for your agent](../reference/agent-prompts.md) has each prompt. The commands
themselves are in [Working with a coding agent](working-with-a-coding-agent.md), sections 3 to 8.

## 3. Hello world: your first extension

Do the tutorial [Build your first extension](build-your-first-extension.md). You build a small
extension with one skill, see it on each surface (the command line, an agent's tools, the API and
the web app), and add a small logbook. It uses the same loop: test first, then code.

Do it in a scratch folder. It is practice, not a pull request.

## 4. What am I running?

On your computer you can run three things, and they look alike at a glance:

| Mode | How you start it | What you see |
|---|---|---|
| The bare platform | `axi dev up` in an Axiom clone | The Axiom wordmark, at `/receipts/` |
| The bare product | `<product> dev up` in the product's clone | The product's wordmark, at `/app/` |
| The product with a site applied | The site repository's own guide, by hand | The site's brand, labels and flags |

**How to tell today.** Check `which axi` (or the product's command) points into your clone's
`.venv`, not at a released copy elsewhere on your `PATH`; then read the `Running:` block that
`dev up` and `dev status` print (also in `--json`):

```
Running: <product> from <your checkout> (<version>)
  Platform: Axiom <version> from <clone path | installed package>
  Site look: none (sites are not applied locally yet, #1716)
  Data: local | bound to <site> at <host>
  Database: <host:port/dbname>
  Sign in: SSO via <host>, or with a password as <email> | password as <email>
```

A site named in `AXIOM_SITE` or by the binding shows as "none: <site> is not applied locally yet",
so a set variable never looks as if it worked. When the node is already running as somebody else,
`dev up --email <other>` says "Running as <A>; add --restart to switch to <other>". Also check
`<product> site whoami` and the wordmark at the top of the rail. Two catches:

- **Binding a site changes data, not looks.** `site bind`, `site use` and `site whoami` choose
  which node's data your install reads. The screen still shows the bare product's brand, flags and
  rail, so a bound laptop shows one site's data in a shell that looks like no site.
- **`axi dev up` can show the product.** If a product's web package is installed in the same
  environment, `axi dev up` lands on the product's `/app/` page instead of `/receipts/`
  ([details](web-app-variants.md)).

**Coming (#1716):** one switch, `dev up --site <name>` (and `--bare`) that applies a site's look,
and the same `Running:` block as a banner in the web app.

## 5. Run the tests on your computer

Run your extension's tests:

```sh
python -m pytest path/to/your/extension/tests -q
```

**Green** means the last line says `passed` and does not say `failed` or `error`. `skipped` is
fine.

(Today your agent installs Axiom's hooks with `make hooks`. Pointing git straight at the
tracked hooks, so a pull keeps them current and nobody installs anything, is coming, as some
products built on Axiom already do.) Then run the hook by hand before you push, as the
[quickstart's step 4](contributor-quickstart.md#4-run-the-fast-tests-and-the-layer-guard) shows.
The first run is slow. Later runs only test what your change can affect.

Two failures are usually not your fault:

- **A full disk.** Some tests refuse to write when less than about 5% of the disk is free. They
  fail with a message about free space. Free some space and run again.
- **A busy computer.** A few tests measure time, and they fail when the computer is busy. Run the
  failing test alone. If it passes alone, it was the load.

For anything else, see [When something goes wrong](../reference/developer-workflow.md#when-something-goes-wrong).

## 6. Kinds of tests

You write unit tests for every change. The others depend on what you change, or CI runs them for
you.

| Kind | What it checks | Who runs it |
|---|---|---|
| Unit | One small piece of behaviour. You write these first, for every change | You, the hook and CI |
| Integration | Your code with a real database or service | CI, after merge |
| Acceptance | A row of a spec is really done | You, when a ticket names a row |
| Contract | Two repositories agree, for example on an API | You, when your change crosses repositories |
| Visual | A screen still matches its design | You, when you change a screen |
| Smoke and install | The package installs and starts | CI |

For example, a new API route that the web app calls needs unit tests and a contract test. Ask
your agent to check the [agent reference](../reference/developer-workflow.md#test-kinds), which
says when each kind is required.

## 7. AGENTS.md

`AGENTS.md` is the rule book at the top of each repository, for people and agents. Your agent reads
it when it starts. Read it once yourself.

Two rules:

- **The repository's `AGENTS.md` wins** over your own agent settings.
- **Edit only `AGENTS.md`.** Files such as `GEMINI.md` and `.github/copilot-instructions.md` are
  copies made from it. Never edit a copy.

## 8. The checks your change must pass

Each check catches one kind of mistake. When one fails, ask your agent to read the message and
look it up in the [agent reference](../reference/developer-workflow.md#the-gates).

| Check | What it catches |
|---|---|
| Lint | Small code mistakes, such as an unused import or a misspelled name |
| Naming guard | A customer or a specific field of work named inside the platform |
| ADR numbers | Two decision records (ADRs) with one number |
| Pre-push hook | All of the above, plus the tests your change can affect, before anything leaves your computer |
| CI (continuous integration) | The same again on GitHub, plus the full test suite |
| The lander | Merges a pull request when it is green and nothing holds it |

A red check is information. Read it before you run anything again.

## 9. Pull requests

Every change goes through a pull request, and a person reviews it before it merges.\*

1. Push your branch and open a draft: `gh pr create --draft --fill`.
2. In the description, say why the change exists and which tests you ran.
3. Ask for a reviewer: `gh pr edit --add-reviewer <their GitHub login>`. Your program lead tells
   you who; with no program, ask the platform owner.
4. Mark it ready: `gh pr ready`. The checks run.
5. Answer the review. Push fixes to the same branch.
6. When it is approved, run `gh pr merge --squash --auto`. It merges by itself once the checks
   pass.

Ask for the reviewer **before** you mark it ready. In Axiom, a green pull request with no review
pending can be merged automatically.

Your agent knows the commit rules. Two to watch for yourself: it names each file it adds (never
`git add -A`), and commits carry no "Co-Authored-By" or "Generated with" line for an AI tool. You
are the author.

\* How the team uses pull requests is being evaluated. Until that changes, every change is
reviewed.

## 10. After you merge

**Versions.** Each release of Axiom has a version number. Your change is in the next one.

**Release.** A maintainer cuts a release from `main` and publishes it. You do not do this yet. A
product that installs Axiom as a package gets your change only through that release: until it
exists, a product clone runs the previous one, and `pip install -U axiom-os-lm` picks up the new
one once it is published.

**Deploy.** A site moves to a new release when its owner updates it. That is when people see your
change.

[Part 4: Ship and operate](ship-and-operate.md) covers all three.

## 11. Your first ticket

A ticket is a written task. If you have joined a program, its tickets folder names your first
change, with its branch, files and checks. Follow the loop above with that ticket. If you have no program, ask the platform owner
for a small first change.

[Your first week](your-first-week.md) is the plan for the days around it, and
[Prompts for your agent](../reference/agent-prompts.md) has a prompt for each step.

---

**Reference:** [Part 3 agent reference](../reference/developer-workflow.md) ·
[FAQ](../reference/faq.md) · [Guides index](README.md)
