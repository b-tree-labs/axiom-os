# Guides

This is the one place that says what a new developer reads, and in what order. Link here from a
plan rather than repeating the list.

## Reading order for a new developer

The onboarding series has five parts. Parts 1 and 2 (use the system, then add data) belong to the
product you use, so they are in that product's own onboarding, which then sends you here. Start
with step 1 below if Axiom is the only repository you will touch.

| Part | You can | Here |
|---|---|---|
| 1 | Use the system | Your product's onboarding |
| 2 | Contribute data | Your product's onboarding |
| 3 | Build a feature, with the developer workflow | [build-a-feature.md](build-a-feature.md) |
| 4 | Ship and operate it | [ship-and-operate.md](ship-and-operate.md) |
| 5 | Build agents, then change the shared core | [the-agentic-layer.md](the-agentic-layer.md) |

**Your first week** with a coding agent, day by day, is [your-first-week.md](your-first-week.md),
and the prompts to paste at each step are in [agent-prompts.md](../reference/agent-prompts.md).

1. [Part 3: Build a feature, and the developer workflow](build-a-feature.md): start here. How the
   pieces fit, the loop from branch to merge, the checks and what they catch, pull requests, and
   your first ticket. Ten minutes of reading. Its [agent reference](../reference/developer-workflow.md)
   has every command and fix; questions go in the
   [FAQ](../reference/faq.md). It sends you to the guides below as you need them.
2. [Contributor quickstart](contributor-quickstart.md): access, a clone, hooks, a test database
   and a first branch.
3. [Run Axiom from your own source](run-from-source.md): which code is running, what can replace
   it, worktrees, appkit, what needs a restart, and what not to run.
4. [Working with a coding agent](working-with-a-coding-agent.md): worktrees, the test-first loop,
   what runs before a push, reviewing an agent's diff, landing a pull request and what never to
   do.
5. [Build your first extension](build-your-first-extension.md): a hands-on tutorial for any
   domain. Scaffold an extension, meet one skill on the command line, MCP, an agent, the API and
   the web app, then declare a small logbook, propose an entry as an agent and sign it as a person.
6. [Implement a design](implement-a-design.md): where design images live, how to compare a screen,
   and the traps. Read it before you touch a screen.
7. [CONTRIBUTING.md](../../CONTRIBUTING.md): the house rules.
   After your first merge: [Part 4: Ship and operate](ship-and-operate.md) (versions, release,
   keeping `main` green, deploy and a rehearsed rollback, the agents that keep a node), then
   [Part 5: The agentic layer](the-agentic-layer.md).
8. [Running the web app's three variants](web-app-variants.md): see the app on your laptop.
9. `AGENTS.md` at the repository root: the invariants and the layering questions. Read it once in
   full, and tell your agent to obey it.
10. For extension work, the [AEOS spec](../specs/spec-aeos-0.1.md) and the
   [skill chaining guide](skill-chaining-author-guide.md).

## Other guides

| Guide | What it gets you |
|---|---|
| [Acceptance harness](acceptance-harness.md) | How spec rows are proven by tagged tests |
| [Build a previewer](build-a-previewer.md) | A file previewer for the Library |
| [Connect a Box folder](connect-a-box-folder.md) | A storage provider connected |
| [Follow-ups](follow-ups.md) | Promises tracked until they are kept |

## Before you build on logbooks

A logbook is a signed record of what people did. It is delivered by the `attest` extension, and a
domain product declares its own logbooks on top. Read in this order:

1. [ADR-142](../adrs/adr-142-attestation-is-the-record-of-accountable-human-acts.md): what an
   attestation is, and that it records a human act.
2. [ADR-143](../adrs/adr-143-attestations-are-signed-into-a-verifiable-chain.md): how records are
   signed into a chain that can be verified.
3. [ADR-150](../adrs/adr-150-an-attestation-record-is-kept-in-a-logbook.md): why the container is
   called a logbook.
4. [prd-attestation.md](../prds/prd-attestation.md): what the feature must do and why.
5. [spec-attestation.md](../specs/spec-attestation.md): how it is built, phase by phase. Its
   status line says which phases exist. ADR-144, ADR-146 and ADR-147 are worth reading when you
   reach confirmation, browser signing and live updates.
6. [The example log](examples/example_logbook/README.md): a small, domain-free log extension to
   copy. It uses only keys the loader enforces.
7. [Write a log extension](write-a-logbook-extension.md): the author guide, from scaffold to a
   signed, verified entry, with what is declared but not yet enforced. Its second example is the
   [field log](examples/field_log/README.md).

**Words that sound alike.** They are different things, and agents confuse them.

| Word | What it means | Where it lives |
|---|---|---|
| **Logbook** | A declared set of signed records of human acts, kept per site | `attest` extension; `axi log` |
| **Log** (the rail page) | The web page where a person reads and signs a logbook | appkit `LogbookPage`, reading `/api/v1/attest` |
| **System log** | What the machine wrote about itself: routing events, audit, diagnostics | `axi log`; [prd-logging.md](../prds/prd-logging.md) |
| **Tail** | A live view of a log or lane as it is written, in the Timeline, a drawer or chat. An approved design, not built yet | appkit (design approved in appkit #142) |

`axi log` and `prd-logging.md` have nothing to do with logbooks. Do not build on them for this
feature.

**How one capability reaches every surface.** A skill is a function registered with a
`SkillSpec`. Its `surfaces` tuple says where it appears. The web app does not use that tuple: it
calls the HTTP API, which the extension registers separately.

| Where a person or agent meets it | How it gets there | Example in `attest` |
|---|---|---|
| Command line | `"cli"` in `surfaces`, and a thin command that calls the skill | `axi log <log> show` |
| An outside agent over MCP | `"mcp"` in `surfaces`; it becomes a tool such as `axiom_attest__show` | `attest.show` |
| Chat, and agents inside Axiom | `"agent_tool"` in `surfaces` | `attest.show` |
| The web page | An `[[extension.provides]]` entry of `kind = "api"` | `/api/v1/attest`, from `attest/api.py` |

The surfaces are not equal for logbooks, and that is deliberate. Reading is on all of them.
Signing is `("cli",)` plus the browser's signing grant, because an agent drafts for a person and
never signs (ADR-114, ADR-142). If a change gives an agent a path to sign, it is wrong, however
convenient it looks. Read `attest/skills/__init__.py` to see each skill's surfaces.

To see the Log page on your laptop, follow [Running the web app's three variants](web-app-variants.md).
A bare node shows the page with no logbooks, because a logbook comes from an extension that
declares `kind = "logbook"`.
