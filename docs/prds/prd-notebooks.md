# PRD: notebooks with AI, and learning by doing

**Owner:** platform • **Status:** Draft, design study • **Last updated:** 2026-10-30
**Related:** [prd-model-loop.md](prd-model-loop.md) (notebooks attached to analyses,
hypotheses and V&V records; cycle reruns), [prd-tenant-data-kit.md](prd-tenant-data-kit.md)
(the workbench notebook), [spec-webapp-buildout-roadmap.md](../specs/spec-webapp-buildout-roadmap.md)
(phasing), ADR-056 (skill as function), ADR-102 (runnable runbooks), ADR-128 (tier
boundaries), ADR-214 (one chart spec), ADR-036 §D10 (per-dispatch sandbox)

The product's specialisation (its examples, courses and practice flows) lives in its own repo.

## Why this exists

Notebooks are the language engineers, researchers and students already speak. Few of
them work in a repository; they work in a notebook with examples to start from, and
that is how students learn. The platform has chat, skills, governed data, a chart
catalog, a Library and a signing card, but no notebook. Today a notebook is a file on
someone's laptop, and when it is finished nothing happens to it.

This PRD keeps the notebook, puts the assistant inside it as cells a person can see and
review, runs code only where it is safe, and makes saving lead somewhere.

## What the state of the art does (2025–2026)

A survey of the leading hosted, open-source, reactive and teaching notebook tools
converged on these patterns, which this PRD copies:

- two AI surfaces: a per-cell assist (generate, fix, explain) and a chat or agent that
  edits many cells, runs them and reads the outputs;
- talking separated from changing (ask versus edit modes);
- a diff and an accept step for every AI change, per cell;
- dependency-aware (reactive) execution, also used to validate agent edits;
- SQL cells whose results are dataframes;
- reviewable text formats instead of JSON with embedded outputs;
- governed context (catalog metadata, semantic models) instead of clever prompts;
- the notebook as an app or report, and tutor modes for teaching.

The same survey found problems no tool has solved: hidden state (a large study of public
notebooks found only about a quarter ran cleanly and about 4% reproduced their results),
no record in the saved file of which cells AI wrote and who accepted them, hidden agent
queries that give unverifiable answers, weak review of output-dependent claims, and
executable notebooks as a prompt-injection and code-execution risk. The platform already
solves three of these elsewhere (recorded skill calls with hashes, the attested signing
card, governed gold) and this design leans on them.

## Requirements

### Cells

| Kind | Holds | Runs | Produces |
|---|---|---|---|
| Code | Python first; the language comes from a Jupyter kernel spec | an executor, never the serving node | MIME outputs, variables |
| SQL | a read-only query on **gold** (through the data door) or the app database's read views; silver is not selectable | the data door, under the person's grants | a named dataframe with row count, quality-mark summary, freshness |
| Text | Markdown, maths, record links | the browser | prose |
| Chat | a prompt and the assistant's answer, with action cards: Insert as code, Run, Chart this, Explain, Make a test | the one chat | an answer; proposed cells as a diff |
| Agent command | a plain-language command resolved to a registered skill call | the skill runtime, under the caller's grants | the tool result; deterministic replay |
| Chart | a chart spec bound to an earlier dataframe | the as-built renderer | a chart; Save puts it in the catalog |
| Parameter | a typed input | the browser | a value; a job or skill argument when promoted |

- **AI provenance on every AI-written cell:** the writer (assistant, model and version, or
  an outside agent), the prompt or plan step, the time, who accepted it and whether they
  edited it. A badge on the cell; metadata in the file and the run record; edits extend
  the history, never erase it.
- **Reactive, lazy by default.** A dependency graph from names defined and read, SQL table
  names, chart bindings and parameters. An edit marks dependents stale at once; cheap
  cells rerun; expensive cells wait for Run with a cost estimate. Redefinition is a
  warning (imported notebooks open in a declared linear-compatibility mode). Only "run
  all from clean" produces a promotable run record.

### AI in the notebook

