---
name: data.archive_reclaim
description: Free the local archive's space by retention before its disk fills, oldest first, never a batch not yet delivered upstream.
---

# data.archive_reclaim

Acts only under pressure: the disk holding the archive is below its alarm
floor, or the archive's own bytes (bronze plus silver) exceed
`AXIOM_ARCHIVE_BUDGET_BYTES`. Then it rotates out the oldest data older than
the declared minimum `AXIOM_ARCHIVE_KEEP_DAYS`, until the pressure clears:
first bronze batches already delivered upstream (on a contributor node the
forwarder's cursor is the boundary; declared edge downstreams bound it too),
then whole `silver.signals` chunks on a time-partitioned archive.

With no minimum declared it deletes nothing and the alarm stays up. The last
pass is written to `<state>/archive/reclaim.json`, which node status shows.
Fails (non-zero) while the alarm is still up after everything the policy allows.
