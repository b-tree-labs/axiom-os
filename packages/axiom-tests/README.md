# axiom-tests

Reusable test infrastructure for Axiom ecosystem extensions.

`axiom-tests` provides **base classes, shared fixtures, mocks, Hypothesis
strategies, and schema validators** that any Axiom-ecosystem extension can
inherit and compose. Extensions use `axiom-tests` to avoid reimplementing
common test scaffolding — it is **not** a home for extension-specific tests.

Current coverage (0.1.0): AEOS conformance. Future releases add base classes
and fixtures for memory, RAG, gateway, prompts, sessions, federation, signals,
and other Axiom concerns. See `ROADMAP.md` for the expansion plan.

Today's scope:

- Abstract base `TestCase` classes per AEOS capability kind (agent, tool, cmd,
  service, adapter, skill, hook)
- An extension-level `ExtensionStandardTests` base that verifies manifest,
  layout, and public-API conformance
- Reusable pytest fixtures for mocked LLMs, federation peers, OIDC, the Vyzier
  registry, and an isolated `~/.axiom/` home
- Hypothesis strategies for property-based testing of AEOS-relevant types
- The AEOS 0.1 JSON Schema for `axiom-extension.toml` manifests

The package is registered as a `pytest11` plugin, so installing it is all that
is required — no `conftest.py` wiring on the consumer side.

## Scope Boundary

**Extension-specific tests live in the extension's own `tests/` directory**,
not here. Per ADR-031, every extension is self-contained: its PRD, spec, tests,
CHANGELOG, and manifest travel with the extension. `axiom-tests` provides the
reusable primitives those tests inherit from — no more, no less.

Decision rule when considering whether to add something here: _would at least
two unrelated extensions need this primitive?_ If yes, `axiom-tests`. If no,
the extension.

See also:

- AEOS specification — `docs/specs/spec-aeos-0.1.md`
- Extension layout — `docs/specs/spec-extension-layout.md`
- ADR-031 — extension self-containment
- `ROADMAP.md` — what's planned and what it takes to add scope

## Installation

```bash
pip install axiom-tests
```

Once installed in the same environment as your test runner, the fixtures
and markers are available automatically.

## Standard usage from an extension's test suite

Create `tests/unit_tests/test_standard.py` inside your extension and inherit
from the base classes:

```python
from pathlib import Path

import pytest

from axiom_tests.unit_tests import (
    ExtensionStandardTests,
    ToolTests,
    AgentTests,
)


class TestClassroomExtension(ExtensionStandardTests):
    @pytest.fixture
    def extension_manifest_path(self) -> Path:
        return Path(__file__).parent.parent.parent / "axiom-extension.toml"


class TestSyllabusExtractionTool(ToolTests):
    @pytest.fixture
    def tool_class(self):
        from classroom.tools.syllabus_extraction import SyllabusExtractor
        return SyllabusExtractor

    @property
    def supports_streaming(self) -> bool:
        return False

    @property
    def idempotent(self) -> bool:
        return True


class TestChalkeAgent(AgentTests):
    @pytest.fixture
    def agent_class(self):
        from classroom.agents.chalke import ChalkeAgent
        return ChalkeAgent

    @property
    def implements_plan(self) -> bool:
        return True
```

`pytest` picks up the fixtures registered by the plugin, the base classes
contribute the standard test methods, and capability properties you do not
override default to `False` (so tests for unsupported capabilities skip
automatically).

## Capability kinds covered

| Kind | Base class | What it checks |
|---|---|---|
| `extension` | `ExtensionStandardTests` | Manifest, required files, `__all__`, pyproject agreement |
| `agent` | `AgentTests` | Name convention, interface, persona, `uses_skills` |
| `tool` | `ToolTests` | `input_schema`/`output_schema`, idempotency, side effects |
| `cmd` | `CommandTests` | Noun, subcommands, argument parsing |
| `service` | `ServiceTests` | `start`/`stop`/`status`/`health_check` |
| `adapter` | `AdapterTests` | Connection interface, auth methods, capabilities |
| `skill` | `SkillTests` | `SKILL.md` frontmatter, referenced dirs |
| `hook` | `HookTests` | Event names, fail mode |

## Fixtures provided

- `mock_llm` — canned-response fake LLM (sequence or rule mode)
- `mock_federation` — in-process fake federation peer and artifact fabric
- `mock_oidc` — fake OIDC IdP issuing deterministic tokens
- `mock_registry` — fake Vyzier registry serving manifests + signatures
- `tmp_axiom_home` — isolated temporary `~/.axiom/` directory per test
- `manifest_validator` — AEOS JSON Schema validator (session-scoped)
- `hypothesis_strategies` — bundle of Hypothesis strategies for AEOS types

Markers:

- `@pytest.mark.integration` — applied automatically to integration-level
  base classes; use `pytest -m integration` or `-m "not integration"` to
  filter.
- `@pytest.mark.aeos_capability(kind)` / `aeos_conformance(level)` — available
  for ad-hoc labelling.

## Integration-level test base classes

Parallel classes under `axiom_tests.integration_tests` add integration-level
smoke tests on top of the unit-level checks — e.g.
`ExtensionIntegrationTests` verifies the extension package actually imports
and that every `provides.entry` in the manifest resolves at runtime.

## License

Apache-2.0. Copyright (c) 2026 B-Tree Ventures, LLC.
