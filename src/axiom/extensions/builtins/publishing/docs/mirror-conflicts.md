# Mirror conflicts, and how to resolve them

The document mirror keeps one local file in step with a remote editor (a
SharePoint/OneDrive doc, a shared-drive path). You edit wherever you like; the
agent edits the local file; `axi pub mirror sync` reconciles the two. Most of
the time one side moved and the other didn't, so a sync is just a pull or a
push. This page is about the one case that needs you: **both sides changed to
different text between two syncs.** That's a conflict.

The mirror copies Git's conventions here: a conflict is explicit, both sides are
kept, and **nothing auto-proceeds until you decide.**

## What happens when a conflict occurs

On a conflict the mirror does three things and then stops:

1. **The incoming remote text lands in your file.** The live document stays
   readable and correct — never littered with `<<<<<<<` markers. (Markers would
   be worse than Git here: the watcher would push them straight back to
   SharePoint and corrupt the canonical copy.)
2. **Your edit is preserved next to it**, in a sidecar named `doc.md.conflict`
   (then `doc.md.conflict.1`, and so on, if more pile up). Nothing you wrote is
   lost.
3. **Sync pauses.** Every following `sync` returns `blocked` and touches
   nothing — not your file, not the remote — until you resolve the conflict.
   The mirror will *not* quietly keep following the remote past a conflict.

## Seeing a blocked conflict

```
axi pub mirror status
```

A blocked mirror shows `"blocked": true` and a one-line `guidance` string
telling you where your edit was kept and what your choices are. A `sync` on a
blocked mirror returns the same guidance instead of doing work.

## Resolving it

Pick a side, exactly like Git:

```
axi pub mirror resolve <name> --theirs     # keep the incoming remote text
axi pub mirror resolve <name> --ours       # push your preserved edit instead
axi pub mirror resolve <name> --merged     # push whatever you hand-edit the file into
```

- **`--theirs`** — you're fine with the remote version that's already in your
  file. The sidecar is cleaned up and sync resumes.
- **`--ours`** — you want your edit to win. The mirror restores it from the
  sidecar and pushes it to the remote as the new canonical version.
- **`--merged`** — you want a blend. Open your file, use the `.conflict` sidecar
  as reference, edit the file into the merged result you want, then run
  `--merged`; the mirror pushes exactly what the file now holds.

After any of these, sync resumes and the next pass is a quiet `noop`.

### If the remote moved again while you were deciding

`--ours` and `--merged` push against the version you were shown at conflict
time. If someone edited the remote *again* in the meantime, the push is refused
and the conflict simply **re-opens against the newer remote** — your text is
preserved in a fresh sidecar, never overwritten blind. Look at the new remote
and resolve again.

## If the mirror lives in a Git repository

The conflict sidecar (`*.conflict`), the editor's version sidecar
(`*.mirrormeta.json`), and the mirror's state directory are working artifacts,
not content you want to commit. **The mirror keeps them out of Git for you.**
When you `mirror add` a document that lives in a Git repo — and again the first
time a conflict arises — it maintains a visible, clearly-marked block in the
repo's top-level `.gitignore`:

```gitignore
# >>> axiom mirror (managed) >>>
*.conflict
*.conflict.*
*.mirrormeta.json
.axi/publisher/mirror-state/
# <<< axiom mirror (managed) <<<
```

It is *visible and marked* (not a hidden `.git/info/exclude`) so you can see and
understand it, and it is written once — re-running never duplicates or churns it.
It is not auto-committed; commit the `.gitignore` change yourself when you're
ready. When `--git-annotate` is on, the mirror commits *only* the mirror file on
a clean sync and **does not** commit while a conflict is unresolved, so a blocked
conflict never lands in your history as a mystery edit.

## The one promise

At every step, both sides are on disk: the canonical text in your file, your
edit in the sidecar. The mirror never discards either one on its own — the only
thing that drops a side is you, choosing it, with `resolve`.
