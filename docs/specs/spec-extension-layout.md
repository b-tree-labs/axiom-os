# Extension Layout Specification

**Status:** Active
**Audience:** Extension authors (human and agent contributors)
**Parent:** [spec-aeos-0.1.md](spec-aeos-0.1.md) — full AEOS specification
**Related:** [ADR-031](../adrs/adr-031-extension-self-containment.md) — self-containment doctrine
**Last Updated:** 2026-04-21

---

## What This Document Is

A short, tactical reference for day-to-day extension work. If you need the full spec, rationale, capability definitions, or standards-alignment details, read `spec-aeos-0.1.md`. This document tells you where to put what.

---

## Canonical Layout

```
<extension-name>/
├── <extension-name>/                   # Python package; same name as directory
│   ├── __init__.py                     # Public API via __all__
│   ├── agents/                         # Agent capabilities (optional)
│   │   └── <name>/
│   │       ├── agent.py
│   │       └── persona.md              # agent's own system prompt (NOT a standalone skill)
│   ├── tools/                          # Tool capabilities (optional)
│   ├── commands/                       # CLI command capabilities (optional)
│   ├── services/                       # Service capabilities (optional)
│   ├── adapters/                       # Adapter capabilities (optional)
│   ├── skills/                         # STANDALONE reusable SKILL.md skills (optional)
│   │                                   # NOT bound to any agent; invokable by any agent with access
│   ├── hooks/                          # Hook capabilities (optional)
│   ├── signals.py                      # Signal type registrations (optional)
│   ├── _internal/                      # Private; never imported externally
│   └── py.typed                        # PEP 561 type marker
├── tests/
│   ├── unit_tests/
│   │   └── test_standard.py            # Must inherit from axiom_tests
│   ├── integration_tests/
│   ├── fixtures/
│   └── conftest.py
├── docs/
│   ├── prds/
│   │   └── prd.md
│   ├── specs/
│   │   └── spec.md
│   ├── decisions/                      # Extension-level ADRs
│   ├── working/                        # In-flight design docs
│   └── reference/                      # Generated API reference
├── README.md                           # User-facing landing
├── CHANGELOG.md                        # Keep-a-Changelog format
├── LICENSE
├── AGENTS.md                           # Optional; coding-agent guidance
├── pyproject.toml                      # Python packaging
└── axiom-extension.toml                # AEOS manifest
```

Populate only the capability-kind subdirectories your extension provides. Delete or omit the others.

---

## Naming Rules

### Directory and package name

- Purpose-named. Describes what the extension does or which domain it serves.
- Normal case (lowercase with underscores). Directory name and Python package name are identical.
- No type suffix. Never `_agent`, `_tool`, `_cmd`, etc.

**Valid:** `classroom`, `connect`, `memory`, `syllabus_extraction`, `reactor_physics`

**Invalid:** `classroom_domain`, `memory`, `connect_adapter`, `syllabus-extraction` (no hyphens in Python packages)

### Agent names inside the extension

Agents follow the AXI convention (ALL-CAPS-HYPHEN) as their identity:
- SCAN, TIDY, PRESS, TRIAGE, CURIO, AXI, CHALKE, WARDEN

In code, use snake_case module names but the agent's string identity preserves the convention:

```python
# classroom/agents/chalke/agent.py
class ChalkeAgent:
    name = "CHALKE"
    ...
```

---

## Required Files

Every extension MUST have:

1. **`<extension>/__init__.py`** with `__all__` enumerating public symbols
2. **`pyproject.toml`** with project metadata and entry points
3. **`axiom-extension.toml`** with AEOS manifest
4. **`README.md`** — user-facing overview
5. **`CHANGELOG.md`** — Keep-a-Changelog format
6. **`LICENSE`** — license file
7. **`tests/unit_tests/test_standard.py`** inheriting from an `axiom_tests` base class

