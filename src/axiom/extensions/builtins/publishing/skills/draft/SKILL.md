---
name: press.draft
description: Render a draft artifact locally (no upload).
version: 0.4.0
inputs:
  - name: source
    type: Path
outputs:
  - kind: SkillResult
allowed-tools: []
---

Resolves the source path, instantiates the PublisherEngine, and generates the draft output in the source's scope. No upload to OneDrive or other providers happens here — `press.draft` is the dry-run companion to `press.publish` and is what authors iterate against locally before flipping the publish bit.
