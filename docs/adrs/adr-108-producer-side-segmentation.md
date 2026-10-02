# ADR-108 — Producer-side segmentation: the producer classifies, it does not discard

**Status:** Draft — 2026-08-31
**Owner:** @ben
**Amends:** [ADR-106](adr-106-push-ingest-gateway.md) — extends §5 (streaming is a
producer-side concern) and the producer contract; adds a stage to Execution P2.
**Related:** ADR-08 (DAQ subsystem — reader, consolidator, transmitter, journal,
retention and overflow policy; this amendment places the new stage inside it),
ADR-105 (`access_tier`), ADR-049 (data-platform orchestration boundary).
Supersedes nothing.

## Context

ADR-106 assumed a producer forwards what its source emits, at a cadence it
chooses. That holds while the source rate and the useful rate are the same
number. For a high-rate instrument feed with more than one consumer they are not.

Two facts arrived after ADR-106 was written.

**The consumers want different slices of the same stream.** One consumer wants the
full rate and has no use for the steady stretches. Another wants exactly those
steady stretches, expressed as state variables over time rather than as samples.
One stream, two shapes.

**Only a small part of the day is transient.** On the order of 10–100 real
transients per day, each under five minutes. That is 3.5–35% of the wall clock.

The volumes follow directly:

| | rate | per day |
|---|---|---|
| Full stream | 10 Hz | ~2 GB |
| Transient windows at full fidelity | 10 Hz, 3.5–35% of the day | **69–694 MB** |
| Quasi-steady as state intervals | one row per state change | **order of KB** |

**Bandwidth is not the reason to act.** 2 GB/day is 23 KB/s — 0.19 Mbit/s. It
would cross any link the site has without being noticed. Three other things are
the reason:

- **Durable storage is the binding constraint.** The medallion store may sit on a
  storage-limited node until it moves to its scale-out home. The saving that matters
  is on write, not on the wire.
- **An edge consumer cannot wait for a round trip.** The same model commonly runs
  in two places: on the back end to populate analysis data, and at the edge inside
  a control loop. The edge instance needs the classification locally whatever the
  ingest path does, so the detector exists at the edge regardless.
- **A detector that triggers on the edge of an event loses its onset.** Deciding
  "this is a transient" requires having already buffered the seconds before it
  began, which means the producer is holding the boring data at the moment it
  decides.

## Decision

### A1.1 The producer segments and labels; it does not filter

A conforming producer for a high-rate source runs a **segmenter** between reading
and transmitting. The segmenter emits `Segment` records carrying a class, a time
range and provenance. It does **not** decide what to delete.

Three properties make this an amendment rather than a rewrite:

- **Classify, then route.** A segment class selects a lane and a fidelity. It
  never selects a wastebasket. "Steady-state" means *not interesting to the
  event-focused consumer*, which is a statement about one consumer, not about
  the data.
- **Pad every segment.** A transient segment carries lead-in and lead-out beyond
  the detector's trigger points. Onset and recovery are the parts an
  event-focused model most needs, and the parts a threshold crossing most
  reliably clips.
- **Hysteresis, not a bare threshold.** Separate enter and exit conditions, with
  a minimum dwell, so a signal loitering at the boundary produces one segment
  rather than a burst of them.

### A1.2 Two lanes leave the site, at different fidelities

| Lane | Content | Written as |
|---|---|---|
| `transient` | Full source rate across the padded window | Rows, existing row lane |
| `quasi_steady` | State variables by time, one row per state change | Rows, existing row lane |

Both ride `POST /ingest/rows` unchanged. This amendment adds no endpoint, no
verb, and no second delivery contract — the lanes are a `metadata` distinction on
batches the gateway already accepts, so §4 idempotency and §6 bounds apply as
written.

The quasi-steady lane's shape is **not ours to invent**: it is an existing model-input
schema, owned outside the platform, that already defines those state variables
over time. The producer emits into that shape so the two stay consistent by
construction.

### A1.3 The raw stream is retained locally on a rolling window

The producer's journal keeps the **unsegmented** source at full rate under a
`RetentionPolicy`, independently of what was transmitted.

This is the load-bearing decision, and the reason A1.1 forbids discarding.

*Steady-state* is a model, not a fact. Its thresholds, the channels it watches
and its dwell times will be revised, and revised again once there is a trained
model to argue with. A producer that deletes at the edge makes every future
revision **retroactively impossible** — the evidence that would show the old
detector was wrong is the evidence it threw away. A rolling local window bounds
that exposure to the window length instead of to forever: for as long as it
holds, a corrected detector can be re-run at the edge and the segments re-emitted,
and idempotency (§4) makes the replay safe.

