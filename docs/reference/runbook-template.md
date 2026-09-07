---
runbook: <slug>
audience: builder
surfaces: [package, cli, chat, notebook]
phases: [local, stage, prod]
---

# <Feature> — quickstart

One-paragraph statement of what a builder ends up with and the shape of the
path (copy → … → deployed). Say what they need up front (runtime, network,
credential).

<!-- Author jupytext-clean: fenced code blocks are the notebook cells; keep
     prose in markdown, no exotic markup, so this file doubles as the .ipynb. -->

---

## Local — build it on your machine

Pure, read-only credential, no deploy. Everything here runs against the served
endpoint (or a local target); nothing is promoted.

- **Install + connect once** (persisted config, with a live probe that fails
  fast on a bad credential).
- **Make a home** for the artifact — a plain file the builder owns, in their
  own folder, importing the library (not living inside it).
- **Explore** the inputs before choosing thresholds/parameters.
- **Write** the rule/logic — keep the core pure (no side effects) so it is
  testable and replayable.
- **Test** with canned inputs (no DB, no server).
- **Back-test / dry-run** over real history — the honest check.

## Stage — prove it

The gate between "works on my laptop" and shared.

- **PR** into the feature's repo; the CI **test ladder** runs.
- **Rehearsal** against the live endpoint (a scripted walkthrough + grade
  sheet), no severity-high findings.
- **Review** and merge.

## Prod — promote it

- **Tag** a release; the node installs it via the deploy script (or a
  self-hosted runner).
- It **runs for everyone** as a site extension / scheduled unit.
- It is **monitored** — a failure pages; staleness self-alerts.

---

## Surfaces (how this runbook is reached)

- **Package:** ships at `<extension>/docs/runbook-<slug>.md` (this file).
- **CLI:** `<cli> <noun> guide` prints/opens it; the steps are real verbs.
- **Chat:** indexed under `doc_class='builder'`; "how do I build/deploy X"
  routes here, and the agent can execute the steps via the same CLI tools.
- **Notebook:** opens as a runnable `.ipynb` (jupytext round-trip).

## Reference

Link the shipped API reference and the deeper walkthrough that live beside the
package, so a builder can go deeper without leaving the source.
