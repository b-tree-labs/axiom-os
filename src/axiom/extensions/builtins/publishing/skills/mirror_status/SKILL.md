---
name: press.mirror_status
description: Show registered mirrors and their last-known state.
version: 0.4.0
inputs: []
outputs:
- kind: SkillResult
allowed-tools: []
generator: axi-skills-emit-md
---

Reads the mirror registry and each mirror's persisted sync state — no remote call is made.
