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

from axiom.infra.skills import SkillRegistry, SkillSpec, default_registry

from . import (
    activity,
    backfill_urls,
    backup,
    backup_policy,
    backup_validate,
    conform_run,
    conform_try,
    diagnose,
    edge_pull,
    enroll,
    ensure_schema,
    gaps,
    ingest,
    ingest_freshness,
    ingest_push,
    install,
    preflight,
    rederive,
    refresh,
    register,
    retrieval,
    site_rename,
    tier_audit,
    troubleshoot,
    unregister,
)
from . import gold as _gold
from . import kit as _kit
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
    # The tenant data kit (docs/prds/prd-tenant-data-kit.md).
    "kit_init": _kit.kit_init,
    "kit_up": _kit.kit_up,
    "kit_try": _kit.kit_try,
    "kit_check": _kit.kit_check,
    "kit_down": _kit.kit_down,
    "ensure_schema": ensure_schema.run,
    "rederive": rederive.run,
    "tier_audit": tier_audit.run,
    "ingest_freshness": ingest_freshness.run,
    "edge_pull": edge_pull.run,
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

_NON_CLI_SKILLS = {
    "ingest_push": ingest_push.run,
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
    "kit_init": (
        "Write a data kit into a site repository: data/ with one working example "
        "of each contribution (a normalizer, silver declarations, a gold object, "
        "a verb chat can call, a chart) and sample data, so the whole loop runs "
        "on a fresh clone. Refuses a non-empty data/ unless force.",
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
    "kit_down": ("Remove the kit's local medallion.", {"dir": "path"}),
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
    "conform_run": (
        "Run the real bronze→silver conform pass over every registered connector's "
        "bronze rows and upsert canonical rows into silver.signals. Site comes from "
        "the connector registry (axi data register --site). Reports the funnel and "
        "exits non-zero on a skipped connector or an unknown schema — no silent drops.",
        {"bronze_root": "Path", "dsn": "str", "strict": "bool"},
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
        "silver.signals columns and the gold views. The Alembic-versioned "
        "extensions are `axi db migrate upgrade head`; this is the part that is "
        "raw DDL and was only ever applied by a conform pass, which is how a "
        "release can install cleanly and leave the database behind it. Reports "
        "the columns it actually added, read back after the fact. Skips index "
        "creation: that blocks writes and wants a maintenance window.",
        {"dsn": "str", "lock_timeout": "str"},
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
     "gaps", "compare", "roles", "tier_audit", "serving_report"}
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
}


def bind(registry: SkillRegistry) -> None:
    """Register every data_platform skill into ``registry``, with its spec."""
    # Bind the tiers before the verbs that dispatch to them, so a caller
    # cannot reach `catalog` before anything can answer it.
    from ..resolvers import register_all

    register_all()

    for verb, fn in {**_SKILLS, **_RETRIEVAL_SKILLS, **_NON_CLI_SKILLS, **_GOLD_SKILLS}.items():
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
                idempotent=verb in {"kit_up", "kit_try", "kit_check", "kit_down"},
                side_effects=True,
                surfaces=("cli", "mcp", "agent_tool"),
            )
        else:
            spec = SkillSpec(name=name, fn=fn, description=description, inputs=inputs)
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
    return list(_SKILLS) + list(_RETRIEVAL_SKILLS) + list(_GOLD_SKILLS)


__all__ = ["bind", "bind_default", "verbs"]
