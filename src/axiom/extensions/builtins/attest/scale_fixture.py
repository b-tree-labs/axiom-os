# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The scale fixture for the Log (LS-1, #1780): many fictional entries, generated in the database.

``<cli> log seed --scale N`` and the nightly scale tests write the same rows: ``N`` entries across
twenty fictional logs at one site, with the fixture vocabulary of the end-to-end suite (a desk, a
session, a check, a lead; D31), numbered sessions, attachments named by their hash and a few sealed
periods per log. Everything is a function of ``(N, seed, site)``: the same call writes the same
rows, and a second call adds nothing (``ON CONFLICT DO NOTHING``).

The rows are written by one ``INSERT ... SELECT generate_series`` per call, so a million entries
take seconds, not the hours signing them would. Their digests are unique and linked but not
signed: the budgets they serve are about reading. :func:`signed_chain` writes a short chain that
does verify, for the ``verify``-a-slice budget.

Dev only. :func:`refuse_unless_dev` refuses a production or staging mode, a bound node (whose data
is its site's) and any database that is not a lane's own or a test run's throwaway one.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: How many logs the fixture spreads its entries over.
LOGBOOKS = 20
#: The fixture's log ids: ``scale_log_01`` .. ``scale_log_20``.
LOG_PREFIX = "scale_log_"
#: Every ``SEAL_PERIODS``-th part of a log is closed by a seal, so each log has a few.
SEAL_PERIODS = 4
#: Entries per session; the first session is numbered ``SESSION_SEED``.
SESSION_LENGTH = 100
SESSION_SEED = 5000
#: Every this-many-th entry carries the search needle in its title (a rare text hit).
NEEDLE_EVERY = 9973
NEEDLE = "blue marker found"
#: Every this-many-th entry carries an attachment, named by its hash.
ATTACHMENT_EVERY = 50
#: Database names the fixture writes into: a lane's own, or a test run's throwaway one.
DEV_DATABASE = re.compile(r"^axiom_(lane|test)_[A-Za-z0-9_]+$")

TYPES: tuple[tuple[str, str], ...] = (
    ("OPEN", "authored"),
    ("CHECK", "performed"),
    ("NOTE", "authored"),
    ("CHANGE", "performed"),
    ("HANDOVER", "authored"),
    ("CLOSE", "authored"),
)
#: Each entry's ``area``, a declared choice filter (ADR-268 LF-A24): about a quarter each.
AREAS: tuple[str, ...] = ("North", "South", "East", "West")
WORDS: tuple[str, ...] = (
    "desk check done",
    "session opened",
    "reading taken at the desk",
    "lead told of the change",
    "handover to the next desk",
    "note left for the lead",
    "session closed",
)


class ScaleFixtureRefused(RuntimeError):
    """The fixture may not write here."""


def log_ids(count: int = LOGBOOKS) -> list[str]:
    return [f"{LOG_PREFIX}{i:02d}" for i in range(1, count + 1)]


def declaration(log_id: str) -> dict[str, Any]:
    """The fictional declaration of one fixture log (parsed by ``logbooks.parse_logbook``)."""
    number = log_id.removeprefix(LOG_PREFIX)
    return {
        "logbook": {"id": log_id, "version": "1", "display": f"Scale Desk {number}",
                    "handle": f"scale-desk-{number}"},  # fmt: skip
        "type": [
            {"id": t, "meanings": [m], "roles": ["member", "lead"],
             "fields": [{"id": "text", "type": "text", "label": "Text"},
                        {"id": "area", "type": "choice", "label": "Area", "choices": list(AREAS)}]}  # fmt: skip
            for t, m in TYPES
        ],
        "filter": [{"field": "area"}],
    }


def register(count: int = LOGBOOKS) -> list[str]:
    """Declare the fixture logs in this process (tests and a node that seeded them)."""
    from . import registry
    from .logbooks import parse_logbook

    ids = log_ids(count)
    for log_id in ids:
        registry.register(parse_logbook(declaration(log_id), source="scale-fixture"))
    return ids


def _toml_declaration(log_id: str) -> str:
    """:func:`declaration` as TOML (the fixture's own fixed shape: strings and lists only)."""
    d = declaration(log_id)
    head = d["logbook"]
    lines = ["[logbook]", *(f'{k} = "{v}"' for k, v in head.items()), ""]
    for t in d["type"]:
        choices = ", ".join(f'"{a}"' for a in AREAS)
        lines += ["[[type]]", f'id = "{t["id"]}"', f'meanings = ["{t["meanings"][0]}"]',
                  'roles = ["member", "lead"]',
                  'fields = [{ id = "text", type = "text", label = "Text" }, '
                  f'{{ id = "area", type = "choice", label = "Area", choices = [{choices}] }}]', ""]  # fmt: skip
    lines += ["[[filter]]", 'field = "area"', ""]
    return "\n".join(lines)


def write_extension(root: Path, count: int = LOGBOOKS) -> Path:
    """Write the fixture logs as a project-local extension under ``root`` (the checkout's
    ``.axi/extensions``, ignored by git), so a dev node started there declares them too."""
    home = root / "scale_fixture"
    (home / "logbooks").mkdir(parents=True, exist_ok=True)
    manifest = [
        "# Written by `log seed --scale --declare` (LS-1): fictional logs for scale tests. Dev only.",
        "[extension]", 'name = "scale_fixture"', 'version = "0.0.0"',
        'description = "Fictional logs written by the scale fixture (dev only)"', "",
    ]  # fmt: skip
    for log_id in log_ids(count):
        (home / "logbooks" / f"{log_id}.toml").write_text(_toml_declaration(log_id), encoding="utf-8")
        manifest += ["[[extension.provides]]", 'kind = "logbook"', f'file = "logbooks/{log_id}.toml"', ""]
    (home / "axiom-extension.toml").write_text("\n".join(manifest), encoding="utf-8")
    return home


# -- where it may write ------------------------------------------------------------------------


def refuse_unless_dev(db_url: str | None = None) -> str:
    """The database name the fixture may write into, or :class:`ScaleFixtureRefused`."""
    from sqlalchemy.engine import make_url

    from axiom.governance.mode import current_mode
    from axiom.infra.serving_mode import resolve_mode

    mode = current_mode()
    if mode != "dev":
        raise ScaleFixtureRefused(f"the scale fixture is for development; this node runs as {mode}")
    if resolve_mode().bound:
        raise ScaleFixtureRefused(
            "this node is bound to a site and reads that site's data; seed a lane's own database"
        )
    url = db_url or os.environ.get("AXIOM_DB_URL") or ""
    try:
        name = make_url(url).database or ""
    except Exception:  # noqa: BLE001 - an unreadable URL is not a lane's database
        name = ""
    if not DEV_DATABASE.match(name):
        raise ScaleFixtureRefused(
            f"database {name or '(none)'!r} is not a lane's own (axiom_lane_*) or a test run's "
            "(axiom_test_*); claim a lane (axi lane claim) and use its AXIOM_DB_URL"
        )
    return name


# -- the rows ----------------------------------------------------------------------------------

#: One row per ``g``: log ``1 + (g - 1) % L``, sequence ``1 + (g - 1) / L`` in it, so every chain
#: is gapless and links to its own previous row. Type, signer and title come from independent
#: bits of ``h``, a hash of the row, so every combination occurs in every log. A row at the end of
#: each of a log's first ``SEAL_PERIODS - 1`` parts is a seal covering that part.
RECORDS_SQL = """
INSERT INTO attest_records (attestation_id, site_id, logbook, seq, digest, prev_digest, kind,
                            meaning, entry_type, signer, occurred_at, recorded_at, record)
SELECT
    x.id, :site, x.log, x.seq, x.digest, x.prev, x.kind, x.meaning, x.etype, x.signer, x.at, x.at,
    jsonb_strip_nulls(jsonb_build_object(
        'attestation_id', x.id, 'seq', x.seq, 'logbook', x.log, 'site_id', :site,
        'kind', x.kind, 'meaning', x.meaning, 'entry_type', x.etype,
        'signer', jsonb_build_object('principal', x.signer),
        'occurred_at', to_char(x.at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'),
        'prev_digest', x.prev, 'digest', x.digest,
        'interval', jsonb_build_object('kind', 'session', 'number', x.session),
        'content', CASE WHEN x.kind = 'seal' THEN jsonb_build_object(
            'title', 'Close out: #' || (x.seq - :period + 1)::text || ' to #' || x.seq::text,
            'seal', jsonb_build_object('covers', jsonb_build_object(
                'from_seq', x.seq - :period + 1, 'to_seq', x.seq, 'head_seq', x.seq)))
        ELSE jsonb_strip_nulls(jsonb_build_object(
            'title', x.title,
            'fields', jsonb_build_object('text', x.title || ' (' || x.g::text || ')', 'area', x.area),
            'attachments', CASE WHEN x.g % :att_every = 0 THEN jsonb_build_array(jsonb_build_object(
                'sha256', encode(sha256(convert_to('att:' || :seed || ':' || x.g::text, 'UTF8')), 'hex'),
                'name', 'photo-' || x.g::text || '.jpg', 'bytes', 1000 + x.g % 9000)) END))
        END))
FROM (
    SELECT
        g,
        logs.ids[1 + (g - 1) % :logs] AS log,
        1 + (g - 1) / :logs AS seq,
        md5(:tag || ':id:' || g::text)::uuid::text AS id,
        md5(:tag || ':d1:' || g::text) || md5(:tag || ':d2:' || g::text) AS digest,
        CASE WHEN g <= :logs THEN repeat('0', 64)
             ELSE md5(:tag || ':d1:' || (g - :logs)::text) || md5(:tag || ':d2:' || (g - :logs)::text)
        END AS prev,
        CASE WHEN :period > 0 AND (1 + (g - 1) / :logs) % :period = 0
                  AND (1 + (g - 1) / :logs) < :period * :parts THEN 'seal' ELSE 'statement' END AS kind,
        CASE WHEN :period > 0 AND (1 + (g - 1) / :logs) % :period = 0
                  AND (1 + (g - 1) / :logs) < :period * :parts THEN 'seal'
             ELSE ((:meanings)::text[])[1 + h % :ntypes] END AS meaning,
        CASE WHEN :period > 0 AND (1 + (g - 1) / :logs) % :period = 0
                  AND (1 + (g - 1) / :logs) < :period * :parts THEN NULL
             ELSE ((:types)::text[])[1 + h % :ntypes] END AS etype,
        CASE WHEN g % 11 = 0 THEN '@lead:' || :site
             ELSE '@desk' || (h / 8 % 7)::text || ':' || :site END AS signer,
        timestamp with time zone '2025-01-01 00:00:00+00' + (g * interval '30 seconds') AS at,
        :session_seed + (1 + (g - 1) / :logs) / :session_len AS session,
        ((:words)::text[])[1 + h / 64 % :nwords]
            || CASE WHEN g % :needle_every = 0 THEN ' ' || :needle ELSE '' END AS title,
        ((:areas)::text[])[1 + h / 4096 % :nareas] AS area
    FROM (SELECT g, ('x' || substr(md5(:tag || ':h:' || g::text), 1, 12))::bit(48)::bigint AS h
          FROM generate_series(:first, :last) AS g) AS gs,
         (SELECT (:ids)::text[] AS ids) AS logs
) AS x
ON CONFLICT DO NOTHING
"""


@dataclass(frozen=True)
class Seeded:
    site: str
    logbooks: list[str]
    rows: int
    per_log: int
    seals_per_log: int
    seed: int

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _tag(seed: int, site: str, logbooks: Sequence[str]) -> str:
    """What makes ids and digests unique to this seed, site and set of logs."""
    return f"scale:{seed}:{site}:{logbooks[0]}:{len(logbooks)}"


def seed_records(session: Any, *, site: str, n: int, seed: int = 0,
                 logbooks: Sequence[str] | None = None, batch: int = 250_000) -> Seeded:  # fmt: skip
    """Write ``n`` fixture entries at ``site`` across ``logbooks`` (default the twenty fixture
    logs), in batches so a million rows never sit in one transaction's memory. The caller
    commits nothing: each batch commits itself, then the table is analysed."""
    from sqlalchemy import text

    if n < 1:
        raise ValueError("n is at least 1")
    ids = list(logbooks or log_ids())
    per_log = -(-n // len(ids))
    period = per_log // SEAL_PERIODS if per_log >= 2 * SEAL_PERIODS else 0
    params = {
        "site": site, "seed": int(seed), "tag": _tag(seed, site, ids), "ids": ids, "logs": len(ids),
        "period": period, "parts": SEAL_PERIODS, "types": [t for t, _ in TYPES],
        "meanings": [m for _, m in TYPES], "ntypes": len(TYPES), "words": list(WORDS),
        "nwords": len(WORDS), "areas": list(AREAS), "nareas": len(AREAS), "needle": NEEDLE, "needle_every": NEEDLE_EVERY,
        "att_every": ATTACHMENT_EVERY, "session_seed": SESSION_SEED, "session_len": SESSION_LENGTH,
    }  # fmt: skip
    first = 1
    while first <= n:
        last = min(n, first + batch - 1)
        session.execute(text(RECORDS_SQL), {**params, "first": first, "last": last})
        session.commit()
        first = last + 1
    session.execute(text("ANALYZE attest_records"))
    session.commit()
    seals = (SEAL_PERIODS - 1) if period else 0
    return Seeded(site=site, logbooks=ids, rows=n, per_log=per_log, seals_per_log=seals, seed=seed)


def signed_chain(session: Any, *, site: str, logbook: str, n: int, signer: Any,
                 seed: int = 0) -> dict[str, Any]:  # fmt: skip
    """Append ``n`` really signed entries to a new chain ``logbook`` (the ``verify`` budget):
    the same ``seal`` the signing path uses, written in one ``executemany``. Returns the head."""
    import json
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    from axiom.attest.chain import GENESIS, seal

    prev, rows = GENESIS, []
    base = datetime(2025, 6, 1, tzinfo=UTC)
    for i in range(1, n + 1):
        at = (base + timedelta(seconds=30 * i)).isoformat()
        etype, meaning = TYPES[(i + seed) % len(TYPES)]
        body = {
            "attestation_id": f"{seed:08x}-0000-4000-8000-{i:012x}", "logbook": logbook,
            "site_id": site, "kind": "statement", "meaning": meaning, "entry_type": etype,
            "signer": {"principal": f"@desk{i % 7}:{site}"}, "occurred_at": at,
            "content": {"title": WORDS[i % len(WORDS)], "fields": {"text": f"row {i}"}},
        }  # fmt: skip
        rec = seal(body, seq=i, prev_digest=prev, signer=signer)
        prev = rec["digest"]
        rows.append({
            "id": rec["attestation_id"], "site": site, "log": logbook, "seq": i,
            "digest": rec["digest"], "prev": rec["prev_digest"], "meaning": meaning, "etype": etype,
            "signer": body["signer"]["principal"], "at": at, "record": json.dumps(rec),
        })  # fmt: skip
    session.execute(
        text(
            "INSERT INTO attest_records (attestation_id, site_id, logbook, seq, digest, prev_digest, "
            "kind, meaning, entry_type, signer, occurred_at, recorded_at, record) VALUES (:id, :site, "
            ":log, :seq, :digest, :prev, 'statement', :meaning, :etype, :signer, :at, :at, "
            "CAST(:record AS jsonb))"
        ),
        rows,
    )
    session.commit()
    return {"seq": n, "digest": prev}


def seed(n: int, *, site: str, seed_value: int = 0, journal: bool = True,
         declare_in: Path | None = None) -> dict[str, Any]:  # fmt: skip
    """The CLI's entry: refuse unless this is a dev database, then write the entries (and the
    matching journal rows), and optionally declare the logs for a dev node."""
    database = refuse_unless_dev()
    from . import store

    with store.session_scope() as s:
        out = seed_records(s, site=site, n=n, seed=seed_value).as_dict()
    out["database"] = database
    if journal:
        from axiom.extensions.builtins.journal import scale_fixture as journal_fixture

        out["journal"] = journal_fixture.seed(n, site=site, seed_value=seed_value)
    if declare_in is not None:
        out["declared"] = str(write_extension(declare_in))
    return out


__all__ = [
    "LOGBOOKS",
    "NEEDLE",
    "ScaleFixtureRefused",
    "Seeded",
    "declaration",
    "log_ids",
    "refuse_unless_dev",
    "register",
    "seed",
    "seed_records",
    "signed_chain",
    "write_extension",
]
