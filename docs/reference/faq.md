# FAQ

Short answers to questions new contributors have asked, with a link to the full answer. Add a
question when someone asks one that is not here. Keep each answer to a few sentences.

## Contents

1. [Access and setup](#access-and-setup)
2. [Nodes and sites](#nodes-and-sites)
3. [Tests and checks](#tests-and-checks)
4. [Pull requests](#pull-requests)
5. [Words](#words)

## Access and setup

**Where do I start?** With your product's own onboarding, Parts 1 and 2. Then
[Part 3: Build a feature](../guides/build-a-feature.md). The reading order is in the
[guides index](../guides/README.md).

**I cannot clone the repository; it says it does not exist.** You have not accepted the access
invitation yet, or it has not been sent. See the
[contributor quickstart, step 1](../guides/contributor-quickstart.md#1-get-access).

**My change does nothing when I run it.** You are probably running another checkout's code. See
[Which code am I running?](../guides/run-from-source.md#which-code-am-i-running)

**Which coding agent should I use?** Any. [Working with a coding agent](../guides/working-with-a-coding-agent.md)
shows how to connect one and hold it to the rules.

## Nodes and sites

**Am I looking at the platform, the product, or a site?** Read the `Running:` block `dev up` and
`dev status` print (product, platform, site look, data, database, sign-in). A site binding changes which node's data you read, not the look.
See [Part 3, What am I running?](../guides/build-a-feature.md#4-what-am-i-running); one switch and
a "Running" banner in the web app are coming (#1716).

**My product clone does not have a change that merged in Axiom.** A product installs Axiom as a
released package, so it gets the change with the next release (`pip install -U axiom-os-lm`).
See [Part 4](../guides/ship-and-operate.md#2-build-and-provenance).

**Which node do I use?** Your product's setup command should find it for you; that work is
tracked in the product's own repository. Until it does, your site's own documentation names the
node and how to reach it. For a first change in Axiom you need no node: you run everything on your
own computer.

**What is the difference between a node and a site?** A site is one organization's install. A node
is one running server. A site can have more than one node. See the
[glossary](glossary-axiom.toml).

## Tests and checks

**What does "green" mean?** Every test passed. `skipped` is fine; `failed` or `error` is not.

**A test failed that has nothing to do with my change.** Run it alone on a clean `main` worktree.
If it fails there too, it is not yours. A nearly full disk and a busy computer are the two common
causes; see [Known traps](developer-workflow.md#known-traps).

**The push was refused because `main` is red.** Someone else's change broke `main`. Wait for the
fix, and tell the platform owner if nobody is on it. See
[What needs a person](developer-workflow.md#what-needs-a-person).

**The hook committed something and stopped.** It repaired lint or the generated context files.
Run `git push` again.

**Can I skip the hook?** No. `--no-verify` needs a written reason and a person's yes. A missing
test database is never a reason; start it as the
[quickstart, step 3](../guides/contributor-quickstart.md#3-install-the-hooks-and-a-test-database) shows.

## Pull requests

**Who reviews my pull request?** Your program lead names a reviewer. If you have no program, ask
the platform owner.

**Why should I ask for a reviewer before marking it ready?** In Axiom a green pull request with no
review pending can be merged automatically by the lander. A requested review holds it.

**My pull request is green but nothing happens.** It may still be a draft, or waiting for its
review, or it touches a protected path that waits for a person. See
[The lander](developer-workflow.md#the-lander-rivet).

**Should I press "Update branch"?** No. It reruns CI at full cost, and your branch does not need
to be up to date with `main`.

## Words

New words are defined where they first appear and collected in the
[glossary](glossary-axiom.toml) (`axi glossary --layer axiom`).

See also: [Part 3 agent reference](developer-workflow.md),
[Part 4 agent reference](ship-and-operate-reference.md), [What to say to your agent](agent-prompts.md).
