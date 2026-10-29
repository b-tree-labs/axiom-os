# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""data_platform skills — invocable through the platform SkillRegistry.

Each skill is a plain Python function::

    def run(params: dict, ctx: SkillContext) -> SkillResult: ...

Registration into the default registry happens at first-use via
:func:`bind_default`. Tests build a clean ``SkillRegistry()`` directly
and call :func:`bind` against it; the CLI module's main() calls
:func:`bind_default` once before dispatching.

Naming: skills are namespaced under ``data`` (the extension's CLI
noun). So ``data.install``, ``data.diagnose``, ``data.ingest``, etc.

A skill maps to its CLI verb 1:1 — that's the principle ADR-056
locks in. ``axi data install`` → ``data.install``; same skill called
from any agent persona.
"""

from __future__ import annotations

from axiom.infra.skill_declarations import Precondition
from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import (
    activity,
    adopt_tick,
    adopt_timeseries,
    adopt_watch,
    archive_reclaim,
    backfill_urls,
    backup,
    backup_policy,
    backup_validate,
    box_login,
    conform_run,
    connector,
    conform_try,
    cutover_gate,
    diagnose,
    edge_bundle,
    edge_pull,
    enroll,
    ensure_schema,
    expectations,
    forward,
    gaps,
    ingest,
    ingest_freshness,
    ingest_push,
    install,
    orchestrator_install,
    partner_health,
    pipeline_proof,
    preflight,
    probe_nodes,
    reading_identity,
    rederive,
    refresh,
    register,
    retrieval,
    site_rename,
    storage,
    tier_audit,
    troubleshoot,
    unregister,
)
from . import friendly as _friendly
from . import gold as _gold
from . import kit as _kit
from . import kit_promotion as _kitp
from . import (
    list_connectors as _list_mod,
)
from . import (
    medallion as _medallion,
)

_NAMESPACE = "data"

_SKILLS = {
    "install": install.run,
    "diagnose": diagnose.run,
    "gaps": gaps.run,
    "ingest": ingest.run,
    "refresh": refresh.run,
    "enroll": enroll.run,
    "register": register.run,
    "unregister": unregister.run,
    "list": _list_mod.run,
    "troubleshoot": troubleshoot.run,
    "preflight": preflight.run,
    "conform_try": conform_try.run,
    "conform_run": conform_run.run,
    "expectations": expectations.run,
    "reading_identity": reading_identity.run,
    # The tenant data kit (docs/prds/prd-tenant-data-kit.md).
    "kit_init": _kit.kit_init,
    "kit_up": _kit.kit_up,
    "kit_try": _kit.kit_try,
    "kit_check": _kit.kit_check,
    "kit_down": _kit.kit_down,
    "kit_snapshot": _kit.kit_snapshot,
    "kit_materialize": _kit.kit_materialize,
    "kit_scaffold": _kit.kit_scaffold,
    "kit_sample": _kit.kit_sample,
    # ADR-203: promote a kit as data; serve what was promoted.
    "kit_promote": _kitp.kit_promote,
    "kit_pull": _kitp.kit_pull,
    "kit_submit": _kitp.kit_submit,
    "kit_review": _kitp.kit_review,
    "kit_host_setup": _kitp.kit_host_setup,
    "kit_rollback": _kitp.kit_rollback,
    "kit_withdraw": _kitp.kit_withdraw,
    "kit_approve": _kitp.kit_approve,
    "kit_retire": _kitp.kit_retire,
    "kit_verbs": _kitp.kit_verbs,
    "kit_charts": _kitp.kit_charts,
    "kit_automations": _kitp.kit_automations,
    "kit_verb": _kitp.kit_verb,
    "ensure_schema": ensure_schema.run,
    "rederive": rederive.run,
    "tier_audit": tier_audit.run,
    "pipeline_proof": pipeline_proof.run,
    "ingest_freshness": ingest_freshness.run,
    "edge_pull": edge_pull.run,
    "cutover_gate": cutover_gate.run,
    "forward": forward.run,
    "archive_reclaim": archive_reclaim.run,
    "adopt_timeseries": adopt_timeseries.run,
    "storage": storage.run,
    "orchestrator_install": orchestrator_install.run,
    "partner_health": partner_health.run,
    "probe_nodes": probe_nodes.run,
    "box_login": box_login.run,
    "edge_bundle": edge_bundle.run,
    # One introspection vocabulary for every tier — see medallion.py.
    "catalog": _medallion.catalog,
    "describe": _medallion.describe,
    "freshness": _medallion.freshness,
    "sample": _medallion.sample,
    "site_rename": site_rename.run,
    "activity": activity.run,
    "backfill_urls": backfill_urls.run,
    "backup": backup.run,
    "backup_policy": backup_policy.run,
    "backup_validate": backup_validate.run,
}


# Skills exposed via MCP / HTTP but NOT as a CLI verb. ``data.ingest_push``
# is the push front door (inline bytes from an egress agent / MCP client),
# the peer of the HTTP ``POST /ingest`` endpoint — it shares the IngestSink
# core. It is kept out of ``verbs()`` because a CLI verb taking inline
# content on the command line is not a usable surface; the pull-side
# ``data.ingest`` verb covers the CLI.
#: `axi data retrieval <verb>` — one skill per sub-verb, per ADR-056. The
#: manifest declared these and the CLI parsed them, but nothing registered them,
#: so every one raised KeyError at dispatch.
_RETRIEVAL_SKILLS = {
    "retrieval_save": retrieval.save,
    "retrieval_list": retrieval.list_saved,
    "retrieval_show": retrieval.show,
    "retrieval_rm": retrieval.remove,
    "retrieval_dialect": retrieval.dialect,
}

#: `axi data connector <verb>` — guided setup and live checks for a connector.
_CONNECTOR_SKILLS = {
    "connector_add": connector.add,
    "connector_list": connector.list_all,
    "connector_show": connector.show,
    "connector_test": connector.test,
    "connector_remove": connector.remove,
}

_NON_CLI_SKILLS = {
    "ingest_push": ingest_push.run,
    # Fired by the orchestrator for a job installed with `adopt-timeseries --schedule`.
    "adopt_tick": adopt_tick.run,
    # Fired by the orchestrator while an adoption job exists.
    "adopt_watch": adopt_watch.run,
    # Fired by the orchestrator on a promoted kit automation's cadence (ADR-203).
    "kit_automation_fire": _kitp.kit_automation_fire,
}

#: Generic medallion ANSWERING (ADR-115) — computing over a tier rather than
#: introspecting it. A separate family from catalog/describe/freshness/sample
#: on purpose: these are tabular-only and they calculate.
_GOLD_SKILLS = {
    "aggregate": _gold.aggregate,
    "series": _gold.series,
    "uncertainty_coverage": _gold.uncertainty_coverage,
    "roles": _gold.roles,
    "compare": _gold.compare,
    "serving_report": _gold.serving_report,
    # The everyday verbs: composed from the served ones above, so a bound reader
    # gets them from a node that has never heard of them.
    "channels": _friendly.channels,
    "latest": _friendly.latest,
    "show": _friendly.show,
}


#: Declarative metadata per verb (ADR-063). Registering with a spec is what
#: makes a capability projectable: one definition becomes the CLI verb, the
#: MCP tool, the agent-facing function and the generated SKILL.md
#: (capability_projection, ADR-072). A verb registered without one exists at
#: the terminal and nowhere else — which also means it cannot be filtered per
#: tenant, since a surface cannot scope what it cannot see.
#:
#: ``inputs`` is shape-only and documentation-facing; a trailing ``!`` marks a
#: required input.
_SPECS: dict[str, tuple[str, dict[str, str]]] = {
    "edge_pull": (
        "Pull what an ingest edge holds into this node's bronze. The edge is the "
        "public node producers push to; this node opens the connection, so "
        "nothing reaches in. Resumable: the cursor advances only past a batch "
        "that is durable here, and rows deduplicate by hash.",
        {
            "connector": "str — the `edge` connector to drain",
        },
    ),
    "cutover_gate": (
        "During a platform move, compare one UTC day of the old and new copies of "
        "silver, record it in the gate's ledger, and say whether it is safe to cut "
        "over, listing every unmet condition. Changes neither copy. Also records "
        "events (outage, upgrade) and the one-off history check.",
        {
            "old_ref": "str — vault reference to the old copy's database address",
            "new_ref": "str — vault reference to the new copy's database address",
            "sites": "str — comma-separated sites",
            "day": "str — UTC day YYYY-MM-DD (default yesterday)",
            "ledger": "str", "event": "str", "event_day": "str", "history": "str",
            "note": "str", "verdict_only": "bool", "min_clean_days": "int",
            "min_active_days": "int", "active_feeds": "str", "required_events": "str",
        },
    ),
    "forward": (
        "Deliver this node's landed batches upstream: to the upstream intake when it "
        "is healthy, to a Box drop folder when it is not, and wait otherwise. Advances "
        "only on a confirmed delivery; the upstream dedupes by content hash.",
        {
            "intake_url": "str", "key_ref": "str", "probe_source": "str",
            "box_remote": "str", "box_login": "str", "box_app": "str", "outbox_dir": "path",
            "source_map": "str", "share_config": "path", "up_after": "int", "down_after": "int",
            "watch_s": "float", "max_passes": "int", "key_reread_s": "float",
        },
    ),
    "archive_reclaim": (
        "Free the local archive's space by retention before its disk fills: under "
        "pressure (the disk alarm, or over its declared budget), rotate out the oldest "
        "data older than the declared minimum, never a batch not yet delivered upstream.",
        {
            "outbox_dir": "path", "keep_days": "float", "budget_bytes": "int",
            "contributor": "bool", "forward_cursor": "path", "disk_path": "path",
            "dsn": "str", "no_db": "bool", "watch_s": "float", "max_passes": "int",
        },
    ),
    "orchestrator_install": (
        "Run the data orchestrator (the scheduler's tick loop: backups, follow-ups, kit "
        "automations) as a per-user systemd service on this node. env_file names the file the "
        "node's services already read; no credential is written. Refuses beside a running one.",
        {"env_file": "path", "write_only": "bool", "force": "bool"},
    ),
    "storage": (
        "Storage health, measured (ADR-189): per table, bytes, compressed against "
        "uncompressed and the ratio, the oldest chunk and bytes per day; per volume, free "
        "space, bytes per day and the days of runway. Reads only.",
        {"top": "int", "growth_days": "int", "disk_path": "path", "dsn": "str"},
    ),
    "adopt_timeseries": (
        "Adopt a populated plain table into the time-series contract (ADR-189) in place, "
        "with the writer running: an estimate that writes nothing (default), a copy by time "
        "window verified per window and resumable (apply), a swap in one short transaction "
        "(swap), a verification of the old table against the new (verify), the drop of the "
        "old table only after that (drop_old), and a rollback at any point before it.",
        {
            "table": "str! — schema.table", "apply": "bool", "swap": "bool", "verify": "bool",
            "repair": "bool", "drop_old": "bool", "rollback": "bool", "status": "bool",
            "identity": "str — comma-separated columns that identify a series",
            "time_column": "str", "rows_per_day": "int", "chunk_interval": "str",
            "compress_after": "str", "retain": "str", "window": "str", "max_windows": "int",
            "tablespace": "str", "sample_rows": "int", "disk_path": "path", "dsn": "str",
            "lock_timeout": "str", "quiet": "bool",
            "schedule": "bool — run the copy unattended as a job the orchestrator fires",
            "unschedule": "bool — remove that job (the copy and its ledger are untouched)",
            "every": "str — the job's interval, e.g. 5m", "batch_seconds": "int",
            "ready_timeout": "int — seconds to wait for the database before a fire gives up",
            "ack_mismatch": "bool — acknowledge the raised mismatch for this table, after investigating",
        },
    ),
    "partner_health": (
        "Every reporting site's health from its nodes' heartbeats, and an alert to a "
        "person when data stops, a node is out of date, or an archive disk is above 80%.",
        {"alert_to": "str", "no_alert": "bool", "sites": "str", "min_version": "str",
         "stopped_after_h": "float"},
    ),
    "adopt_tick": (
        "One bounded batch of an unattended time-series adoption copy: waits for the database, "
        "copies verified windows until its budget, resumes from the ledger on the next fire, and "
        "pauses itself when every window is copied and verified. Never swaps or drops.",
        {"table": "str! — schema.table"},
    ),
    "adopt_watch": (
        "PLINTH's check on an unattended adoption copy: a stalled, down, mismatch or space fault, "
        "attributed to us; quiet when the copy is healthy or none is in progress.",
        {"stall_after": "str", "acknowledge_mismatch": "str — schema.table"},
    ),
    "probe_nodes": (
        "Check every node the platform can reach from outside (its /readyz) and keep "
        "the result for the uptime report.",
        {"sites": "str", "timeout_s": "float"},
    ),
    "box_login": (
        "Make one node's Box login through the owner's own OAuth app (client id and secret "
        "from the vault), by a browser sign-in as the Box account made for that node. Writes "
        "the login, in the one-line form rclone reads, to a new file only the caller can read; "
        "never prints it and never overwrites a file.",
        {"app": "str", "out": "path", "port": "int", "timeout_s": "float", "no_browser": "bool"},
    ),
    "edge_bundle": (
        "Write the ingest edge's deploy folder (Dockerfile, compose file, nginx blocks, "
        "operator script, README) from the installed package, pinned to this release, "
        "optionally as a reproducible tarball. Refuses a build that is not a published "
        "release unless told which, and never overwrites a folder it did not make.",
        {"out": "path", "tar": "bool", "version": "str", "force": "bool"},
    ),
    "kit_init": (
        "Write a data kit into a site repository: data/ with one working example "
        "of each contribution (a normalizer, silver declarations, a gold object, "
        "a verb chat can call, a chart) and sample data, so the whole loop runs "
        "on a fresh clone. Refuses a non-empty data/ unless force. Not the "
        "harness's own /init, which writes an assistant instructions file.",
        {"tenant": "str!", "dir": "path",
         "force": "bool"},
    ),
    "kit_up": (
        "Start the kit's local medallion (Postgres in Docker, or AXIOM_KIT_DSN) "
        "with the platform's silver and gold schema and a tenant role confined to "
        "the tenant's rows by row-level security.",
        {"dir": "path"},
    ),
    "kit_try": (
        "Run every tier of the kit on local data: conform the samples with the "
        "kit's normalizers, apply silver declarations, create the gold objects, "
        "run each verb and chart as the tenant role. Shows a table and chart per tier.",
        {"dir": "path"},
    ),
    "kit_check": (
        "The gate before promotion: declarations, normalizer lint, and an "
        "isolation proof (every answer unchanged when another tenant's data is "
        "added). Without a local medallion the proof is reported NOT RUN, never passed.",
        {"dir": "path", "static_only": "bool"},
    ),
    "kit_down": (
        "Drop the sandbox. A snapshot under data/snapshots/ stays.",
        {"dir": "path"},
    ),
    "kit_pull": (
        "Start from a frame that already exists: pull one site's gold series for a window "
        "(optionally some channels) into a kit snapshot. Each channel's feed comes from the "
        "catalogue unless `feed` names it, so the read uses the signal index; the window and "
        "point count are bounded. On a bound install it reads through the node's door, held to "
        "what your key may read.",
        {"dir": "path", "site": "str", "channels": "str", "feed": "str", "start": "str!", "end": "str!",
         "bucket": "str", "id": "str"},
    ),
    "kit_promote": (
        "Send this kit to the node it is bound to, as data (ADR-203): the node checks it, an "
        "operator approves it there, and its gold views, verbs and charts are then served. "
        "A bundle carrying conform code is refused until the isolated worker exists.",
        {"dir": "path"},
    ),
    "kit_submit": (
        "Receive a kit bundle on the node. The site comes from the caller's credential; a site "
        "credential submits for its own site only.",
        {"bundle": "str!"},
    ),
    "kit_review": (
        "List kit bundles on this node with their state and problems (operator): the pending "
        "ones (checked, waiting for kit-approve) unless `state` or `all` is given.",
        {"site": "str", "state": "str", "all": "bool — every bundle, not only the pending ones"},
    ),
    "kit_host_setup": (
        "Set this node up to take kit bundles, once, with the database's writer role "
        "(operator): the registry, the serving login's grants, and row-level security on "
        "silver once, so a submit runs no DDL and an approve takes no lock on the hot table.",
        {"serve_role": "str"},
    ),
    "kit_approve": (
        "Apply a checked kit bundle on this node in one transaction, with the structural "
        "isolation proof; nothing is applied if the proof fails (operator). A bundle that "
        "would replace another contributor's objects at its site needs `override`.",
        {"id": "int!", "override": "bool"},
    ),
    "kit_rollback": (
        "Re-apply an earlier applied or superseded kit bundle from the registry, with the same "
        "isolation proof as approve; the bundle it replaces becomes superseded (operator).",
        {"id": "int!", "site": "str!", "override": "bool"},
    ),
    "kit_retire": (
        "Remove a site's promoted kit, or one contributor's objects in it (`principal`). Without "
        "`apply` or `force` it only lists what it would drop, and any dependents outside the kit, "
        "and changes nothing. `apply` retires, and refuses when anything outside depends on it; "
        "`force` also drops those dependents (operator). The registry keeps the audit record.",
        {"site": "str!", "principal": "str", "apply": "bool", "force": "bool"},
    ),
    "kit_withdraw": (
        "Ask the node to remove your promoted objects at a site. Records a request an operator "
        "sees in kit-review and applies with kit-retire --principal; nothing is dropped.",
        {"site": "str", "reason": "str"},
    ),
    "kit_verbs": (
        "The promoted kit verbs you may run: your credential's site's only (an operator with no "
        "site on the credential sees every site's, or one with `site`). Each answers one named "
        "question over that site's gold. A bound install asks its node.",
        {"site": "str"},
    ),
    "kit_charts": (
        "The promoted chart specs you may see: your credential's site's only (an operator sees "
        "every site's, or one with `site`). Each is a ChartSpec document the chart surface can "
        "draw. A bound install asks its node.",
        {"site": "str"},
    ),
    "kit_automations": (
        "The promoted kit automations you may see (your credential's site's only; an operator "
        "sees every site's): each one's verb, schedule, condition and action, and its last run "
        "and outcome. A bound install asks its node.",
        {"site": "str"},
    ),
    "kit_automation_fire": (
        "Run one promoted kit automation once on this node: its verb as the site's role, its "
        "condition, then notify the submitter and/or annotate. Fired by the scheduler.",
        {"site": "str!", "automation": "str!"},
    ),
    "kit_verb": (
        "Run one promoted kit verb by name, with its declared parameters, as its site's role. "
        "A site's credential runs its own site's verbs only; a bound install asks its node.",
        {"name": "str!", "site": "str", "params": "json"},
    ),
    "kit_snapshot": (
        "Keep a point-in-time subset (site, channels, window) under data/snapshots/. "
        "Not a cache. kit-down does not remove it.",
        {"dir": "path", "site": "str", "channels": "str", "start": "str", "end": "str",
         "rows": "json", "id": "str"},
    ),
    "kit_materialize": (
        "Load one snapshot into the sandbox kit-up stamped. The snapshot file stays.",
        {"dir": "path", "id": "str!"},
    ),
    "kit_scaffold": (
        "Scaffold one more contribution in an existing data kit: a normalizer "
        "(conform; deposit=true for whole files), a derived silver channel, a "
        "gold object, a verb or a chart. Channels, units and the chart window "
        "come from the kit's own samples, so the scaffold draws on the next "
        "kit_try. With from_csv, a conform reader is written for CSV files shaped like "
        "that one (time column and numeric channels learned from it). kind=automation writes "
        "automations/<name>.toml: run `verb` every `every` (or on `cron`), and when `column` is "
        "`above`/`below` a value (or any_rows), notify the submitter and/or annotate the rows "
        "with the `annotate` label. Never overwrites a file.",
        {"kind": "str!", "name": "str!", "dir": "path", "deposit": "bool", "from_csv": "path",
         "verb": "str", "every": "str", "cron": "str", "above": "float", "below": "float",
         "any_rows": "bool", "column": "str", "notify": "bool", "annotate": "str"},
    ),
    "kit_sample": (
        "Bring the provider's own raw files into a kit's samples for one source "
        "declared in kit.toml: rows sources take bronze .jsonl records; deposit "
        "sources take any files, content-addressed with a mirror record each. "
        "Reports the channels and units the kit's normalizers read from them.",
        {"source": "str!", "origin": "path!", "dir": "path"},
    ),
    "aggregate": (
        "Deterministic aggregate (sum/mean/min/max/std/count) of a gold column, "
        "with an optional time window and filter. Returns a provenance-stamped "
        "value that says which population it summarised. REFUSES to blend rows "
        "spanning more than one unit or source_class into a single number — a "
        "mean over a measurement and a model's prediction is not a measurement "
        "of anything. Pass group_by to split the answer instead (group_by="
        "'source_class' is the measured-versus-predicted comparison), narrow it "
        "with filter, or set allow_mixed to state the blend is intended.",
        {"table": "str!", "column": "str!", "fn": "str!", "window": "dict",
         "filter": "str", "group_by": "list — split rather than blend",
         "allow_mixed": "bool — summarise across populations on purpose",
         "allow_mixed_units": "bool — the unit labels differ but the rows share a scale",
         "include_synthetic": "bool — include the install's own self-test rows"},
    ),
    "series": (
        "Bucketed series from a gold table — the input shape the analytics tool "
        "consumes. With group_by, one named line per population over the same "
        "buckets, which is what a measured-against-predicted overlay is. Without "
        "it, the same refusal as aggregate: one line blended across units or "
        "source classes is a line that means nothing.",
        {"table": "str!", "column": "str!", "bucket": "str!", "time_column": "str!",
         "fn": "str", "window": "dict", "filter": "str",
         "group_by": "list — one line per group",
         "allow_mixed": "bool — blend populations on purpose",
         "allow_mixed_units": "bool — the unit labels differ but the rows share a scale",
         "include_synthetic": "bool — include the install's own self-test rows"},
    ),
    "retrieval_save": (
        "Name a retrieval — what to fetch, said once, reusable on every surface.",
        {"name": "str!", "site": "str", "feed": "str",
         "channels": "list — a retrieval naming no channel names no data",
         "window": "str — a span (24h, 7d, all, operating) or from/to",
         "bucket": "str", "note": "str", "replace": "bool"},
    ),
    "retrieval_list": ("Every saved retrieval in the local catalogue.", {}),
    "retrieval_show": (
        "One saved retrieval, with whether its window is frozen or open.",
        {"name": "str!"},
    ),
    "retrieval_rm": ("Forget a saved retrieval.", {"name": "str!"}),
    "retrieval_dialect": (
        "A retrieval as the query parameters a named surface actually reads.",
        {"name": "str!", "dialect": "str! — chart or telemetry"},
    ),
    "uncertainty_coverage": (
        "How much of the served surface can say how well it is known: per "
        "(site, stream), how many points declare their uncertainty sources "
        "(exact), how many carry only a magnitude (bounded), and how many "
        "carry neither (unclaimable). Plus the inventory of declared sources "
        "and whether each is shared across rows or drawn per reading.",
        {"site": "str", "include_sources": "bool"},
    ),
    "channels": (
        "Each channel at a site with its feed, unit, role, and first and last reading, "
        "from the signal catalogue. The site defaults to the binding's site, or the only one.",
        {"site": "str", "role": "str"},
    ),
    "latest": (
        "The most recent value of each channel at a site, or of one role or one channel. "
        "Read at each channel's own last timestamp, so a stopped channel still answers.",
        {"site": "str", "role": "str", "channel": "str"},
    ),
    "show": (
        "A series for a role or a channel, asked the short way: the table, columns and "
        "feed are worked out, the site defaults to the binding's, the window defaults to "
        "the latest day that has readings (not now), and the bucket follows the window. "
        "An unknown name or an empty window answers with the nearest real name or the "
        "flag that reaches the last reading.",
        {"target": "str! — a role or a channel", "site": "str", "feed": "str",
         "start": "str", "end": "str", "day": "str — YYYY-MM-DD", "last": "str — 7d, 36h",
         "yesterday": "bool", "every": "str — 15m, 1h, 1d", "fn": "str"},
    ),
    "roles": (
        "Which quantities the fleet can answer, and which sites answer each. "
        "Every other gold verb is keyed on table and column, and nobody asks a "
        "question in those terms — a researcher asks about wall temperature or "
        "a position, and the only part of a signal that carries a quantity "
        "portably is its role. Says for each whether it is actually comparable, "
        "since a role answered in two units is two questions sharing a name.",
        {"include_synthetic": "bool"},
    ),
    "serving_report": (
        "Who is using the serving tier and who met a limit: per-principal "
        "admitted/refused counts since service start, the suspended list and "
        "the per-class limits. No content and no query text — what a weekly "
        "review and a suspension decision need (ADR-157).",
        {},
    ),
    "compare": (
        "One quantity across sites, bucketed onto a shared time axis. The verb "
        "a cross-site question wants, and the one that makes declaring a role "
        "worth the effort. Refuses to overlay two units: a figure drawing kW "
        "against degC on one axis is a lie about comparability, and an "
        "aggregate over both is worse because the lie has no shape to notice.",
        {"role": "str!", "bucket": "str!", "sites": "str", "fn": "str",
         "window": "dict", "include_synthetic": "bool",
         "allow_mixed_units": "bool"},
    ),
    "gaps": (
        "What one site would need to declare, ordered by how much each would "
        "fix. A steward cannot declare what they cannot see, and every gap "
        "found on the live node this week was found because a person went "
        "looking, which is not a process. Each entry names the specific act "
        "that closes it and what closing it would DO, in rows and direction — "
        "withholding readings and adding a unit are opposite changes wearing "
        "the same word. Ordered by rows rather than severity, because a "
        "severity is a judgement made on a site's behalf.",
        {"site": "str!", "dsn": "str",
         "collisions": "bool — also group every row to find timestamp collisions"},
    ),
    "install": (
        "Provision the data platform on Kubernetes.",
        {"namespace": "str", "release": "str"},
    ),
    "diagnose": (
        "Post-install health checks for the data platform.",
        {"connector": "str"},
    ),
    "ingest": (
        "Run one connector's source → bronze → RAG pass.",
        {"connector": "str!", "since": "str"},
    ),
    "refresh": (
        "Re-run a connector's pass over changes since its watermark.",
        {"connector": "str!"},
    ),
    "register": (
        "Register a connector — the landing zone a producer pushes into.",
        {"name": "str!", "kind": "str!", "bronze_root": "Path!"},
    ),
    "enroll": (
        "Enroll a source from its manifest: register the connector, record the "
        "site its rows belong to, and name the credential to issue.",
        {"manifest": "Path!", "bronze_root": "Path!"},
    ),
    "unregister": ("Remove a connector.", {"name": "str!"}),
    "connector_add": (
        "Set up a connector step by step: ask only what cannot be detected, read the "
        "credential without echo into the vault, verify live (sign in, open the target, read "
        "one item, dry-run the pull), register it and print its site declaration.",
        {"kind": "str!", "name": "str", "site": "str", "bronze_root": "path", "client_id": "str",
         "enterprise_id": "str", "app_ref": "str", "folder_id": "str", "schema_ref": "str",
         "default_disposition": "str", "check": "bool", "force": "bool"},
    ),
    "connector_list": ("Every registered connector, with its target and credential reference.", {}),
    "connector_show": ("One connector's registration, credentials shown only as references.",
                       {"name": "str!"}),
    "connector_test": (
        "Re-verify a registered connector live, with the same checks setup ran, and a dry-run "
        "pull. Writes nothing.",
        {"name": "str!"},
    ),
    "connector_remove": ("Remove a connector; its vault entry is kept unless asked.",
                         {"name": "str!", "drop_vault_entry": "bool"}),
    "list": (
        "List registered connectors, source kinds, or store backends.",
        {"resource": "str"},
    ),
    "troubleshoot": (
        "Explain why a connector is failing, with the fix.",
        {"connector": "str"},
    ),
    "preflight": (
        "Check a connector can reach its source and its bronze root.",
        {"connector": "str!"},
    ),
    "conform_try": (
        "Dry-run a registered normalizer over one bronze record and report the "
        "canonical rows it yields, plus the mistakes silver would absorb "
        "silently — a duplicate channel, a naive timestamp, a missing unit. "
        "Writes nothing. Omit schema_ref to list what is registered.",
        {"schema_ref": "str", "record": "object"},
    ),
    "expectations": (
        "The catalogue of conforming expectations: named, versioned checks a "
        "site declares on its rows on the way into silver (a declared "
        "no-reading value, say), with each one's parameters and actions "
        "(mark_invalid by default, drop, fail). `declared` lists what this "
        "node declares under <state_dir>/expectations/ and refuses a file it "
        "cannot honour. Reads files only.",
        {"action": "str — list (default) | show | declared", "name": "str — with show"},
    ),
    "conform_run": (
        "Run the real bronze→silver conform pass over every registered connector's "
        "bronze rows and upsert canonical rows into silver.signals. Site comes from "
        "the connector registry (axi data register --site). Reports the funnel and "
        "exits non-zero on a skipped connector or an unknown schema — no silent drops. "
        "Incremental by default: only the bytes not yet conformed, from a durable "
        "watermark per bronze file in the database; `full` reads everything, `watch_s` "
        "keeps passing. `rebuild` re-reads bronze for a scope (connector, site, start, "
        "end) and replaces the readings in place on their identity, reporting inserted / "
        "replaced / unchanged and refreshing gold; `dry_run` shows the counts and writes nothing.",
        {"bronze_root": "Path", "dsn": "str", "strict": "bool", "full": "bool", "rebuild": "bool",
         "watch_s": "float", "max_passes": "int", "batch_size": "int", "checkpoint_rows": "int",
         "connector": "str", "site": "str", "start": "str", "end": "str", "dry_run": "bool"},
    ),
    "reading_identity": (
        "Move a populated silver.signals onto its reading-identity key (site, feed, "
        "channel, ts, source_class; ADR-206). preflight is read-only: duplicates per "
        "site and feed (re-send vs divergent), the layout, and how the index can be built "
        "here and what it locks. collapse keeps the newest conform of each duplicated "
        "reading and copies the rest to silver.signals_identity_audit (a dry run unless "
        "apply; divergent only with accept_divergent). build-index builds the unique index.",
        {"action": "str — preflight | collapse | build-index", "dsn": "str", "site": "str",
         "start": "str", "end": "str", "apply": "bool", "accept_divergent": "bool",
         "window_days": "int", "lock_timeout_s": "float"},
    ),
    "catalog": (
        "What a medallion tier holds. One verb for every tier: bronze lists "
        "connectors with what landed and what the gate refused (by count — "
        "refused content is never readable through a generic verb); silver "
        "and gold list tables and views.",
        {"tier": "str — bronze | silver | gold (default gold)",
         "object": "str — one connector, on bronze"},
    ),
    "describe": (
        "The shape of one object in a tier: its columns and their types. "
        "Bronze refuses and says why — it holds the bytes a producer sent, "
        "in whatever shape they sent them, so there is no schema to describe.",
        {"tier": "str", "object": "str! — the table to describe"},
    ),
    "freshness": (
        "What has stopped advancing. The two tiers answer DIFFERENT questions "
        "and both matter: bronze says whether a PRODUCER is still pushing, "
        "silver whether the CONFORM PASS is advancing. A live producer whose "
        "pass has stalled looks fresh on one and stale on the other, and "
        "telling those apart is the difference between chasing a DAQ and "
        "chasing a pipeline.",
        {"tier": "str", "site": "str", "expect_hours": "float or {stream: float}"},
    ),
    "sample": (
        "Some records, to see the shape of what a producer is sending. Bronze "
        "only, and capped: the served tiers answer with aggregate and series, "
        "and handing back raw rows there would bypass the population guard.",
        {"tier": "str", "object": "str! — the connector", "day": "str", "limit": "int"},
    ),
    "site_rename": (
        "Put one site's data back in one place. A renamed site kept its "
        "history under the old id and gathered new readings under the new "
        "one, so every question answered from either saw half. "
        "`axiom.infra.site_identity` stops it recurring; this is the rows "
        "already written. Dry run by default — it rewrites the column "
        "everything else is keyed and scoped by, and a count is usually "
        "what somebody wanted anyway. Tables are discovered, because a "
        "hardcoded list goes stale and a stale list here leaves a site "
        "half-renamed.",
        {
            "old": "str — the former site id",
            "new": "str — the canonical site id",
            "apply": "bool — write the change (default false: count only)",
            "schemas": "list[str] — schemas to search (default the medallion ones)",
        },
    ),
    "ingest_freshness": (
        "Which feeds stopped advancing, and when. `silence` and CreditedGuard "
        "watch a LIVE producer's feed; they cannot say a site's data stopped "
        "four months ago, because then the producer is not running to notice. "
        "Reads gold.ingest_freshness and grades against `typical_gap`, the "
        "feed's OWN observed cadence, so an undeclared feed is still "
        "graded — refusing to grade without a declaration is how that four "
        "months went unreported. A declared cadence is an override.",
        {
            "site": "str — one site, or every site when absent",
            "expect_hours": "float or {feed: float} — the declared cadence",
            "now": "str — ISO timestamp, for tests",
        },
    ),
    "ensure_schema": (
        "Bring the conformance tier's schema up to the installed code — the "
        "silver.signals columns, the gold views, and the storage shape of every "
        "table declared as time series (ADR-189: hypertable, compression by "
        "identity, compression and retention policies). The Alembic-versioned "
        "extensions are `axi db migrate upgrade head`; this is the part that is "
        "raw DDL and was only ever applied by a conform pass, which is how a "
        "release can install cleanly and leave the database behind it. Reports "
        "the columns it actually added and the contract steps it actually took, "
        "read back after the fact. Skips index creation: that blocks writes and "
        "wants a maintenance window.",
        {"dsn": "str", "lock_timeout": "str", "time_partitioned": "bool",
         "compress_after_days": "int", "retain_days": "int", "reader": "str"},
    ),
    "rederive": (
        "Apply a site's declarations to the rows already written. A unit "
        "filled in, a fault code declared, a role corrected — none of it could "
        "reach existing data, because the conform insert was ON CONFLICT DO "
        "NOTHING and row_hash hashes the source row, which does not change "
        "when a channel map does. The bronze we retain so we can re-derive was "
        "unreachable. Dry run by default: everything is executed and rolled "
        "back, so the count is what would actually happen rather than a "
        "prediction. Scoped by site, because re-deriving every site over one "
        "site's correction is how maintenance becomes an outage.",
        {
            "site": "str — one site, or comma-separated; every site when absent",
            "apply": "bool — write the change (default false: count only)",
            "bronze_root": "Path", "dsn": "str",
        },
    ),
    "tier_audit": (
        "Report the medallion's actual shape — how many base tables and views "
        "in each tier — and everything ADR-128 did not expect: an undeclared "
        "base table in gold, or a gold view serving bronze directly. Reads "
        "information_schema, changes nothing, fails nothing. A static test can "
        "hold the CODE to the rule; it cannot see the database, and the "
        "database is where the drift showed up.",
        {"dsn": "str", "schemas": "str — comma-separated (default bronze,silver,gold)"},
    ),
    "pipeline_proof": (
        "Prove data is flowing bronze to silver to gold, per connector, and "
        "say whose problem it is when it is not. Separates a producer that "
        "stopped (SOURCE_SILENT) from a conform pass that is stuck "
        "(PIPELINE_STUCK), a backlog that is draining (BACKLOG), a connector "
        "that never flowed (NEVER_FLOWED) and gold serving something silver "
        "does not hold (GOLD_DISAGREES). Also reports whether the schema "
        "stamp is current and which sessions are idle in a transaction on "
        "silver.signals. Reads only.",
        {"dsn": "str", "connector": "str", "observe_s": "float — sample the watermarks twice, this far apart",
         "stuck_after_s": "float"},
    ),
    "activity": (
        "Recent ingest activity — what landed, what was refused.",
        {"connector": "str", "limit": "int"},
    ),
    "backfill_urls": (
        "Backfill source URLs onto rows that predate provenance capture.",
        {"connector": "str!"},
    ),
    "backup": ("Back up the data platform's stores.", {"target": "Path"}),
    "backup_validate": ("Verify a backup restores.", {"target": "Path!"}),
    "backup_policy": ("Show the backup policy as the node loads it.", {}),
    "ingest_push": (
        "Land pushed rows for a connector (the push front door's peer).",
        {"connector": "str!", "rows": "list!"},
    ),
}


# Read-only / dry-run verbs safe to project onto the agent + MCP surfaces
# (ADR-072 §4.9.4 bounded exposure). Everything else stays CLI-only until a
# deliberate per-verb decision — a write on MCP must declare its effect to be
# gated, not be exposed by default.
#: `sample` is deliberately absent: catalog and freshness are counts and
#: timestamps, while sample returns records from the substrate of record.
#: A write on MCP must declare its effect to be gated; so must a read that
#: hands back content.
_MCP_READS = frozenset(
    {"diagnose", "list", "troubleshoot", "preflight", "conform_try", "activity", "backup_validate",
     "catalog", "describe", "freshness", "aggregate", "series",
     # Deliberate: read-only, and it is the verb that tells an agent
     # whether the numbers it is about to quote can say how well they are
     # known. An agent that cannot ask this will quote a bare figure from
     # a surface where nothing declared anything, and nothing will object.
     "uncertainty_coverage",
     "gaps", "compare", "roles", "tier_audit", "serving_report", "pipeline_proof",
     "channels", "latest", "show", "expectations"}
)


#: Serving cost class per gold verb (ADR-157). Declared HERE, where the specs
#: are built, so the projection carries it to every surface; the guard in
#: ``skills/gold.py`` refuses a serving verb whose declaration is missing.
_COST_CLASSES: dict[str, str] = {
    "aggregate": "aggregate",
    "series": "series",
    "uncertainty_coverage": "lookup",
    "roles": "lookup",
    "compare": "aggregate",
    "serving_report": "lookup",
    "channels": "lookup",
    "latest": "lookup",
    "show": "series",
}


#: Chaining declarations (ADR-185), by verb. Only the verbs swept so far; the rest are on the debt list.
_CHAINING: dict[str, dict] = {
    # The everyday read verbs: composed from served reads, and write nothing.
    "channels": dict(risk="read"),
    "latest": dict(risk="read"),
    "show": dict(risk="read"),
    # Reads the catalogue, the watermark table and the bronze files' sizes; writes nothing.
    "pipeline_proof": dict(risk="read"),
    # Reads the catalogue and the declaration files; writes nothing.
    "expectations": dict(risk="read"),
    # Reads both copies read-only; appends to its own ledger. Nothing it records can harm either copy.
    "cutover_gate": dict(risk="reversible-write", requires=()),
    # The tenant data kit's file-writing verbs. Both only ADD files under the
    # kit's own data/ folder and refuse to overwrite one, so deleting what they
    # wrote undoes them: reversible.
    "kit_scaffold": dict(
        risk="reversible-write",
        requires=(
            Precondition("kit-present", "a data kit must exist (data/kit.toml)",
                         check=_kit.kit_present,
                         fix="data kit-init --tenant <your-site>, or pass dir=<site repository>"),
        ),
    ),
    "kit_snapshot": dict(risk="reversible-write", requires=()),
    # ADR-203. Promote records a bundle on the node; nothing is applied until an
    # operator approves. Approve applies views and registrations that kit-retire
    # removes. Retire drops the site's kit schema: destructive.
    "kit_promote": dict(
        risk="reversible-write",
        requires=(
            Precondition("kit-present", "a data kit must exist (data/kit.toml)",
                         check=_kit.kit_present,
                         fix="data kit-init --tenant <your-site>, or pass dir=<site repository>"),
        ),
    ),
    "kit_submit": dict(risk="reversible-write", requires=()),
    "kit_pull": dict(
        risk="reversible-write",
        requires=(
            Precondition("kit-present", "a data kit must exist (data/kit.toml)",
                         check=_kit.kit_present,
                         fix="data kit-init --tenant <your-site>, or pass dir=<site repository>"),
        ),
    ),
    "kit_review": dict(risk="read"),
    "kit_approve": dict(risk="reversible-write", requires=()),
    "kit_host_setup": dict(risk="reversible-write", requires=()),
    "kit_rollback": dict(risk="reversible-write", requires=()),
    "kit_withdraw": dict(risk="reversible-write", requires=()),
    "kit_retire": dict(risk="destructive", requires=()),
    # collapse removes duplicated readings from silver (copied to the audit table first).
    "reading_identity": dict(risk="destructive", requires=()),
    "kit_verbs": dict(risk="read"),
    "kit_charts": dict(risk="read"),
    "kit_automations": dict(risk="read"),
    # Sends a message and writes annotation rows into the site's own schema.
    "kit_automation_fire": dict(risk="reversible-write", requires=()),
    "kit_verb": dict(risk="read"),
    "kit_materialize": dict(risk="reversible-write", requires=()),
    "kit_sample": dict(
        risk="reversible-write",
        requires=(
            Precondition("kit-present", "a data kit must exist (data/kit.toml)",
                         check=_kit.kit_present,
                         fix="data kit-init --tenant <your-site>, or pass dir=<site repository>"),
            Precondition("source-declared", "the source must be declared in kit.toml",
                         check=_kit.source_declared,
                         fix='add [sources.{source}] with schema_ref (and kind = "deposit" for whole files) to kit.toml'),
            Precondition("origin-exists", "the raw data to bring in must exist",
                         check=_kit.origin_exists,
                         fix="pass origin=<a file or folder of your raw data>"),
        ),
    ),
    # Sends batches upstream, which cannot be recalled (the upstream dedupes by content hash), so the
    # vocabulary's middle level is the honest one: it adds copies elsewhere and removes nothing here.
    "forward": dict(
        risk="reversible-write",
        requires=(
            Precondition("outbox-configured", "the node must have an ingest outbox to read batches from",
                         check=forward.outbox_configured,
                         fix="pass outbox_dir=<directory>, or set AXIOM_INGEST_OUTBOX_DIR on this node"),
            Precondition("destination-given", "there must be somewhere to deliver to",
                         check=forward.target_given,
                         fix="pass intake_url=<upstream intake> with key_ref=<vault reference>, and/or box_remote=<rclone remote>"),
        ),
    ),
    # Reads heartbeats; with alert_to it also writes alerts to that principal's inbox.
    "partner_health": dict(risk="reversible-write", requires=()),
    # Deletes the oldest archived data under disk pressure (never a batch not yet delivered upstream, and
    # never newer than the declared minimum). It runs as the archive role's companion, which is a CLI
    # caller; from a protocol surface it goes through plan/apply.
    "archive_reclaim": dict(
        risk="destructive",
        requires=(
            Precondition("outbox-configured", "the node must have an ingest outbox whose archive is reclaimed",
                         check=forward.outbox_configured,
                         fix="pass outbox_dir=<directory>, or set AXIOM_INGEST_OUTBOX_DIR on this node"),
        ),
    ),
    # Writes a folder of deploy files under the path it is given; reads nothing but the package.
    "box_login": dict(risk="reversible-write", requires=()),
    # Stores one vault entry and writes one connector file; `connector remove --forget-secret` undoes both.
    "connector_add": dict(risk="reversible-write", requires=()),
    "connector_list": dict(risk="read"),
    "connector_show": dict(risk="read"),
    # Signs in and lists the source; the dry-run pull fetches no content and writes nothing.
    "connector_test": dict(risk="read"),
    # Deletes the connector file (and the vault entry only with drop_vault_entry).
    "connector_remove": dict(risk="destructive", requires=()),
    "edge_bundle": dict(risk="reversible-write", requires=()),
    # Asks each reachable node for /readyz and keeps the readings for the uptime report: it writes only
    # its own probe store.
    "probe_nodes": dict(risk="reversible-write", requires=()),
    # Copies verified windows into the new table beside the old one; the swap and the drop are not here.
    "adopt_tick": dict(risk="reversible-write", requires=()),
    # Reads the ledger and PULSE; writes only its own fault file and a HERALD event.
    "adopt_watch": dict(risk="reversible-write", requires=()),
    # Measures storage per table and per volume and writes its last report under the node's state for the
    # health surface; it changes no data.
    "storage": dict(risk="reversible-write", requires=()),
    # Writes one unit file under ~/.config/systemd/user and starts it; uninstall is
    # `systemctl --user disable --now` and removing the file.
    "orchestrator_install": dict(risk="reversible-write", requires=()),
    # One verb with several steps; classified by the riskiest: `drop_old` drops the old table, and `swap`
    # renames the live one. With no flag it only estimates, which is what a caller should run first.
    "adopt_timeseries": dict(
        risk="destructive",
        requires=(
            Precondition(
                "table-named",
                "an adoption works on one table, named schema.table",
                check=lambda params, ctx: "." in str(params.get("table") or ""),
                fix="pass table=<schema.table>; run with no flag first to see the estimate, which writes nothing",
            ),
        ),
    ),
}


def bind(registry: SkillRegistry) -> None:
    """Register every data_platform skill into ``registry``, with its spec."""
    # Bind the tiers before the verbs that dispatch to them, so a caller
    # cannot reach `catalog` before anything can answer it.
    from ..resolvers import register_all

    register_all()

    for verb, fn in {**_SKILLS, **_RETRIEVAL_SKILLS, **_CONNECTOR_SKILLS, **_NON_CLI_SKILLS, **_GOLD_SKILLS}.items():
        name = f"{_NAMESPACE}.{verb}"
        if registry.has(name):
            continue
        description, inputs = _SPECS.get(verb, ("", {}))
        if verb in _MCP_READS:
            spec = SkillSpec(
                name=name,
                fn=fn,
                description=description,
                inputs=inputs,
                idempotent=True,
                side_effects=False,
                surfaces=("cli", "mcp", "agent_tool"),
                cost_class=_COST_CLASSES.get(verb),
                **_CHAINING.get(verb, {}),
            )
        elif verb.startswith("kit_"):
            # The tenant data kit is meant to be driven by an assistant as much
            # as by a person, so it declares the agent surfaces too. It writes
            # (files, a local database), so it is a WRITE: confirm-gated.
            spec = SkillSpec(
                name=name,
                fn=fn,
                description=description,
                inputs=inputs,
                idempotent=verb in {"kit_up", "kit_try", "kit_check", "kit_down", "kit_review",
                                    "kit_verbs", "kit_verb", "kit_charts", "kit_automations"},
                side_effects=verb not in {"kit_review", "kit_verbs", "kit_verb", "kit_charts",
                                          "kit_automations"},
                # kit_submit is the node's receiving end, reached through the served
                # door; it has no terminal verb of its own (kit-promote is that).
                # kit_automation_fire is the scheduler's: no surface projects it.
                surfaces=(("mcp", "agent_tool") if verb == "kit_submit"
                          else () if verb == "kit_automation_fire"
                          else ("cli", "mcp", "agent_tool")),
                **_CHAINING.get(verb, {}),
            )
        else:
            spec = SkillSpec(name=name, fn=fn, description=description, inputs=inputs, **_CHAINING.get(verb, {}))
        registry.register_skill(spec)


def bind_default() -> SkillRegistry:
    """Bind into the process-local default registry; idempotent."""
    reg = default_registry()
    bind(reg)
    return reg


def verbs() -> list[str]:
    """Return the verb names (no namespace prefix). Used by the CLI parser.

    The gold verbs are included: they declare ``cli`` among their surfaces,
    and a declared surface that no parser backs is a surface that does not
    exist. They live in their own mapping because they share one module.
    """
    return list(_SKILLS) + list(_RETRIEVAL_SKILLS) + list(_CONNECTOR_SKILLS) + list(_GOLD_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
