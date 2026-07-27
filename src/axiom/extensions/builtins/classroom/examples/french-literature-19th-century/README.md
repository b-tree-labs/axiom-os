# Example — French Literature, 19th-Century Survey (humanities course)

**Status:** Stub — demonstrates Keplo's domain-agnosticism.
**Cohort using this:** None yet; this is a generalization-proof
example, not a deployed Class.

## Why this example exists

If Keplo only works for nuclear engineering, it's not a domain-agnostic
classroom platform; it's an NE tool. The litmus test from
`feedback_chalke_never_ut_specific`:

> Would this confuse a French-Lit instructor at Charles University?

This example exists to make sure Keplo's CLI, prompt scaffolds, RAG
behavior, eval rubrics, and CHALKE reasoning are **as ergonomic for
a French Lit instructor** as they are for Ondrej teaching NE-101.

## What this example would cover (planned)

A representative 19th-century French Literature undergraduate
survey course, with module structure typical of humanities pedagogy:

- Week 1: Realism — Balzac, *Le Père Goriot*
- Week 2: Realism continued — Flaubert, *Madame Bovary*
- Week 3: Naturalism — Zola, *L'Assommoir* and the Rougon-Macquart
- Week 4: Symbolism — Baudelaire, *Les Fleurs du mal*; Verlaine
- Week 5: Late-century novel — Maupassant short fiction
- Week 6: Comparative analysis — selected criticism (Auerbach, Lukács)

The corpus would include:
- Lecture notes for each week (text-based; humanities lectures are
  more discursive than STEM)
- Selected primary-text excerpts (where licensing permits) or
  paraphrases
- Critical-essay extracts and citation lists
- Rubrics for close-reading exercises and essay assessments

## What this example tests in Keplo

Compared to NE-101:

| Dimension | NE-101 (STEM) | French Lit (humanities) |
|---|---|---|
| Material density | Equations, tables, regulatory references | Discursive prose, textual citations |
| Citation style | Numbered references, technical precision (§50.46(b)) | MLA / Chicago, page-anchored quotations |
| Assessment shape | Calculation problems, mastery-checkpoint quizzes | Close-reading essays, comparative analysis |
| Student-tutor interaction | "Walk me through the derivation" | "Help me see what Flaubert is doing in this paragraph" |
| Refusal pattern | "Material does not state" | "The text doesn't directly address this; here's adjacent context" |
| Cross-references | Regulatory ID strings | Inter-textual allusions |

If Keplo's tutor/quiz/reflect modes work *natively* for both — without
prompt-engineering forks per domain — that's the generalization
proof.

## Status

- [ ] Corpus authoring (synthetic; ~15-20 KB representative material)
- [ ] Course manifest
- [ ] Domain-appropriate prompt scaffolds
- [ ] Sample assessment rubrics
- [ ] Run through Day 1 harness as the second-domain validation

## Why this matters for the paper

The lead paper's §7 *Threats to Validity* explicitly notes
single-domain limitation. A French Lit run produces second-domain
evidence that the chunker effect, the refusal-quality pattern, and
the pedagogical-intent battery design generalize beyond NE.

This stub is the placeholder for that evidence; the work to populate
it is queued.
