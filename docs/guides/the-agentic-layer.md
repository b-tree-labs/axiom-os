# Part 5: The agentic layer

For a contributor who wants to build an agent, and, later, to change what every agent stands on.
Reading this page takes about fifteen minutes. The first half is for everyone who extends the
platform; the second half, **Change the shared core**, is for core contributors.

**You end with** an agent of your own on the roster and offered to MCP. Core contributors also end
with a change to the shared core that an eval shows is better.

**Before you start.** Finish [Part 3](build-a-feature.md) and read [Part 4](ship-and-operate.md).
Your agent reads the design documents this page links; the commands it needs are in the
[Part 3 agent reference](../reference/developer-workflow.md).

## Contents

**Build agents**

1. [What an agent is](#1-what-an-agent-is)
2. [Your first agent](#2-your-first-agent)
3. [Skills, surfaces and permissions](#3-skills-surfaces-and-permissions)
4. [Earning trust: graduation](#4-earning-trust-graduation)
5. [MCP and chat](#5-mcp-and-chat)
6. [Hand-offs and chains](#6-hand-offs-and-chains)

**Advanced: change the shared core**

7. [What the shared core is, and why a core reviewer is required](#7-what-the-shared-core-is)
8. [The six areas](#8-the-six-areas)
9. [Evals are the gate](#9-evals-are-the-gate)
10. [Where this is going](#10-where-this-is-going)

## 1. What an agent is

**An agent is data.** It is a goal, a persona, the contracts it keeps, the capabilities it owns and
the things it acts on. The loop that runs it, the **harness**, is code, and it is the same for every
agent: trigger, admission, dispatch, gate, hand-off, run, verify, record. Nothing specific to one
agent lives in those eight steps ([ADR-237](../adrs/adr-237-every-agent-runs-on-one-harness-an-agent-is-a-contract.md),
proposed).

So building an agent is mostly declaring one, and giving it skills. The platform's own agents,
such as RIVET for releases and KEEL for the node, are built the same way.

## 2. Your first agent

**Coming: AgentKit.** The simplest way to extend the platform will be to create an agent. AgentKit
will scaffold one with its own small page in the web app (a chat, its activity and its settings),
on the same lifecycle as a data kit: `agent init`, `agent try` in a sandbox, `agent check`,
`agent submit`, `agent promote` one rung, shadow first. It is not built. It needs an extension to
be able to add its own page (#1673) and chat to load a project's skills (#1672).

**Today,** do [the contributor quickstart, step 7](contributor-quickstart.md#7-add-an-agent-and-a-skill-in-an-extension-and-see-it-in-mcp).
In a scratch folder you scaffold an extension, write a persona, declare a `kind = "agent"` entry in
its manifest, install it, and see its skill as an MCP tool and the agent on the roster:

```sh
axi agents roster
```

Then give it a skill of its own, test first, as
[Build your first extension](build-your-first-extension.md) does for `greeter`.

## 3. Skills, surfaces and permissions

A **skill** is one function an agent can call, registered with a `SkillSpec`. Its `surfaces` say
where people and agents meet it: the command line, MCP for outside agents, and `agent_tool` for
chat and agents inside the platform. One function, every surface, no copies.

Every action passes one gate. GUARD answers **deny**, **ask** (wait for a person, with
`axi approve`), **notify** (run, show in a digest, undo for a day) or **allow**, and keeps a
receipt for every answer ([ADR-236](../adrs/adr-236-one-permission-model-deny-ask-notify-allow.md),
proposed). Your agent declares what it does; the rules decide what it may do. An agent never signs
for a person.

## 4. Earning trust: graduation

A new agent starts in **shadow**: it says what it would do, and a person decides. It moves up one
rung at a time as it earns trust from clean, reviewed actions, and high-severity actions earn it
slowest ([ADR-045](../adrs/adr-045-raci-evolution.md), the RACI graduation rules).
`axi audit graduation` shows where each actor stands. There is one ladder; do not invent a mode.

## 5. MCP and chat

**MCP** is how an outside agent, such as the one you code with, calls the platform's skills:
`axi mcp list-tools` lists them. **Chat** is how a person talks to the platform's own agents.
Address one by name: `axi agents ask <agent> "<request>"`, or `@<agent>` in chat.

Chat does not yet offer a project extension's `agent_tool` skills (#1672). Until it does, reach
your agent's skills over MCP.

## 6. Hand-offs and chains

Past hello world, agents hand work to each other. A chain of hand-offs behaves like one program:
each step carries the chain's identity, a refused step says exactly what it needs, and the result
names the next step ([ADR-231](../adrs/adr-231-a-chain-of-hand-offs-has-executor-semantics.md)).
A vetted chain is published as a **recipe**: `axi recipe list` shows them and `axi recipe plan`
dry-runs one. Read the [skill chaining guide](skill-chaining-author-guide.md) before your skill
joins a chain.

---

## Advanced: change the shared core

## 7. What the shared core is

The shared core is what every agent and every domain stands on. A mistake here reaches everyone,
so the rules are stricter:

- **A core reviewer is required** on every pull request, whatever the lander would allow.
- **An eval proves the change.** A unit test shows the code does what you meant; an eval shows the
  agents are better for it.
- **Shadow first.** A change that alters what agents do ships switched off or in shadow, and is
  turned on after it has been measured.

## 8. The six areas

| Area | What it is | Read first |
|---|---|---|
| The harness | The eight steps, the baton carried by each call, chains and admission | ADR-237 (proposed), ADR-231, [prd-agent-backed-capabilities-and-subagents.md](../prds/prd-agent-backed-capabilities-and-subagents.md) |
| Retrieval | The search corpora, embeddings, hybrid recall, and how retrieval quality is measured | `src/axiom/rag/`, [spec-rag-retrieval-policy.md](../specs/spec-rag-retrieval-policy.md) |
| Memory | Memory across coding agents, and the serving gate that decides what may be recalled for whom | ADR-087, ADR-088, `src/axiom/memory/serving.py` |
| Model routing | Which model answers, seats, providers, and changing them shadow first | [spec-model-routing.md](../specs/spec-model-routing.md) |
| Permissions and audit | GUARD's decisions, the journal, signed records | ADR-236 (proposed), `axi audit` |
| Evals | The suites that decide whether a change is better | `tests/evals/`, [prd-evals.md](../prds/prd-evals.md) |

## 9. Evals are the gate

An eval is a repeatable measurement of behaviour: does retrieval find the right passage, does the
router pick an adequate model, does an agent refuse what it must refuse. CURIO owns them. The
suites today are in `tests/evals/` and `tests/promptfoo/`, and an extension's own suite runs with
`axi ext eval`. The full framework is specified, not built ([prd-evals.md](../prds/prd-evals.md)).

**Exercise.** Make one small change to retrieval or routing. Run the relevant eval before and
after, and put both numbers in the pull request. A change with no before-and-after is not ready for
review.

## 10. Where this is going

Proposed, not decided: one model for remote execution, development containers and remote support,
in which a task, a chain, a workspace, a grant and a journal are the only nouns (epic #1722; the
draft review is `docs/working/unified-agent-workspaces-review-2026-11-10.md`). Read it to see the
direction; build only on what is on `main`.

---

Back: [Part 4](ship-and-operate.md). Index: [Guides](README.md).
**Reference:** [Part 3 agent reference](../reference/developer-workflow.md) ·
[What to say to your agent](../reference/agent-prompts.md) · [FAQ](../reference/faq.md)
