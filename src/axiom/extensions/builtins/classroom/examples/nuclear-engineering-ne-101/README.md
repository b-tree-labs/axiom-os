# Example — Nuclear Engineering NE-101 (introductory undergraduate)

**Status:** Reference example, most complete.
**Used in:** Day 1 RAG harness, lead paper empirical evaluation.
**Cohort using this:** Prague summer 2026 (UT NE department, Ondrej
Chvala instructor of record).

## What this example covers

A fictional "Prague Lab Reactor" introductory course covering:
- Reactor overview and operating parameters
- Course syllabus, grading, late-policy, office hours
- Lab safety policies (three-strike framework)
- Week-3 problem set (one-group diffusion, slab boundary conditions)
- Week-3 lecture (delayed neutrons in TRIGA designs)
- Week-4 lecture (point kinetics, prompt-jump approximation)
- 10 CFR §50.46 ECCS performance criteria (regulatory reading)
- TRIGA pulse experiment lab manual
- Week-5 lecture with multi-author guest section (reactivity feedback,
  stability analysis)

## What's synthetic vs real

- **Prague Lab Reactor (PRL)** — fictional teaching abstraction. Real
  research-reactor specifications are governed by institutional
  licenses and aren't in this public corpus.
- **10 CFR §50.46** — actual NRC regulation excerpt. Cross-references
  (§50.34, Appendix K, RG 1.157, RG 1.183) are real.
- **Lamarsh, Duderstadt & Hamilton, Hetrick** citations — real
  textbooks; chapter numbers may be approximate.
- **Khiloni Shah, James Terry** — real UT-affiliated names with
  permission to appear in synthetic teaching content.

## Corpus location

The synthetic course material currently lives at:
`axiom/docs/working/visual-journeys/day1-rag-harness/fixtures-synthetic/`

This is the working corpus the Day 1 RAG harness uses. When the
`axi classroom prep init --from-example` flag lands, it will read
from this directory by default.

| File | Bytes | Probes (questions targeting it) |
|---|---|---|
| 01-prl-overview.md | 640 | rw-01 (max op temp 425°C) |
| 02-syllabus.md | 859 | rw-03 (Lamarsh Ch.7), rw-06 (10% per day late) |
| 03-policies.md | 1,112 | rw-04 (three-strike) |
| 04-homework.md | 759 | rw-05 (boundary condition) |
| 05-week3-lecture.md | 1,499 | rw-02 (Ondrej delayed neutrons) |
| 06-week04-lecture-reactor-kinetics.md | 4,237 | rw-10 (~30s asymptotic period) |
| 07-regulatory-10cfr50-46-eccs-excerpt.md | 4,241 | rw-08 (2200°F max), rw-11 (RG 1.157) |
| 08-lab-manual-triga-pulse.md | 4,304 | rw-07 (~600 MW peak), rw-12 (TC-FE-22 in C-5), rw-14 (Strike 3 actions) |
| 09-week05-multi-author-reactivity-feedback.md | 5,005 | rw-09 (Khiloni Shah guest), rw-13 (Hetrick recommended) |

## How this example is used in evaluation

This corpus + the 26-question battery (`questions.yaml` in the
harness directory) constitute the **synthetic v2 evaluation set**
used in the lead paper's §4-§5 results.

Real cohort runs (post-Prague onboarding) will replace this corpus
with Ondrej's actual lecture material; the structural shape stays
the same.

## Limitations

This example is small (9 docs, ~22 KB total). Real semester corpora
are 100-300 documents, 200-500 KB. Conclusions drawn from this
example generalize ONLY as far as the controlled-experiment
methodology — see lead paper §7 (Threats to Validity).
