# Changelog — node (KEEL)

## 0.1.0 — 2026-10-31

- First release of KEEL, the one agent for the machine a node runs on (ADR-230).
- Verbs: `inventory`, `vitals`, `doctor`, `drift`, `report`, `declare`, `clean --plan`.
- Detects vLLM, Ollama, llama.cpp, SGLang, TGI and LiteLLM engines from their
  command lines; reads systemd units, listening ports, GPU use and disks.
- Declarations for engines run outside Axiom, storage tiers with floors, and
  tracked files.
- Drift rules: local-model pins vs ensure unit vs serving env (ADR-140), tracked
  file vs source, hand edits with no source.
- Hand-offs: HERALD for alerts, KEEP for exposed credentials, `update.check`,
  PLINTH's `data.storage`, `fleet.report`.
