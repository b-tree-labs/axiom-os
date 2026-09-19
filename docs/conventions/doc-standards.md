# Documentation Standards

The portfolio-wide standard for organizing written documentation in any
Axiom-family repository. `axi hygiene stat docs` checks a repo against this
document; TIDY's `doc-hygiene` skill proposes fixes for what it finds.

## One folder per document kind

Every document lives in exactly one `docs/` subfolder named for its kind.
A repo only creates the folders it needs — but when a kind appears, it uses
the canonical name:

| Folder | Kind | Filename pattern | Lifecycle |
|---|---|---|---|
| `docs/prds/` | Product requirements — WHAT a surface must do and WHY | `prd-<noun>.md` | Living |
| `docs/specs/` | Technical specs — HOW a subsystem is built | `spec-<noun>.md` | Living |
| `docs/adrs/` | Decision records — ONE decision each | `adr-NNN-<noun>.md` | Immutable once accepted |
| `docs/guides/` | How-tos for people | free | Living |
| `docs/runbooks/` | Operational procedures | free | Living |
| `docs/reference/` | Catalogs and lookup material | free | Living |
| `docs/strategy/` | Execution plans, OKRs | free | Living |
| `docs/research/` | Analysis, assessments, personas | free | Frozen when concluded |
| `docs/conventions/` | Standards like this one | free | Living |
| `docs/templates/` | Skeletons for new PRDs / specs / ADRs | `<kind>-template.md` | Living |
| `docs/working/` | Scratch: drafts, session notes | free | Disposable — promote or archive |
| `docs/_archive/` | Retired documents | frozen | Frozen |

Nothing sits loose at the docs root except `README.md` and a glossary.
`docs/README.md` is each repo's map: it lists which folders that repo uses
and links its entry-point documents. The names `requirements/` and
`tech-specs/` are **retired** — they must not reappear in any repo.

## The three-document rule

- **PRD** — what and why, per product surface. Update as scope evolves.
- **Tech Spec** — the *current truth* of a subsystem's design: contracts,
  schemas, flows. **A PR that changes a subsystem's behavior updates its
  spec in the same PR.**
- **ADR** — the record of *one* hard-to-reverse decision: options weighed,
  why one won. Immutable once accepted — supersede with a new ADR and link
  both directions, never edit. One shared `NNN` series per repo (three-digit,
  next free number; keep the `adrs/README.md` index current). Amendments
  that refine without reversing may suffix (`adr-NNN-a1-…`), but prefer a
  new number.

A feature is documented when the owning PRD covers its requirement, the
owning spec covers its design, and any hard-to-reverse decision has an ADR.
Most features amend existing documents — check before creating a new file.
A spec holding decision history becomes unreadable; an ADR holding spec
content goes stale.

## Naming

- Lowercase-kebab throughout. Never `SHOUTY_CASE`, `CamelCase`, or
  underscores.
- Generic English nouns — never brand names, codenames, or retired
  terminology.
- The filename must reflect the document's actual content and match its H1
  title in substance. Every PRD/spec/ADR has an H1.
- Start new PRDs / specs / ADRs from `docs/templates/`.

## Enforcement

- `axi hygiene stat docs [--repo PATH] [--json]` — the deterministic audit:
  loose root files, kind-prefix violations, casing, ADR number collisions,
  retired folder names, missing H1s, broken relative links.
- TIDY runs it in the heartbeat repo-hygiene sweep and surfaces findings as
  INFO/WARNING through `node_health`; fixes route through the normal
  propose → approve flow. Report-only: the audit never moves files itself.
- In review: a PR that adds a doc in the wrong place, or changes behavior
  without touching the owning spec, is not done.
