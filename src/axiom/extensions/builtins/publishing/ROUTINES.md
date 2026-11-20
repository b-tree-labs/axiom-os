# PRESS Routines

> OpenClaw HEARTBEAT.md equivalent — defines PRESS's continuous operational loops.

## Always-On File Watcher

PRESS watches source document directories for changes and auto-publishes:

| Watcher | Interval | Cooldown | Description |
|---------|----------|----------|-------------|
| **source_docs** | 10s | 300s | Poll for changed .md files, wait 5min after last edit before publishing |

## Publishing Cycle (on change detected)

When a source document has been stable for the cooldown period:

1. Detect changed files via mtime comparison
2. Render Mermaid diagrams (if mmdc available)
3. Convert Markdown to .docx via Pandoc
4. Apply document template (headers, footers, version)
5. Upload to configured output (OneDrive, local dir)
6. Update `.doc-registry.json` with new version

## Heartbeat (10s)

- Check publish queue depth
- Report last successful publish timestamp
- Verify tool availability (Pandoc, mmdc)

## On Startup

- Rebuild file state index from `.doc-registry.json`
- Check for documents modified while PRESS was stopped
_Copyright (c) 2026 The University of Texas at Austin and B-Tree Labs. Apache-2.0 licensed._
