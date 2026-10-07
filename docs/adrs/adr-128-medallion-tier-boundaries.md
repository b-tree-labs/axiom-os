# ADR-128 — Medallion tier boundaries, their allowances, and how they are enforced

- **Status:** Proposed — 2026-09-24
- **Context owner:** data platform
- **Supersedes nothing.** Makes explicit a rule that was assumed, violated
  periodically, and never checkable.

## Context

The rule everybody states is: **bronze lands raw, silver conforms,
decomposes, composes and stages, gold serves.** Nothing enforced it, and it
had drifted in both directions.

What is actually on the node today, verified rather than assumed:

| tier | base tables |
|---|---|
| bronze | `dt_source_files`, `ingested_files`, `reactor_ops_pages` |
| silver | `signals`, `reactor_status`, `rod_anomalies`, `reactor_operator_roster`, `reactor_operator_roster_candidates`, `reactor_operator_shifts` |
| gold | 11 base tables (`core_config`, `rod_calibration`, `rod_worth_catalog`, `shadow_crh`, `sample_tracking`, `investigation_findings`, …) plus 10 views |

Two drifts, of opposite kinds:

**Serving read the working tier.** The ingest summary — what a partner
calls to ask "did my data land?" — and the freshness report both queried
`silver.signals` directly. The freshness one also re-derived, in Python, an
answer `gold.ingest_freshness` already gave, and gave better: that view
carries `typical_gap`, each stream's own observed cadence, so staleness
needs no declaration. The skill refused to grade anything undeclared, which
is how SENNA sat four months stale under a report working as designed.

**The same kind of data lives in different tiers.** An operator roster is
authored reference data and sits in silver. A rod calibration is authored
reference data and sits in gold. Neither ever passed through bronze,
because neither has a raw form. Nothing said which was right, so both are.

The question this answers is not "is the rule good" — it is "what are the
exceptions, when do they apply, and how do we stop violating it by
accident". They are called **allowances** below, because "exception" is
already three other things in a Python codebase.

## Decision

### The rule

| tier | holds | written by | read by |
|---|---|---|---|
| **bronze** | bytes as received, shape not yet trusted | ingest | the conform pass |
| **silver** | conformed, validated, decomposed and recomposed records — the joinable ones | the conform pass, and the writers below | gold, and the tools that maintain it |
| **gold** | what is served | derivations of silver, and the writers below | everything that serves |

### Transformation lives in silver

Silver is not a holding pen between bronze and gold. It is the
transformation tier, and that single sentence is the most useful form of
this whole rule: **bronze receives, silver transforms, gold publishes.**

Twelve things happen in silver. The list is exhaustive on purpose — "the
working tier" is not a definition anyone can check, and this is:

1. **Conform** — one shape, one unit vocabulary, one timestamp
   convention, whatever the producer sent.
2. **Clean** — sentinels, NaNs, out-of-range values, stray whitespace,
   naive timestamps. Repaired where repair is defined, refused where it
   is not.
3. **Cast** — strings to numbers, numbers to the declared type,
   timestamps to timezone-aware.
4. **Decompose** — a landed artifact is broken into the atomic records it
   actually contains. One acquisition file is not one row; it is a channel
   set across instants, and silver is where it becomes one row per
   observation.
5. **Hydrate** — fill in what the producer referenced but did not carry.
   A channel name becomes a role and a unit through the site's channel
   map; a connector becomes a site; a code becomes a label.
6. **Resolve identity** — a former name becomes the canonical one. This
   is where `axiom.infra.site_identity` does its work, and skipping it is
   how one site became two.
7. **Deduplicate** — one canonical row per natural key, by idempotent
   upsert, so a replayed batch converges instead of accumulating.
8. **Compose** — atomic records are assembled into the joinable entities
   gold derives from: an observation carries its site, stream and channel
   identity, a run carries its segments.
9. **Validate** — refuse what cannot be admitted, and record why, where
   the producer can see it.
10. **Stamp provenance** — `source_class`, `schema_ref`, `row_hash`,
    quality, arrival time. Measured and modelled are told apart here or
    nowhere.