Optional but strongly recommended:
- `AGENTS.md` for coding-agent guidance
- `docs/` with PRD and spec (required for any extension with ≥2 capability kinds)
- `py.typed` file for PEP 561

---

## Public API via `__init__.py`

Every extension's `__init__.py` declares what is public:

```python
"""classroom — classroom learning management and analytics."""

from classroom.agents.chalke import ChalkeAgent
from classroom.tools.syllabus_extraction import SyllabusExtractor
from classroom.commands.enrollment import cli as enrollment_cli

__all__ = [
    "ChalkeAgent",
    "SyllabusExtractor",
    "enrollment_cli",
]
```

Everything NOT in `__all__` is private. Other extensions must not import private symbols. Use `_` prefix or the `_internal/` subpackage for things that should never escape.

Import-linter at the monorepo root enforces this automatically.

---

## Capability Kinds (Seven)

Each capability kind has a dedicated subdirectory and a manifest block. Seven total:

| Kind | Directory | When to use | Scope |
|---|---|---|---|
| **agent** | `agents/` | LLM-backed, persistent identity, state | Instance is an agent; may invoke skills |
| **tool** | `tools/` | Stateless callable with typed schemas | Instance is a callable |
| **cmd** | `commands/` | CLI nouns/verbs extending `axi` or `neut` | Instance is a CLI group |
| **service** | `services/` | Long-running daemon | Instance is a process |
| **adapter** | `adapters/` | Third-party integration (LMS, IdP, etc.) | Instance is an integration |
| **skill** | `skills/` | agentskills.io SKILL.md skill | **Standalone and reusable — NOT bound to any agent; any agent with access may invoke** |
| **hook** | `hooks/` | Lifecycle interceptor | Instance is an event handler |

**Important distinction:** An agent's own system prompt / persona (the "what this agent is" document) lives inside the agent's directory as `agents/<name>/persona.md`. This is internal to the agent — it is NOT a standalone skill and does NOT appear in the `skills/` directory. Use `skills/` only for reusable instruction documents that multiple agents (or any agent) can invoke. An agent declares which standalone skills it may invoke via its `uses_skills` manifest field.

Your extension can provide any combination. Most provide 1-3.

---

## Manifest (`axiom-extension.toml`)

Minimum viable manifest:

```toml
[extension]
name = "my_extension"
version = "0.1.0"
description = "What this extension does"
license = "Apache-2.0"
aeos_version = "0.1.0"
owner = "b-tree-labs"  # or "ut-austin", etc.

[extension.compatibility]
python = ">= 3.11"
axiom = ">= 0.14"

[[extension.provides]]
kind = "tool"
name = "my_tool"
entry = "my_extension.tools.my_tool:MyTool"
description = "Does the thing"
```

See spec-aeos-0.1.md §6 for the full schema, optional sections (federation, signing, testing), and all fields per capability kind.

---

## Pyproject.toml Entry Points

Capability `entry` values in the AEOS manifest map to entry points in `pyproject.toml`:

```toml
[project]
name = "my_extension"
version = "0.1.0"
# ...

[project.entry-points."axiom.agents"]
chalke = "classroom.agents.chalke:ChalkeAgent"

[project.entry-points."axiom.tools"]
syllabus_extraction = "classroom.tools.syllabus_extraction:SyllabusExtractor"

[project.entry-points."axiom.commands"]
enrollment = "classroom.commands.enrollment:cli"
```

Axiom loads capabilities via `importlib.metadata.entry_points()` at startup. `axi ext lint` verifies manifest entries match declared entry points.

---

## Tests — Standard Base Classes

Every extension has at minimum `tests/unit_tests/test_standard.py`:

```python
from axiom_tests.unit_tests import ExtensionStandardTests

class TestMyExtensionStandard(ExtensionStandardTests):
    @pytest.fixture
    def extension_manifest_path(self):
        return Path(__file__).parent.parent.parent / "axiom-extension.toml"
```

