---
name: data.partner_health
description: Every reporting site's health from its nodes' heartbeats, and an alert to a person when data stops, a node is out of date, or an archive disk is above 80%.
---

# data.partner_health

Reads the heartbeat store on the platform node. For each site in scope it reports
when each node was last heard, how fresh its newest reading is, each lane's
state, its version against the oldest the intake accepts (`min_version`), its
last update, and its archive disk.

With `alert_to` (a principal), each condition that needs a person goes to that
principal's inbox, once per condition per day:

- data stopped: no new reading within `stopped_after_h` hours (default 24);
- out of date: a node older than `min_version`;
- disk high: an archive disk above 80%.

Run it on a schedule, for example every 15 minutes.
