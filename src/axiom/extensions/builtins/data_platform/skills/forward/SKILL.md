---
name: data.forward
description: Deliver this node's landed batches upstream — the intake when healthy, else a Box drop folder, else wait. Advances only on a confirmed delivery.
---

# data.forward

Reads this node's ingest outbox in order and delivers each batch upstream.
The transport is chosen each pass, with hysteresis: the upstream intake when it
answers its health check and accepts this site's key; otherwise a Box drop
folder through rclone, using the operator's own Box login from the vault;
otherwise it waits. A batch is marked delivered only after the intake answered
200 or the dropped copy was read back and matched. Status, including every
switch and its reason, is written to `<state>/forward/status.json`.

A pass sends for at most a few seconds (`lane_slice_s`), writing the status
after every confirmed request, then returns to choosing the transport. So a
slow drop folder never keeps the forwarder from a recovered intake: it asks the
intake again between slices and switches back after the configured healthy
checks, with the rest of the backlog going to the intake oldest first. A drop
file packs many outbox batches in the intake's own request body, so the drop
lane keeps up with a live collector; a missing drop folder is named as such.
