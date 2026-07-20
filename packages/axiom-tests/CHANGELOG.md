# Changelog

All notable changes to `axiom-tests` are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this package adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Nothing yet.

## [0.1.0] - 2026-04-21

### Added

- Initial release of `axiom-tests` — AEOS-conformance test infrastructure
  for Axiom ecosystem extensions.
- `axiom_tests.plugin` registered as a `pytest11` entry point, exposing
  all fixtures to consumers with no `conftest.py` wiring required.
- Extension-level `ExtensionStandardTests` base class covering AEOS §5.2,
  §5.3, §6, §7 conformance (manifest validation, required files,
  `pyproject.toml` agreement, `__all__` declaration).
- Capability-kind unit-test base classes: `AgentTests`, `ToolTests`,
  `CommandTests`, `ServiceTests`, `AdapterTests`, `SkillTests`,
  `HookTests`, each using opt-in capability properties that default to
  `False` so unsupported capabilities skip automatically.
- Parallel integration-level base classes under
  `axiom_tests.integration_tests`, automatically marked with the
  `integration` pytest marker. `ExtensionIntegrationTests` additionally
  verifies that the extension package imports and that manifest
  `provides.entry` values resolve at runtime.
- Reusable fixtures: `mock_llm`, `mock_federation`, `mock_oidc`,
  `mock_registry`, `tmp_axiom_home`, `manifest_validator`,
  `hypothesis_strategies`.
- Hypothesis strategies for AEOS-relevant types (extension names, semver,
  capability blocks, minimal manifests).
- AEOS 0.1 JSON Schema bundled as package data at
  `axiom_tests/schemas/aeos-manifest-0.1.json`.
- Helper utilities `load_manifest`, `load_schema`, `validate_manifest`,
  `build_validator` exposed at the package root.
- Self-test suite covering plugin loading, fixture behavior, schema
  validation (known-good + known-bad manifests), and every base class's
  assertions, both via direct in-process invocation and via nested
  `pytester`-driven end-to-end runs.
- Python 3.11, 3.12, 3.13, and 3.14 support.

### Notes

- Leap-ahead AEOS features (behavioral attestation, quarantine/recovery,
  federation-shareable metadata) are deliberately out of scope for
  AEOS 0.1 base classes. They are scheduled for AEOS 0.2+ and the
  corresponding `axiom-tests` 0.2 line.
- The AEOS JSON Schema version tracked here is `0.1.0`.
