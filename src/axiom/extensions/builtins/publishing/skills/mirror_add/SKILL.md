---
name: press.mirror_add
description: Register a local mirror of a remote-editor document.
version: 0.4.0
inputs:
- name: local
  type: Path
- name: url
  type: str
- name: name
  type: str | None
- name: git_annotate
  type: bool | None
outputs:
- kind: SkillResult
allowed-tools: []
generator: axi-skills-emit-md
---

Names a document that lives in a remote web editor and is mirrored to a local file. The registry entry carries the local path, the share URL, and whether sync events are annotated as git commits when the mirror lives inside a repository.
