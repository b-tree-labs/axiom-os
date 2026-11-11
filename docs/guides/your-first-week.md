# Your first week

A day-by-day plan for a new contributor who works through a coding agent, in any program. It turns
[Part 3](build-a-feature.md) into a rhythm. A product built on Axiom may have its own version of this
page that adds its repositories and commands; if it does, follow that one.

**You work by talking to your agent.** Each step below is something you *say*. The agent does the
work, runs the commands and reports back. The exact prompts, and the commands behind them, are in
[Prompts for your agent](../reference/agent-prompts.md); paste them as they are. Read what the agent
reports and say yes or no. You do not need to remember any command on this page.

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
5. Say *"Start my session."* Every session begins this way: the agent updates `main` and its
   dependencies, loads your environment, checks which command runs, and tells you what is running.
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

1. *"Brief me on `<ticket>`."*
2. *"Write the acceptance tests first."* Watch them fail.
3. *"Make them pass, smallest change first."*
4. *"Check my change against the design."* (if the ticket has screens)
5. *"Before I push."*
6. *"Open the pull request."* Your reviewer is requested before it is marked ready.
7. *"Respond to review."* For each round of comments.

**Done when** the pull request has merged, and the agent has removed the worktree and branch.

## Day 5: the daily loop, on your own

| You say | The agent |
|---|---|
| *Start my session* | Updates `main` and dependencies, loads the environment, checks which command runs, reports what is running and what your ticket needs |
| *Update my setup* | Runs the setup again after a pull that changed it |
| *Brief me on `<ticket>`* | Reads the contract, screens and what exists to reuse; plans; changes nothing |
| *Write the acceptance tests first* | Writes them and shows them failing |
| *Check my change against the design* | Compares the running page with the design |
| *Before I push* | Runs every check the hook and CI will run, then pushes |
| *Open the pull request* | Opens it with the standard body and requests the reviewer |
| *Respond to review* | Proposes a change per comment, then makes the ones you agree to |
| *Which mode am I in?* | Says what is running, from facts it checked |

Keep at most three pull requests open, and finish one before you start a fourth.

## Where to get help

1. Say *"I'm stuck: diagnose."* It collects the facts before it tries a fix.
2. It reads [When something goes wrong](../reference/developer-workflow.md#when-something-goes-wrong)
   and the [FAQ](../reference/faq.md).
3. **Still stuck after two tries at the same fix?** Stop. Ask in your team's chat, with what you
   asked for, the first error and what was tried.

A refused push because `main` is red is not your fault and not yours to fix: tell your reviewer and
wait.

---

Next: [Part 4: Ship and operate](ship-and-operate.md). Index: [Guides](README.md).
