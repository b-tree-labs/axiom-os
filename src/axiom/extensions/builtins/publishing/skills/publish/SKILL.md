---
name: press.publish
description: Draft + upload + notify via the event bus.
version: 0.4.0
inputs:
  - name: source
    type: Path
  - name: scope
    type: str | None
outputs:
  - kind: SkillResult
allowed-tools: []
---

End-to-end publish: builds the artifact, uploads to the configured provider, and emits an event on the platform EventBus so HERALD (and any other agent_bridge consumer) can broadcast the announcement. Per ADR-060 this skill never imports a NotificationProvider directly — routing happens through the bus.
