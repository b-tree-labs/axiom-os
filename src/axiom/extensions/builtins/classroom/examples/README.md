# Keplo Classroom Extension — Example Domains

The Keplo classroom extension is **domain-agnostic by design**. This
directory holds canonical example domains showing how to use Keplo for
a range of subject areas. New domains slot in by adding a sibling
directory; the extension's CLI, RAG, eval, and CHALKE surfaces work
the same regardless of subject.

## Currently included

| Domain | Status | Path |
|---|---|---|
| Nuclear Engineering — NE-101 introductory course | **Reference example** (most complete) | `nuclear-engineering-ne-101/` |
| French Literature — 19th-century survey course | Stub example (demonstrates domain-agnosticism) | `french-literature-19th-century/` |
| K-12 Introductory Chemistry | Planned | — |
| Corporate Compliance Training | Planned | — |
| Coast Guard / Maritime Operations | Planned | — |

## What an "example domain" contains

Each example is a self-contained set of fixtures + an example Course
manifest illustrating Keplo configuration for that subject:

```
examples/<domain-name>/
├── README.md              # what this example demonstrates + how to load
├── course-manifest.toml   # canonical Course definition (axiom://course/...)
├── corpus/                # synthetic or licensed-for-example course material
│   └── *.md
├── prompts/               # tutor / quiz / reflect / review system prompts
│   └── *.md
├── assessments/           # checkpoint quizzes, rubrics
│   └── *.toml
└── README.md              # how to instantiate this Course as a Class
```

## How to load an example as a Class

```bash
# Initialize a Class from one of these examples
axi classroom prep init --from-example examples/french-literature-19th-century

# Now the Class is set up with the example's Course + corpus
axi classroom prep status <classroom-id>
```

(The `--from-example` flag is **not yet implemented**; tracked as a
follow-up. Today, instructors can manually copy fixture content
through `axi classroom prep corpus --upload` for each file.)

## Why we do this

Keplo's strategic value is being **adoptable elsewhere** — French Lit
at any university, mech-eng at any institution, K-12 chem in primary
school. UT Nuclear Engineering is the *first customer*, not the
target audience. Per `feedback_chalke_never_ut_specific`:

> When in doubt: ask "would this confuse a French-Lit instructor at
> Charles University?" If yes, generalize.

These example domains are the proving ground for that
generalization. If a Keplo capability works for NE-101 *and* French
Literature *and* K-12 Chemistry, it's domain-agnostic. If it only
works for one, we have a leak in the abstraction.

## Federation context — Course vs Class

A **Course** (canonical template) is a federation-citable artifact.
The example domains here are *Courses* — content-addressable, signed,
forkable. A **Class** (scheduled instance) is the running cohort that
instantiates a Course at a specific institution and term. See
[ADR-056: Course/Class Federation](../../../../../../docs/adrs/adr-053-course-class-federation.md)
for the architectural separation.

## Adding a new example domain

1. Create `examples/<domain-name>/` with the structure above
2. Author or curate ~10-30 KB of representative course material in
   `corpus/` (synthetic if licensing constrains real material)
3. Author a sample Course manifest in `course-manifest.toml`
4. Author tutor / quiz / reflect / review prompts that are
   *domain-appropriate but not subject-locked*
5. Add to the table in this README
6. Add ledger entry to the lead paper's Hypothesis Ledger if the
   example produces meaningful new evidence about Keplo's
   domain-generality
