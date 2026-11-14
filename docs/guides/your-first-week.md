# Your first week

A day-by-day plan for a new contributor who works through a coding agent, in any program. It turns
[Part 3](build-a-feature.md) into a rhythm. A product built on Axiom may have its own version of this
page that adds its repositories and commands; if it does, follow that one.

**You work by talking to your agent.** Each step below is something you *say*. The agent does the
work, runs the commands and reports back. You paste nothing: the steps behind each sentence are
standing instructions your agent reads in `AGENTS.md`, and [What to say to your
agent](../reference/agent-prompts.md) lists the sentences and what each gives back. Read what the
agent reports and say yes or no. You do not need to remember any command on this page.

**Coming from your product's Parts 1 and 2?** Keep what they gave you: an installed copy of the
product's command, a site binding and its key, a data kit in progress. Nothing here asks you to
uninstall them. Your source clone adds a second copy of the command that runs your code; your
agent checks which one runs at the start of every session.

## Day 1: finish what is open, then set up

1. If your product's Part 2 has a last step open, say *"Check my data kit and tell me what is
   left."*
2. **Get access** to the repositories you will touch, and accept the invitations
   ([contributor quickstart, step 1](contributor-quickstart.md#1-get-access)).
3. **Connect your agent** ([Working with a coding agent](working-with-a-coding-agent.md),
   section 1).
4. Say *"Set up my source clone."* The agent clones, installs, sets your git identity, installs
   the hooks, starts a test database and runs the fast tests.
5. Say *"Get me oriented."* Every day begins this way: the agent checks your environment, tells
   you what changed while you were away and what is unfinished, and recommends one thing to do
   first.
6. Say *"Which mode am I in?"* and learn the answer: the bare platform, the bare product, or the
   product with a site applied ([Part 3, What am I running?](build-a-feature.md#4-what-am-i-running)).

**Done when** your agent reports green tests and the mode you are in, and how it knows.

## Day 2: hello world

Say *"Walk me through Build your first extension in a scratch folder."*
([the tutorial](build-your-first-extension.md)): one skill on every surface, then a small logbook.
It opens no pull request.

**Done when** you have seen your skill on the command line and over MCP.

## Day 3 and 4: a first small ticket, end to end

Your program's tickets folder names your first ticket. With no program, ask the platform owner
for a small one. Then, in order, say:

1. *"Get me oriented."* It names your first ticket.
2. *"Pick up where we left off."* It finds the ticket's contract, its PRD phase and the plan.
3. *"Is it ready to build?"* It offers to draft whatever is missing before any code.
4. *"Start it."* It branches, shows you the acceptance tests failing, then makes them pass. You
   never ask for the tests first: every change is test-first.
5. *"Does it match the design?"* (if the ticket has screens)
6. *"Open the PR."* It runs every check first; your reviewer is requested before it is ready.
7. *"Address the review."* For each round of comments.

**Done when** the pull request has merged, and the agent has removed the worktree and branch.

## Day 5: the daily loop, on your own

| You say | The agent |
|---|---|
| *Get me oriented.* | Checks your environment, what changed while you were away and what is unfinished, and recommends one thing to do first |
| *Pick up where we left off.* (or *What's next?*) | Finds where the work stands (the ticket, its PRD phase, the plan, which tests pass) and the next step |
| *Is it ready to build?* | Checks for a design, a requirement, the ADRs and a plan; offers to draft what is missing |
| *Start it.* | Branches, shows the acceptance tests failing, then builds |
| *Does it match the design?* | Compares the running page with the design |
| *Ready to push?* | Runs every check the hook and CI will run, then pushes on your yes |
| *Open the PR.* | Opens it with the standard body and requests the reviewer |
| *Address the review.* | Proposes a change per comment, then makes the ones you agree to |
| *Update my setup.* | Runs the setup again after a pull that changed it |
| *Which mode am I in?* | Says what is running, from facts it checked |

You never have to ask for the acceptance tests, the checks before a push or a reviewer: your
agent does them every time.

Keep at most three pull requests open, and finish one before you start a fourth.

## Where to get help

1. Say *"I'm stuck."* It collects the facts before it tries a fix.
2. It reads [When something goes wrong](../reference/developer-workflow.md#when-something-goes-wrong)
   and the [FAQ](../reference/faq.md).
3. **Still stuck after two tries at the same fix?** Stop. Ask in your team's chat, with what you
   asked for, the first error and what was tried.

A refused push because `main` is red is not your fault and not yours to fix: tell your reviewer and
wait.

---

Next: [Part 4: Ship and operate](ship-and-operate.md). Index: [Guides](README.md).