For each capability kind, add a test class inheriting from the kind-specific base:

```python
from axiom_tests.unit_tests import ToolTests

class TestSyllabusExtraction(ToolTests):
    @pytest.fixture
    def tool_class(self):
        from my_extension.tools.syllabus_extraction import SyllabusExtractor
        return SyllabusExtractor
    
    @property
    def supports_streaming(self) -> bool:
        return False
    
    @property
    def idempotent(self) -> bool:
        return True
```

Capability properties default to `False`. Override to opt-in. Standard tests skip tests for unsupported capabilities automatically.

Fixtures (`mock_llm`, `mock_federation`, `tmp_axiom_home`, etc.) are registered via the `axiom-tests` pytest plugin — available automatically once `axiom-tests` is in dev dependencies.

---

## Cross-Extension Imports

Rules, enforced by import-linter at repo root:

- Extensions may import from `axiom` (core) and from third-party packages declared in `pyproject.toml`
- Extensions may import from another extension's public API (`__all__` only)
- Extensions MUST NOT import another extension's private modules (prefix `_`, inside `_internal/`, or not listed in `__all__`)

If you need something private from another extension, promote it to that extension's public API (file an issue there for a minor version bump) or move it to `axiom` core if it's genuinely shared infrastructure.

---

## Docs — What Goes Where

Inside an extension's `docs/`:

- `prds/prd.md` — product requirements for this extension
- `specs/spec.md` — technical specification
- `decisions/adr-NNN-<title>.md` — extension-level architectural decisions
- `working/` — session checkpoints, in-flight design docs
- `reference/` — generated API reference (often auto-generated from docstrings)
- `overview.md` — user-facing overview (sometimes serves as README.md's detailed companion)

`axiom/docs/` (core-level) contains only platform-wide content: ADRs affecting multiple components, portfolio strategy, research papers, core specs (spec-security, spec-memory, etc.). Extension-specific material lives in the extension.

---

## CHANGELOG Format

Keep-a-Changelog format, semver-aligned:

```markdown
# Changelog

All notable changes to this extension are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this extension adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Nothing yet

## [0.2.0] - 2026-05-01

### Added
- New `syllabus_extraction` tool with PDF support

### Changed
- Migrated to AEOS 0.1 layout (ADR-031)

### Removed
- Legacy `_agent` suffix on directory name
```

---

## Common Pitfalls

- **Missing `__all__`** — other extensions won't know what's public; import-linter may over-restrict
- **Directory name doesn't match package name** — breaks Python import resolution
- **`[[extension.provides]]` without matching entry point** — `axi ext lint` fails
- **`axiom-extension.toml` out of sync with `pyproject.toml`** — name/version/description drift
- **Private import reach into another extension** — import-linter blocks merge
- **Standard test missing** — `axi ext lint` fails
- **`_agent`, `_tool`, or other type suffix in new extension name** — reject; rename to purpose
- **Scope creep during migration** — migrate layout only; don't refactor internals

---

## Quick Commands

```bash
axi ext init my_extension              # Scaffold a new extension
axi ext lint                           # Verify conformance
axi ext test                           # Run extension tests
axi ext doctor                         # Diagnose issues
axi ext validate                       # Deep check: load and verify declarations
axi ext publish                        # Sign + release to registry
```

See spec-aeos-0.1.md §10 for the full CLI reference.

---

## If You Need More

- **Full AEOS specification:** [spec-aeos-0.1.md](spec-aeos-0.1.md)
- **Self-containment rationale:** [ADR-031](../adrs/adr-031-extension-self-containment.md)
- **Standards positioning:** [ADR-032](../adrs/adr-032-standards-positioning-dual-track.md)
- **Operational guide:** [aeos-playbook.md](../working/aeos-playbook.md)
- **Portfolio context:** [brand-product-strategy.md](../working/brand-product-strategy.md)