Four depths, each opt-in from the previous: inline (one cell); ✦ Discuss on a cell (the
cell, upstream data, outputs and parameters as removable chips; withheld context shown as
withheld); the one chat proposing multi-cell edits as a reviewable diff validated by the
graph; and an agent mode that works through an approved plan as visible cells, pausing
before expensive work, writes and deletes.

- Nothing writes outside the notebook without the signing card.
- Ask mode cannot change cells.
- The assistant has no private path: every action is a skill the person could have run,
  recorded as a cell; exploratory queries are shown collapsed, not hidden.
- Cell text and outputs are data, never instructions.
- Outside agents reach notebooks through the platform's MCP tools with the same rules.

### Teaching notebooks

A template kind with steps, progressive hints, visible checkpoints and hidden graded
checks run from clean on submit; a tutor (the same assistant in tutor mode) whose policy
(Off · Hints only · Explain · Full, plus an exam lock) is set by the instructor and
**enforced server-side**; submissions frozen into a course folder. Tutorials run in the
browser by default. Products supply their own examples.

### Execution

| Executor | For | Limits |
|---|---|---|
| Browser (Pyodide) | teaching, light work, reading and poking | the tab; reads gold through the data door's API |
| Per-user container | normal work | CPU, memory, disk and wall-clock quotas; idle shutdown; pinned image; vault-injected secrets |
| HPC executor | heavy runs, training, controlled codes | the person's allocation; core-hour estimate before Run |

- No notebook code on the serving node, ever.
- **States that must not lie:** a kernel chip says where code runs and its state; outputs
  carry their run time; stale outputs are marked; a restart stales what it lost.
- Controlled codes: a cell that runs one delegates to the job broker, which checks the
  invoking principal and runs it only on an authorised executor; the notebook has no gate
  of its own. The invocation's arguments are not sent to commercial models, federated or
  traced. Data and results are not controlled; they flow under ordinary access control.
  Outputs inherit the strictest classification of their inputs.

### Save → outcome (the lifecycle)

