# Axiom — Documentation

Platform-level documentation. Extension-specific docs (PRDs, specs, and
extension ADRs) live with the extension under
`src/axiom/extensions/builtins/{ext}/docs/` per ADR-031.

The portfolio-wide rules — one folder per document kind, the
PRD / Tech Spec / ADR three-document rule, naming, and enforcement — are in
[`conventions/doc-standards.md`](conventions/doc-standards.md). This README
is the map of what this repo uses.

```
docs/
├── prds/          # Product requirements — WHAT/WHY (living)
├── specs/         # Technical specs — HOW (living); spec-aeos-* govern extensions
├── adrs/          # Decision records — one each, immutable; MADR-lite
├── conventions/   # Portfolio standards (doc-standards, the-axiomatic-way)
├── reference/     # External citations and reading notes
├── papers/        # Research papers
├── security/      # Security analyses
├── working/       # Scratch: session checkpoints, in-flight design docs
├── templates/     # Start here for a new PRD / spec / ADR
├── assets/  _tools/
└── README.md
```

Entry points: [`specs/spec-aeos-0.1.md`](specs/spec-aeos-0.1.md) (the
extension standard), the load-bearing ADR list in `AGENTS.md`, and
[`conventions/the-axiomatic-way.md`](conventions/the-axiomatic-way.md).

## Creating documents

- Start from [`templates/`](templates/). Filenames are lowercase-kebab,
  generic English nouns: `prd-<noun>.md`, `spec-<noun>.md`,
  `adr-NNN-<noun>.md`.
- ADR numbers: `python scripts/lint_adr_numbers.py --next` picks the next
  free number — never hand-pick (collision-prone).
- Axiom docs never name domain consumers (see `AGENTS.md`).
- `axi hygiene stat docs` audits this tree against the standard; TIDY runs
  it in the heartbeat sweep and proposes fixes for what it finds.
