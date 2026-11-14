# What to say to your agent

You run your day with a few short sentences. Type them as they are; there is nothing to paste
and nothing to fill in. Your agent already knows what each means: the steps behind every
sentence are standing instructions in `AGENTS.md` and the
[Part 3 agent reference](developer-workflow.md#when-the-person-says), which a coding agent reads
when it opens. They work in any program and any domain. A product built on Axiom may extend this
page with its own repositories and commands; if it does, use that version.

## Starting work

### Get me oriented.

**What you get back** (a description for you, not something to paste): the agent checks your
environment (your clone's own command runs, the environment is loaded, whether your dev node is
up, what is new on `main` in each repository); what changed since you last worked (commits
merged, your pull requests' review and CI state, issues assigned to you or mentioning you); and
your unfinished work in every clone (uncommitted changes, unpushed commits, open pull requests,
stashes). It reads the priorities (the program tracker, your tickets folder's order) and ends
with one recommendation: "finish X first", or "next: ticket Y", with why.

### Pick up where we left off.

Or *"What's next?"* **What you get back:** for unfinished work, what it was for, what is done and
what is left. For a new ticket, its contract, which phase of its PRD it belongs to, the plan and
where you are in it (which acceptance tests pass, which pull requests merged), and the next
concrete step. Nothing changes yet.

### Is it ready to build?

**What you get back:** whether the ticket has a design, a PRD requirement, an ADR for each
architecture choice, a plan or contract, and a reuse map, with the path to each. If something is
missing, it says exactly what and offers to draft it (a PRD section or an ADR from the
repository's templates) before any code is written.

### Start it.

**What you get back:** a feature branch in each repository the ticket names, in its order; the
acceptance tests, shown failing for the right reason; then the code that makes them pass. You
never have to ask for the tests first: every change is test-first.

## Finishing work

### Ready to push?

**What you get back:** each check the hook and CI will run, with its result; it pushes when you
say yes. Your agent runs these before every push anyway.

### Open the PR.

**What you get back:** a draft pull request with the standard sections, your reviewer requested,
and the address. It does not merge.

### Address the review.

**What you get back:** each comment with whether the agent agrees and the change it proposes.
After your yes it makes the changes as new commits, runs the checks, pushes and replies.

## Anytime

| You say | What you get back |
|---|---|
| *Set up my source clone.* | Your clone set up, and proof that its own command is the one that runs |
| *Update my setup.* | The setup rerun after a pull that changed it, and what changed |
| *Which mode am I in?* | The bare platform, the bare product, or the product with a site applied, with each fact it checked |
| *Does it match the design?* | Each difference between the running page and the design, as bug, missing piece or allowed deviation |
| *I'm stuck.* | The facts collected first, then the cause and the fix it proposes. Two failed tries at one fix and it stops to explain |

## What you never have to say

Your agent does these every time, unasked: acceptance tests before code; the checks before every
push; a reviewer requested before a pull request is ready; no attribution lines; one branch per
change.
