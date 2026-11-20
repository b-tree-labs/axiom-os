---
name: setup.run
description: Get this computer ready, asking before each change; ends with a checklist.
---

# setup.run

For each step in order (upgrade, vault, shell, node, key, any the product adds,
AI apps): check it; if it is not right, say what would fix it, ask, apply, and
check again. A step that needs another (the key needs the vault and the node)
waits while that one is red. Ends with a checklist: a green line for what works,
and for each red line the one thing to do about it.

`yes` answers every question; `dry_run` says what would change and changes
nothing. With no way to ask and no `yes`, nothing is changed. Running it again
skips what is done and repairs what drifted.