The window length is a site configuration and a storage negotiation. It is not
zero.

### A1.4 A segment carries what it takes to be re-judged

Every segment record carries, alongside its rows:

- `segmenter_id` and `segmenter_version`
- the **parameter set** the decision used (thresholds, dwell, padding, channels)
- `segment_class` and, where the detector produces one, a score
- the **source context** at the time — the configuration of the apparatus the
  samples came from, including which positions were occupied and which were
  known empty

The first three exist so a training set can be selected on *which detector
produced this*, exactly as an insertion fact is selected on its confidence class.
A model trained across a detector change, with no way to see the change, learns
the seam.

The fourth exists because a source commonly feeds **several** consumer models,
each configured for a different source context. Context is what routes a segment
to the model it belongs to.

### A1.5 The segmenter is a seam, not an algorithm

This ADR fixes the **contract** — stage position, record shape, retention
obligation — and deliberately fixes no detection method. The first implementation
is the inverse of an existing capability: a parser that already identifies
steady-state ranges for the state-focused consumer, run backwards to yield the
complement.

That inverse is **co-owned**, not ours alone. It is specified here so the
producer has something to call, and the method behind it can change without
touching this contract.

## Consequences

**Positive**

- Roughly a 3–20× reduction in durable writes, with the *interesting* part kept
  at **full fidelity** rather than uniformly decimated.
- The edge-side and back-end consumers share one segmenter rather than diverging.
- A detector revision is a re-run, not a data loss, for the length of the window.
- No new endpoint, no second delivery contract, no change to the gateway.

**Negative**

- The producer gains state. It buffers, holds hysteresis, and keeps a rolling
  journal, so it is no longer a thin forwarder and its own failure modes matter.
- Local disk becomes a site requirement with a real number attached.
- A segmenter bug is now upstream of what gets written. The rolling window is the
  mitigation, and it is bounded.
- The quasi-steady shape couples us to a model input schema we do not own.

**Neutral**

- P2 grows. The producer SDK was spool, resume, backoff, timeouts and metrics; it
  is now those plus a segmentation stage with its own tests and metrics
  (segments emitted per class, dropped-to-window events, detector version in use).

## Alternatives considered

**Detect at the ingest face, discard server-side.** Easier to change centrally,
and it keeps the producer thin. Rejected: it spends the storage the exercise
exists to save, and it leaves an edge-side consumer without a local detector it
needs anyway.

**Detect at the producer and discard at the producer.** The smallest possible
footprint. Rejected on A1.3 — it is unrecoverable, and the detector is the part
of this system most likely to be wrong on the first attempt.

**Forward everything, decide later.** Honest and simple, and the bandwidth
genuinely permits it. Rejected on storage while the store is on a constrained
node. Worth revisiting **once the medallion store reaches its scale-out home**,
at which point A1.3's window could reasonably become the whole retention period and the
segmenter becomes a labeller only.

**Decimate uniformly to a lower rate.** One knob, no detector. Rejected: it
degrades exactly the transients the event-focused consumer exists to capture,
which is the
one part of the day that must stay at full rate.

## Execution

Amends ADR-106 P2, which becomes:

- **P2a** — producer SDK core: spool, resume, backoff, timeouts, spool-depth
  metrics. *Unchanged; ships the reusable client.*
- **P2b** — segmenter seam: `Segment` record, class routing, padding and
  hysteresis, rolling journal retention, provenance stamping. Ships with a
  pass-through segmenter (everything is one `transient` segment) so the seam is
  exercised before any detection method exists.
- **P2c** — first real detector, co-owned: the inverse of the steady-state
  parser, emitting quasi-steady state intervals in the external model-input schema.

P2a and P2b are additive and carry no migration. P2c depends on a collaborator
and should not block P2b.

## Open questions

1. **Window length.** What rolling retention does the site's disk actually
   support at 10 Hz, and does that survive a multi-day gateway outage?
2. **Padding.** How many seconds of lead-in does the event-focused consumer need
   before onset? This is a modelling answer, not an engineering one.
3. **Where the segmenter runs relative to the edge instance.** One process
   serving both, or two with a shared configuration and a drift check?
4. **Whether `segment_class` belongs in the natural key.** It is derived, and
   derived values in a key are how re-classification becomes a duplicate.
