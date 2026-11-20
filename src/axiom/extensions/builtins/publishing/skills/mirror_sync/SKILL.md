---
name: press.mirror_sync
description: Reconcile registered mirrors with their remote editors.
version: 0.4.0
inputs:
- name: name
  type: str | None
- name: watch
  type: bool | None
outputs:
- kind: SkillResult
allowed-tools: []
generator: axi-skills-emit-md
---

One directional pass per mirror: the remote editor is canonical, the local file follows, and local edits push with the version they expect. With watch=true the pass also detects a remote save that matches an old base — a stale editor session flushing its buffer — and repairs it instead of following it.
