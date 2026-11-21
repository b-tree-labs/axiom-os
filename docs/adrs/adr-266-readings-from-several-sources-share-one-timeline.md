# ADR-266: Readings from several sources share one timeline, kept by a coordinator in the DAQ core

**Status:** Accepted (2026-11-20; drafted 2026-11-17; revised 2026-11-19: a declared filter may produce a derived channel)
**Related:** ADR-132 (quality vocabulary), ADR-164 (node functions), ADR-180 (three lanes), ADR-182 (never down from our own causes), ADR-270 (a site declares its machines, zones and conduits)

## Context

A model that runs beside a plant needs one state at each step, built from sources that were never
designed to agree: an instrument chassis sampling at a fixed rate, a console that redraws a value
only when it changes, an archive that records on change, a camera. Each stamps time differently,
and today nothing records what a stamp means.

Measured on a bench with two real source formats:

- One source's stamp was the receiving machine's clock when a line arrived. The source itself sent
  no time.
- Another's was the host clock when a block was fetched, so every sample was labelled about one
  block late, on a different clock from the first.
- Readings that passed through files reached a consumer about ten seconds late. The same readings
  on the in-memory lane took a few milliseconds.
- A slow channel read beside a fast one has a value at the fast rate only if something invented
  it, and an invented value is indistinguishable downstream from a measurement.

This is the same problem at every site with more than one source, whatever the domain. The pieces
that exist are split: the DAQ core here has the journal, health and a latest-value snapshot; the
realtime lane and the declared clock corrections sit in a domain layer.

## Decision

The DAQ core gains a **timeline**: a time model, clock estimation, the realtime lane, and a
coordinator that builds frames.

1. **Every reading says what its time means.** It carries the source's own time when the source has
   one, with the clock it came from and that clock's kind (hardware-disciplined, network time, host,
   inferred); the time the receiving machine took the reading; and that machine's estimated offset
   with its uncertainty, in the platform's uncertainty envelope.
2. **A site declares its clock.** The reference source and its kind are site configuration, so a
   site with a disciplined hardware clock and a site with network time only use the same code and
   report different uncertainties.
3. **Offsets are measured, by whichever side may open the connection.** The exchange respects the
   site's conduits (ADR-270). Declared corrections for runs stamped at the wrong instant move here
   from the domain layer.
4. **The realtime lane is part of the core.** Every collector publishes each reading as it is read,
   before it is journaled. A publisher never waits on a consumer: a send that would block is a
   dropped consumer. The lane does not depend on how the collector was started.
5. **A coordinator builds the frame for step time *t*** from each channel's latest value at or
   before *t*, with its age. Ages come from a monotonic clock and sequence numbers, never from
   wall time, so a clock step cannot make stale data look fresh. Each channel declares its age
   limit and whether a value may be held; a channel past its limit makes the frame stale for
   whatever needs it. Nothing is interpolated or padded on the realtime path. A declared filter
   may produce a second channel from a measured one, for example a fast detector downsampled to
   a control loop's rate: that channel says it is derived, names its filter and its source, and
   carries the filter's delay in its age. It is never presented as a measurement. The frame builder is
   a pure function of the readings and the declaration, so replaying the journal after a restart
   or a gap produces the same frames as the live run.
6. **Lag between sources is measured, not assumed.** A site may declare pairs of channels that
   follow the same physical quantity. The coordinator cross-correlates them on every change,
   records the lag with its uncertainty, and raises an alarm when it drifts.
7. **Offline alignment uses the same model.** Conform aligns on corrected times and applies each
   channel's declared policy: continuous channels may be interpolated, step-held channels are only
   ever held.

The wire form of a reading and of a frame is versioned, so a compiled implementation can replace
the reference one where the rate demands it.

## Options considered

- **Keep each source's stamp as it is and join in the store.** No query can recover what a stamp
  meant, or tell a held value from a measured one.
- **Let each consumer align for itself.** Every model, display and control law would do it
  differently, and the one on the control path could not be shown to match the record.
- **Resample everything to a common grid on the way in.** It invents readings.

## Consequences

- The realtime lane, the overlapping-instance registry and the declared clock corrections move
  from the domain layer into the core; the domain layer keeps its decoders, instrument
  declarations and channel pairs.
- A model host, a control law and a display all read frames from one coordinator, so what was
  predicted, what was commanded and what was shown can be compared on one timeline.
- Staleness has one definition. The credited guard and the snapshot use the coordinator's ages.
- New work: the time fields on the envelope, the clock declaration, the offset exchange, the
  coordinator and its replay test, the lag calibration, and the conform policy per channel.
