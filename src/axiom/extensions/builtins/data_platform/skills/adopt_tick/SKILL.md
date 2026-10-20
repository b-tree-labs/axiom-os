---
name: data.adopt_tick
description: One bounded batch of an unattended time-series adoption copy; the orchestrator fires it on the job's interval, it resumes from the ledger, and it pauses itself when every window is copied and verified.
---

# data.adopt_tick

Installed by `axi data adopt-timeseries <schema.table> --schedule`. Each fire:

1. waits, bounded, for the database to accept connections (and fails loudly when it does not);
2. copies verified windows until its time budget, reconnecting if the connection drops;
3. returns. The next fire continues from the ledger, so a kill, a reboot or a restart of the
   orchestrator loses nothing.

When every window is copied and verified the job marks itself done and its cadence pauses.
The swap, the verification of the old table and the drop of it are explicit steps and are never run here.

The database address comes from `AXIOM_ADOPT_DSN` (or `AXIOM_DB_DIRECT_URL`): set it to the
database's own address, not a load balancer that closes idle connections.