Draft (the person's notebooks folder in the Library, autosaved, versioned) → shared (the
Library's roles) → **promoted** to one or more outcomes → frozen when attached to a
concluded or signed record.

| Outcome | Lands in | Official act |
|---|---|---|
| Library report (outputs frozen) | a Library folder | no |
| Chart | the Charts catalog | no |
| Dataset | gold, through a data kit contribution, never an ad hoc table | the kit's review |
| Model or surrogate | the model registry as a candidate, via a training run | registration and promotion are signed |
| Scheduled job | the schedule substrate, on a named executor, with an owner | yes if it writes to gold or alerts |
| Skill or agent tool | an extension's skills, after code review | review and enablement |
| Signed analysis | a hypothesis, V&V record or finding; certification evidence | the signing card |
| Course or template | the template gallery behind "+ New ▾" | the instructor publishes |

Promotion reruns from clean, compares outputs and summarises AI provenance. Every outcome
records the notebook revision and run record; every notebook lists what it fed ("Used by").

### Formats and review

- Canonical: the **percent-format `.py`** the platform already ships (data kits,
  runbooks), with typed markers for SQL, chat, agent, chart and parameter cells and AI
  provenance in cell metadata. Outputs live in the run record, not the file.
- `.ipynb` imports (cells untrusted until run on an executor; rich outputs sanitised) and
  exports (chat cells as Markdown, agent cells as client calls, a provenance header).
- Files live in the Library; a repo-backed Library folder is the option for git users.
- A review request for promoted notebooks shows the code diff, the clean rerun's
  comparison and the provenance summary.

### Placement

A notebook is a Library file kind opening in a full-width notebook surface; no new rail
item. Start points: "+ New ▾ › Notebook" on pages whose noun allows it (with that page's
context), ✦ Discuss › Open in a notebook, ⌘K "new notebook", chat action cards, the
Library's + New. Model's records show attached notebooks in place.

### Learning by doing (principle)

Every area offers practice where it is natural, as starters in "+ New ▾" and on empty
states: "show me how" chat cards, example chart groups, sample Library folders, notebook
tutorials, practice runs of a product's request flows, training replays of recorded
operations, and reruns of past model-improvement cycles in a practice sandbox.

- **Visibly practice:** a hatched banner with the word PRACTICE, a dedicated practice
  accent (never a status colour or the brand accent), a practice border; colour is never
  the only signal. Samples carry a SAMPLE label.
- **One-click Reset** and **Leave practice**.
- **Never mixed into real records:** practice writes go to the person's practice space,
  excluded from search, charts, logs, counts and queues. Verbs that create records
  (submit, sign, promote, register, deploy) are absent in practice.
- Practice reads through the person's own grants, gold only, from a pinned snapshot where
  needed; each practice job runs in its own transient sandbox (ADR-036 §D10).
- Graduating is an explicit copy into a real draft that starts the real flow.
- Where practice is part of qualification, the result is a training record in its own
  training log, never in an operational log.

## Build or adopt

Options considered: embed an existing notebook application with its AI extension; adopt
an open-source reactive engine and its editor; build everything; or a hybrid.
**Recommended: hybrid.** The notebook surface is built on appkit (cells reuse chat,
action cards, Charts, DataTable, ✦ and the signing card, with a standard code-editor
component); execution uses the Jupyter kernel protocol inside executor sandboxes (any
Jupyter kernel works); Pyodide runs in the browser; the dependency graph is static
analysis; format conversion uses jupytext. Embedding a second application would mean two
navigations and two chats; adopting a reactive engine's editor would fork our cell kinds
and file format. Export to Jupyter stays the escape hatch for features we do not build.

## Phasing

| Package | Ships | With | Size |
|---|---|---|---|
| N0 Read and learn | read-only notebook rendering in the Library; Open in Jupyter; RAG ingest; samples; "show me how" cards | P1 (MVP) | S |
| N1 The notebook | editor, seven cell kinds, reactive-lazy graph, provenance, inline AI, ✦, multi-cell diff, container executor, versions, `.ipynb` import/export, promote to report and chart, tutorials in the browser | P6a | L |
| N2 Outcomes and agents | agent mode, HPC executor and controlled-code delegation, promote to dataset, training run, job, signed analysis; review requests; cycle reruns in a practice sandbox | P6b–P6c | L |
| N3 Teaching and reach | courses, graded checks, tutor policy and exam lock, skill generation, repo-backed folders, co-editing | P7 or earlier for a course | L |

Learning mode by phase: MVP samples and "show me how"; P4 training replays; P5 practice
requests; P6 cycle reruns.

## Acceptance

| ID | Acceptance |
|---|---|
| NB-01 | No notebook code executes on the serving node; a test asserts the node has no kernel |
| NB-02 | A SQL cell cannot select a silver object; results carry quality-mark counts |
| NB-03 | Every AI-written cell records writer, prompt, time and acceptor; editing keeps the history |
| NB-04 | An edit marks every dependent output stale before any rerun |
| NB-05 | No AI change applies without a per-cell accept; agent mode pauses before work over the cost budget, writes and deletes |
| NB-06 | Any write outside the notebook goes through the signing card |
| NB-07 | Promotion reruns from clean and records the notebook revision on the outcome |
| NB-08 | `.ipynb` round-trips code and Markdown; imported cells are untrusted until run on an executor |
| NB-09 | Tutor policy is enforced server-side; with Hints only, graded cells refuse Insert as code |
| NB-10 | Practice spaces never appear in search, charts, logs, counts or queues, and expose no record-creating verbs; Reset restores the fork point |

## Open questions

1. Engine: hybrid (recommended) or adopt an open-source reactive engine with its editor.
2. Format: the shipped percent `.py` with typed markers (recommended) or another `.py` dialect.
3. Reactive-lazy as the default (recommended) or linear with reactive opt-in.
4. Whether N3 teaching moves ahead of P6 for a course term.
5. Who may set tutor policy beyond instructors.
