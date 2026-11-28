# ADR-291: RIVET lands work the owner agreed to in chat when the harness's own record proves it

**Status:** Proposed (2026-11-25). The owner confirmed the design and its answers in chat on 2026-11-25 (issue #2471, three comments). It stays Proposed until the shadow results in "Going live" are reported and the owner accepts them; this ADR and its first PR change nothing the lander lands or blocks.
**Related:** ADR-285 (RIVET decides on content and earns autonomy from a record), ADR-257 D11 (irreversible steps keep an explicit yes), `docs/prds/prd-rivet.md` section 5a, issues #2471 and #2147.

## Context

The owner designs a change in a chat session, looks at what it produced and says "build it". A GitHub review is then asked for the same work: open the PR, read the code, approve, mark ready. They almost always approve and almost never change details, and call the second step redundant. They asked for their local RIVET to connect the chat to the PR and to require their GitHub review only when it is uncertain they saw and agreed to the work.

The risk is plain. If RIVET skips a review on the strength of anything a model can write, then any text that reaches a model can approve a change: a pull request body, a tool result, a message another session sent, a file an agent reads. Industry cases of agents hijacked through pull request text make this a requirement, not a corner case.

## Decision

1. **The evidence is on disk and written by the harness.** Two files, both outside every repository (under `<state>/agents/rivet/agreement/`, mode 0600): the prompt log, which a `UserPromptSubmit` hook appends to (the harness runs the hook, not the model), and the session transcript Claude Code writes under `~/.claude/projects/`. An agreement record is only a pointer: it cites one logged prompt by id and its hash. The text of the owner's words is never an argument to the command that creates a record.
2. **Only a row the harness marks as typed by a person is the owner.** A transcript `user` row counts only when `origin.kind` is `human`, it is not `isMeta`, has no `toolUseResult`, and is not a command wrapper. A row with no `origin` never counts. Tool results, messages from another session (`origin.kind` `peer`), task notices, compaction summaries, scheduled wake-ups and automatic continuations never count, whatever they say.
3. **A yes is read conservatively, with no model call.** Quoted lines, code fences and pasted blocks are removed first. A reply counts only if it is short, opens with an affirmative phrase, carries no hedge, and adds nothing but more agreement or an action word. A question, a condition or an explanation is "cannot tell".
4. **The yes must answer a message that names the work.** The assistant message just before it must name the work (its ticket, its branch, or most of its words), or the reply is uncertain.
5. **Bound to scope, not blanket.** The record names the paths the work covers. A blanket scope (`*`, empty, `.`) is no scope. A later head inherits the agreement only while every changed path stays inside it; anything outside is reported as the new part, and only that is asked about.
6. **Irreversible steps are included, and the record must name them.** A release, a tag, a node change, secrets and deleting data are agreed only when the record lists that action and the owner's reply, or the assistant message it answered, names it. A yes to "build it" is not a yes to "tag it". The pull request's own title, branch, labels and paths can only add to the actions that must be named, never remove one.
7. **Newest message wins, and silence never extends.** A later hold, wait, stop or no from the owner in any local session voids the record: a short message with a hold word always, a longer one when it names this pull request, its branch or its work. The record expires 24 hours after the yes.
8. **Carry across sessions depends on timing, work and a pointer.** If the session that heard the yes never mentions the pull request, the yes carries only when all three hold: the work matches (the ticket or branch was named to the owner, or every path of the scope was said to them); the pull request's first commit is after the yes and inside the window (unknown means no); and a handoff pointer exists, a message from another session or a memory note that cites the record id. The pointer says where to look and approves nothing. The record is still checked against the transcript of the session that heard the owner.
9. **Local sessions only.** The transcript must be under this machine's projects folder.
10. **Uncertain blocks.** The verifier returns AGREED, NOT_AGREED or UNCERTAIN. Only AGREED can skip the owner's GitHub review. Everything else waits for it exactly as today.
11. **Shadow first.** For every pull request the lander evaluates, RIVET records "would have skipped the owner's GitHub review: yes / no / uncertain" next to its own decision record, changes no verdict, and a replay compares the rows with what the owner then did on GitHub (approved unchanged, changed after, rejected).
12. **Visible.** When this goes live, a pull request that skipped review says so in its note ("agreed in session `<id>` at `<time>`: `<the owner's words>`"), and the weekly note counts them.

## Threat model

- **Text can never create an agreement.** Text in a pull request, in a tool result, in a message from a peer or relayed session, or written by another agent has no path into the verifier: the pull request subject it reads has no body or comment field, and the cited prompt must exist in the log and in the transcript as a row the harness marked human.
- **The evidence is the session transcript on disk.** The prompt log is a second witness, not the first. A forged record, a line added to the log by hand, or a record whose hash does not match all fail against the transcript.
- **A model that can run shell commands as the owner can write both files.** The hash chain on the log makes an edit or a removal in the middle visible, not an append. This design defends against injection, honest mistakes and relays, not against an agent that deliberately forges the owner's local files. The defence for that is the owner's own account boundary and, if wanted later, a key the model cannot read.
- **Irreversible actions need the record to name them,** and the pull request can only widen what must be named.
- **The hook cannot tell relayed prompts apart.** `UserPromptSubmit` also fires for scheduled tasks, background agents reporting back and messages from other sessions, with no field that says which. The log therefore keeps every prompt as received and marks relayed shapes; the verifier decides from the transcript row's `origin`.

## Going live

Not in this change. The shadow replay must show the owner approved nearly all the agreed pull requests unchanged: at least 20 agreed pull requests with a known outcome, and at most 5 percent changed or rejected afterwards. The owner decides, and the first live step is tier 1 paths. Going live is a separate change that edits the lander and amends this ADR.

## Consequences

- RIVET gains a verifier it can trust more than a label, and the owner's attention is spent where it is uncertain whether they saw the work.
- The owner installs one hook in their own Claude settings after review (`docs/guides/rivet-chat-agreement.md`). Without it nothing is logged and every verdict is no.
- A real cost: a short yes after a clear proposal is the common case, and the rules are strict, so many real agreements will read as UNCERTAIN at first. That is the safe side, and the shadow replay measures it.

## Not decided here

- Whether the 24-hour window should vary by area (shorter for risky paths, longer for docs).
- Whether a yes spoken after the owner saw the pull request's code ("approve and land") should count for the head it saw, as it does here, or only for work discussed before the code existed.
- A model-assisted reading of replies, if the conservative rules turn out too strict. It would never be allowed to turn UNCERTAIN into AGREED without the shadow evidence.