11. **Reconcile** — late arrivals and corrections, so a value that
    arrives after the fact lands as a correction rather than a second
    truth.
12. **Stage** — the result sits there, addressable, until gold derives
    from it.

Two corollaries, and both are checkable:

**Bronze does not transform.** Bytes land as received. A change made on
the way into bronze is a change nobody can audit against the original,
because there is no longer an original.

**Gold does not transform either — it derives.** A gold object may
select, filter, join, rename, bucket and aggregate: operations that
change the *shape* of an answer without changing what a record means. It
may not clean, hydrate, resolve an alias, fix a unit or repair a
timestamp. Those change what a record means, they are silver's twelve, and
doing one of them in a gold view guarantees the next view that needs it
will do it differently. That is not hypothetical: freshness was computed
two ways in this repo, and the version nobody was using was the better of
the two.

Decomposition and composition are also why a record that must be joined
before anyone relies on it belongs in silver even when it arrived fully
formed. The joining happens there.

**A serving path reads gold.** Never silver, never bronze. "Serving" means
anything whose output reaches a person, an agent, an API, a figure or a
report. A count for a status line is serving. A chart's provenance string
is serving.

### The tier is chosen by what must HAPPEN to the data, not by where it came from

This is the part that was missing, and it resolves the roster-versus-
calibration inconsistency without special-casing either.

- Bytes whose shape is not yet trusted → **bronze**.
- Records that must be conformed, validated, versioned, decomposed into
  their atomic parts, or composed with other records before anyone should
  rely on them → **silver**.
- Records that are ready to be relied on as authored → **gold**.

Provenance does not decide the tier. A hand-authored calibration curve that
needs no conforming belongs in gold even though it never saw bronze;
forcing it through two tiers invents a raw landing that does not exist and
a conform step that does nothing.

### The allowances, exhaustively

Each is a category, not a table, so a new table joins an existing category
or it is not an allowance.

**E1 — Authored reference data may be written directly to the tier it is
ready for.** A rod calibration, a core configuration, an operator roster.
It has no raw form, so it has no bronze. If it is ready to serve, gold; if
it needs conforming or versioning first, silver.
*Why:* the alternative is a bronze row that is a copy of the authored
record and a conform pass that copies it again.

**E2 — Curator annotations are written directly to silver.** Run labels and
segment boundaries, authored by a person about data already in silver.
*Why:* they are an input to silver's composition step. They are joined with
silver records and have to be conformed to the same keys, so they are
silver-shaped by construction. They are not served directly; gold serves
the composed result.

**E3 — Analysis outputs are written to the tier their consumers need.** A
shadow-correction parameter set, an investigation finding.
*Why:* an analysis output is authored data with a computation behind it.
The same test applies: ready to serve → gold, needs further conforming →
silver.

**E4 — The tools that MAINTAIN a tier read and write it.** The conform
pass, the schema manager that creates silver's columns and the gold views
over them, the tier-rename migration, the backup path.
*Why:* they are the tier's implementation, not its consumers.

**E5 — A diagnostic that is explicitly not serving may read any tier**,
provided it says which tier it read. `axi data conform-try`, an extension
lifecycle count, a debugging query.
*Why:* refusing to let an operator look at bronze while debugging an
ingest is the rule defeating its own purpose. The condition is that its
output is not presented as an answer about the data.

### What is NOT an allowance

- Convenience. "Silver already has it and gold is only a view over silver"
  is the argument that produced both drifts above.
- Performance. If a gold view is too slow, fix the view or add an index —
  reading around it hides the problem and keeps it.
- A missing gold object. If serving needs something gold does not expose,
  the fix is to expose it — a view added to `GOLD_SIGNALS_DDL` in
  `conformance/`, which `axi data ensure-schema` then applies.

## Enforcement

Three layers, because a rule nobody can check is a preference.

The rule lives in `data_platform/tiers.py` as data, so the guards check the
same list this ADR describes rather than a copy of it.

