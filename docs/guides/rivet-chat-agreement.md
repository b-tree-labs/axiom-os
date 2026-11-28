# RIVET and the owner's agreement in chat (shadow mode)

Decision: ADR-291. Requirement: `docs/prds/prd-rivet.md`, section 5a. Issue: #2471.

In shadow mode RIVET records, for each pull request it evaluates, whether it would have skipped the owner's GitHub review. It changes nothing: the lander lands and blocks exactly what it did before. To have anything to compare, the owner's Claude Code needs one hook, installed by the owner after reviewing it.

## What the hook does

On every submitted prompt it appends one line to a private local log, `~/.axi/agents/rivet/agreement/prompts.jsonl` (mode 0600, outside every repository): the prompt exactly as received, the session id, the prompt id, the transcript path and the time. It prints nothing, because anything a `UserPromptSubmit` hook prints on exit 0 is added to the model's context. It always exits 0, so it can never block or delay a prompt.

Claude Code also runs this hook for scheduled tasks, background agents reporting back and messages from other sessions, and gives the hook no field that says which. The log keeps them all and marks relayed shapes. Whether a person typed a prompt is decided later, from the transcript row's own `origin`.

## Install it (the owner's session does this, after review)

Add this to `~/.claude/settings.json`, merged into any existing `hooks` object. The interpreter must be one that has an Axiom build containing `axiom.extensions.builtins.release.agreement` (the service venv once this ships in a release):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "$HOME/.axi/service-venv/bin/python -m axiom.extensions.builtins.release.agreement hook  # axi-rivet-agreement",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

`python -m` starts in about 0.1 to 0.3 seconds. `axi release agreement hook` does the same through the CLI and is slower on every prompt. There is no matcher for this event. The trailing comment is a marker so a later change can find and remove it.

To remove it, delete that entry. The log stays on disk until the owner deletes it.

## The three commands

- `axi release agreement prompts --last 5` lists recent logged prompts with their ids. An agent finds the owner's yes here; it cannot supply the words.
- `axi release agree --repo OWNER/NAME --pr N --head SHA --message ID --work "..." --paths a/,b.py [--action tag]` records the agreement, citing the logged prompt by id. It refuses an id the hook never logged. `--paths` names the scope; a blanket scope counts as none. `--action` names an irreversible step (release, tag, node_change, secrets, delete_data) the owner agreed to by name.
- `axi release agreement verify --repo OWNER/NAME --pr N` says AGREED, NOT_AGREED or UNCERTAIN, with the reasons.

For a pull request that another session's agent opened, add `--handoff-kind cross-session-message --handoff-ref PATH`, where PATH is the transcript of the session that received a message citing the record id. The pointer says where to look; it approves nothing.

## Shadow reporting and the replay

The lander writes one row per pull request and head to `~/.axi/agents/rivet/agreement/shadow.jsonl`, next to its own event log: "would have skipped the owner's GitHub review: yes, no or uncertain". `axi release agreement replay` compares the latest row of each pull request with what the owner did on GitHub (approved unchanged, changed after, rejected, pending, merged without the owner's review) and reports the false skips. It says "ready" only with at least 20 agreed pull requests of known outcome and at most 5 percent changed or rejected afterwards.
