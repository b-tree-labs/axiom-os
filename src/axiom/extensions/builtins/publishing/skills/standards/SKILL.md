---
name: press.standards
description: List the registered PRESS standards bundles.
version: 0.4.0
inputs:
  - name: category
    type: str | None
outputs:
  - kind: SkillResult
allowed-tools: []
---

Returns the standards bundles available to PRESS — name, description, category, version, tags, and the underlying skill steps. Used by `axi publish standards list` and by agent reasoning loops that need to enumerate which publishing recipes are installed.