1. **Static: serving must not read the working tiers.** A test refuses
   `FROM silver.x` / `JOIN bronze.y` across the whole of `src/axiom`, not
   the data platform alone — serving happens in every extension, and a
   chat view, a receipts page and an ingest summary are all serving.
   Bronze is included although nothing reads it today: a rule added after
   the first violation arrives has to argue with it. E4's maintainers are
   exempt by name, the exemption list is checked for staleness, and an E5
   diagnostic passes by declaring itself and naming the tier it read —
   `conform_funnel` does exactly that, because "did this schema_ref
   conform?" is a question only the working tier answers.

   Widening the walk from one extension to the repo found one read
   immediately, in `cli/ext/lifecycle.py`. It is a legitimate E5 and now
   says so; the point is that scanning one extension would never have seen
   it. A negative control proves the guard fails on a violation introduced
   anywhere under `src/axiom`.

2. **Static: writes declare their allowance.** A write to a tier from
   outside the conform pass carries an `ADR-128 E<n>` marker naming the
   allowance it claims, so a reviewer reads the claim rather than
   inferring it. The marker is checked against the allowances that exist,
   because `E7` is worse than no marker: it looks like a claim somebody
   checked. Refusing the write would be wrong — E1 through E3 are
   legitimate, and the run promoter is one of them.

3. **Runtime: `axi data tier-audit` reads the database.** A static test
   holds the *code* to the rule and cannot see the database, which is
   where both drifts showed up. The audit reports each tier's shape,
   three things the rule did not expect — an undeclared base table in gold,
   a gold view serving bronze directly, and a gold view that TRANSFORMS
   rather than derives — and one preventive list: the
   working-tier tables no gold object reads, which is exactly the set a
   serving path would have to break the rule to reach.

   Authored reference tables are declared in
   `AXIOM_GOLD_REFERENCE_TABLES`; the platform's default is empty, because
   axiom writes no gold base table and should not pretend to know what a
   consumer put there. The audit changes nothing and fails nothing — a
   table someone added to gold by hand is a decision they made, and the
   useful part is that it stops being invisible.

## Consequences

- Serving is slower to extend: a new report needs a gold object rather
  than a query. That is the cost, and it buys a serving surface that can be
  changed without auditing every caller.
- The allowances are real and will be used. E1 in particular covers a large
  share of a reactor's data, which is configuration and calibration rather
  than telemetry.
- **Open, and needing an owner's call rather than a rule.** This ADR moves
  no data. Moving data is a migration; deciding where it belongs is a
  conversation with whoever relies on it. What the audit reports on the node
  today, so the conversation starts from measurements:

  - **Eleven undeclared base tables in gold** — `core_config`,
    `investigation_findings`, `investigation_series`, `log_entries`,
    `pipeline_status`, `rod_calibration`, `rod_worth_catalog`,
    `rto_experiment_worth`, `sample_tracking`, `shadow_correction_params`,
    `shadow_crh`. Most look like E1 or E3 and want declaring; a couple
    (`log_entries`, `pipeline_status`) look more like operational records
    that ended up on the serving tier.
  - **Four silver tables no gold object reads** — `rod_anomalies`,
    `reactor_operator_roster`, `reactor_operator_roster_candidates`,
    `reactor_operator_shifts`. Each is either genuinely internal or a
    serving need with no gold surface, and the second case is how a query
    ends up reaching into silver.
  - **No gold view reads bronze**, which is the one shape that would mean
    unvalidated rows were being published.
  - **Three gold views each convert watts to megawatts independently** —
    `reactor_daily`, `reactor_power` and `reactor_status_clean` all divide
    `nm1000_power_w` by a million and name the result megawatts. Nothing
    compares them. This is the corollary above caught in the act: a unit
    conversion is silver's first transform, and done in three views it is
    three chances to disagree. `reactor_status_clean` also says in its own
    name what it is doing. Found by a peer session reading the deployed
    view, which is how the check came to exist.
- `gold.signals` is `SELECT ... FROM silver.signals`, a pass-through. That
  is a legitimate published contract — it insulates serving from silver's
  shape — but it is worth knowing that "serve from gold" is cheap to
  satisfy for that table, and was being satisfied by nobody.
