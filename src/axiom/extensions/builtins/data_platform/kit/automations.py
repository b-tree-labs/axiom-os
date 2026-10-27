# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Kit automations: run a verb on a cadence, and act when a value crosses a line.

An automation is one more declaration (ADR-203, "Automations"): a TOML file under
``automations/`` that names one of the kit's own verbs, a cadence, an optional
condition on the verb's rows, and what to do when it holds::

    description = "Tell me when the hourly mean runs hot."
    every = "1h"                       # or: cron = "0 7 * * 1-5"
    verb = "my_view_by_hour"
    params = { limit = 24 }            # only the verb's declared params
    [when]                             # absent: act on every run
    column = "mean"
    above = 30.0                       # or below = ...; or any_rows = true
    [then]
    notify = "submitter"
    annotate = { label = "hot", note = "mean above 30" }

Nothing here runs contributor code. On the node a fire runs the promoted verb
through :func:`host.run_registered_verb` (as the site's role), evaluates the
condition in Python over the rows it returned, and acts: a HERALD message to
whoever submitted the bundle, and/or rows written into the site's own
``gold_<site>.annotations`` table, again as the site's role. The cadence itself
is a PULSE schedule, projected from the kit registry by :func:`sync_cadences`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .project import KitError, KitProject, slug

#: The PULSE action a fire dispatches (a qualified skill name, ADR-056).
FIRE_ACTION = "data.kit_automation_fire"
#: A kit may not ask the node to run its verb more often than this.
MIN_INTERVAL_SECONDS = 300
#: How many matched rows a message lists; the rest are counted.
MESSAGE_ROWS = 10
#: How many annotation rows one fire may write.
MAX_ANNOTATIONS = 500
#: Who sends the message: the data platform's agent, as its other events do.
ACTOR = "@plinth"

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
_EVERY = re.compile(r"^\s*(\d+)\s*(s|m|min|h|d)\s*$")
_EVERY_UNITS = {"s": 1, "m": 60, "min": 60, "h": 3600, "d": 86400}
_TOP_KEYS = {"description", "every", "cron", "verb", "params", "when", "then"}
_WHEN_KEYS = {"column", "above", "below", "any_rows"}
_THEN_KEYS = {"notify", "annotate"}
_ANNOTATE_KEYS = {"label", "note"}
#: Columns a row's time is read from for an annotation, in this order.
_TIME_COLUMNS = ("ts", "time", "timestamp", "bucket", "hour", "day", "date")


@dataclass(frozen=True)
class When:
    column: str | None = None
    above: float | None = None
    below: float | None = None
    any_rows: bool = False

    def text(self) -> str:
        if self.any_rows:
            return "any rows"
        if self.above is not None:
            return f"{self.column} above {_num(self.above)}"
        return f"{self.column} below {_num(self.below)}"


@dataclass(frozen=True)
class Automation:
    name: str
    description: str
    verb: str
    params: dict[str, Any]
    every_seconds: int | None
    cron: str | None
    when: When | None
    notify: bool
    annotate: dict[str, str] | None
    source: Path | None = field(default=None, compare=False)

    def schedule_text(self) -> str:
        return f"every {_every_text(self.every_seconds)}" if self.every_seconds else f"cron {self.cron}"

    def condition_text(self) -> str:
        return self.when.text() if self.when else "every run"

    def cadence(self):
        from axiom.extensions.builtins.schedule.api import Cadence

        if self.every_seconds:
            return Cadence(kind="interval", interval=timedelta(seconds=self.every_seconds))
        return Cadence(kind="cron", cron=self.cron)

    def to_record(self) -> dict[str, Any]:
        """The declaration as the registry keeps it (jsonb)."""
        return {
            "description": self.description, "verb": self.verb, "params": self.params,
            "every_seconds": self.every_seconds, "cron": self.cron,
            "when": asdict(self.when) if self.when else None,
            "notify": self.notify, "annotate": self.annotate,
        }

    @classmethod
    def from_record(cls, name: str, rec: dict[str, Any]) -> Automation:
        when = rec.get("when")
        return cls(
            name=name, description=str(rec.get("description", "")), verb=str(rec["verb"]),
            params=dict(rec.get("params") or {}), every_seconds=rec.get("every_seconds"),
            cron=rec.get("cron"), when=When(**when) if when else None,
            notify=bool(rec.get("notify")), annotate=rec.get("annotate"),
        )


def _num(x: float | None) -> str:
    return f"{x:g}" if isinstance(x, float) else str(x)


def _every_text(seconds: int | None) -> str:
    s = int(seconds or 0)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if s % size == 0:
            return f"{s // size}{unit}"
    return f"{s}s"


def parse_every(text: str) -> int:
    """``"1h"`` → 3600. Raises ``ValueError`` with the accepted forms."""
    m = _EVERY.match(str(text))
    if not m:
        raise ValueError(f"every = {text!r} must be a number and a unit, e.g. \"15m\", \"1h\" or \"1d\"")
    return int(m.group(1)) * _EVERY_UNITS[m.group(2)]


def cron_problem(expr: str) -> str | None:
    """Why ``expr`` is not a five-field cron that fires at most every 5 minutes."""
    fields = str(expr).split()
    if len(fields) != 5:
        return (f"cron = {expr!r} must have five fields (minute hour day month weekday), "
                f"e.g. \"0 7 * * 1-5\"")
    try:
        from croniter import croniter

        it = croniter(expr, datetime(2026, 1, 5, tzinfo=UTC))
        fires = [it.get_next(datetime) for _ in range(200)]
    except Exception as exc:  # noqa: BLE001 - croniter's message names the field
        return f"cron = {expr!r} is not a cron expression croniter accepts: {exc}"
    gap = min((b - a).total_seconds() for a, b in zip(fires, fires[1:]))
    if gap < MIN_INTERVAL_SECONDS:
        return (f"cron = {expr!r} fires every {int(gap // 60) or 1} minute(s); an automation runs at "
                f"least 5 minutes apart")
    return None


# -- reading the declarations ------------------------------------------------------


def load(project: KitProject, verbs: list[Any], gold: list[Any], problems: list[str]) -> list[Automation]:
    """Every ``automations/*.toml``, each problem naming its file and the fix."""
    from .declarations import _rel, _toml

    by_verb = {v.name: v for v in verbs}
    by_gold = {g.name: g for g in gold}
    out: list[Automation] = []
    for path in sorted(project.folder("automations").glob("*.toml")):
        label = _rel(project, path)
        here: list[str] = []
        doc = _toml(path, here, label)
        if not doc and here:
            problems.extend(here)
            continue
        a = _parse(path.stem, doc, label, by_verb, by_gold, here)
        problems.extend(here)
        if a is not None and not here:
            out.append(Automation(**{**a.__dict__, "source": path}))
    return out


def _parse(name: str, doc: dict[str, Any], label: str, by_verb: dict, by_gold: dict,
           p: list[str]) -> Automation | None:
    if not _NAME.match(name):
        p.append(f"{label}: the file name is the automation's name; use lowercase letters, digits and _")
        return None
    for key in sorted(set(doc) - _TOP_KEYS):
        p.append(f"{label}: {key} is not an automation field ({', '.join(sorted(_TOP_KEYS))})")
    description = str(doc.get("description") or "").strip()
    if not description:
        p.append(f"{label}: add a description; it is what the message and kit-automations show")

    every_seconds, cron = None, None
    has_every, has_cron = "every" in doc, "cron" in doc
    if has_every == has_cron:
        p.append(f'{label}: give exactly one of every or cron (e.g. every = "1h", or cron = "0 7 * * 1-5")')
    elif has_every:
        try:
            every_seconds = parse_every(str(doc["every"]))
        except ValueError as exc:
            p.append(f"{label}: {exc}")
        else:
            if every_seconds < MIN_INTERVAL_SECONDS:
                p.append(f"{label}: every = {doc['every']!r} is too often; an automation runs at least "
                         f"5 minutes apart")
    else:
        cron = str(doc["cron"]).strip()
        why = cron_problem(cron)
        if why:
            p.append(f"{label}: {why}")

    verb_name = str(doc.get("verb") or "")
    verb = by_verb.get(verb_name)
    if verb is None:
        p.append(f"{label}: verb = {verb_name!r} is not a verb in this kit "
                 f"({', '.join(sorted(by_verb)) or 'none'}); an automation runs one of the kit's own verbs")
    params = doc.get("params") or {}
    if not isinstance(params, dict):
        p.append(f"{label}: params must be a table of the verb's parameters, e.g. params = {{ limit = 24 }}")
        params = {}
    if verb is not None:
        for key in sorted(set(params) - set(verb.params)):
            p.append(f"{label}: verb {verb.name} has no parameter {key!r} "
                     f"(it takes {', '.join(sorted(verb.params)) or 'none'})")

    when = _parse_when(doc.get("when"), label, verb, by_gold, p)
    notify, annotate = _parse_then(doc.get("then"), label, p)
    if p:
        return None
    return Automation(name=name, description=description, verb=verb_name, params=dict(params),
                      every_seconds=every_seconds, cron=cron, when=when, notify=notify, annotate=annotate)


def _parse_when(raw: Any, label: str, verb: Any, by_gold: dict, p: list[str]) -> When | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        p.append(f"{label}: [when] must be a table (column + above/below, or any_rows = true)")
        return None
    for key in sorted(set(raw) - _WHEN_KEYS):
        p.append(f"{label}: [when] {key} is not a condition field ({', '.join(sorted(_WHEN_KEYS))})")
    tests = [k for k in ("above", "below", "any_rows") if k in raw]
    if len(tests) != 1:
        p.append(f"{label}: [when] needs exactly one of above, below or any_rows = true")
        return None
    if tests == ["any_rows"]:
        if raw["any_rows"] is not True:
            p.append(f"{label}: [when] any_rows must be true (omit [when] to act on every run)")
        return When(any_rows=True)
    bound = raw[tests[0]]
    if isinstance(bound, bool) or not isinstance(bound, (int, float)):
        p.append(f"{label}: [when] {tests[0]} must be a number, not {bound!r}")
        return None
    column = str(raw.get("column") or "").strip()
    if not column:
        p.append(f"{label}: [when] {tests[0]} needs a column: the column of the verb's rows to compare")
        return None
    if verb is not None and not _plausible(column, verb, by_gold):
        p.append(f"{label}: [when] column = {column!r} does not appear in verb {verb.name}'s query or in "
                 f"its object {verb.obj}; name a column of the rows the verb returns")
    return When(column=column, **{tests[0]: float(bound)})


def _plausible(column: str, verb: Any, by_gold: dict) -> bool:
    """A column the verb could return: named in its SQL, or in its object's SQL."""
    word = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(column)}(?![A-Za-z0-9_])", re.IGNORECASE)
    obj = by_gold.get(verb.obj)
    return bool(word.search(verb.sql) or (obj is not None and word.search(obj.sql)))


def _parse_then(raw: Any, label: str, p: list[str]) -> tuple[bool, dict[str, str] | None]:
    raw = raw if isinstance(raw, dict) else {}
    for key in sorted(set(raw) - _THEN_KEYS):
        p.append(f"{label}: [then] {key} is not an action (notify, annotate)")
    notify = False
    if "notify" in raw:
        if raw["notify"] != "submitter":
            p.append(f'{label}: [then] notify = "submitter" is the one recipient an automation has '
                     f"(whoever submitted the bundle), not {raw['notify']!r}")
        notify = True
    annotate = None
    if "annotate" in raw:
        a = raw["annotate"]
        if not isinstance(a, dict):
            p.append(f'{label}: [then] annotate must be a table, e.g. annotate = {{ label = "hot" }}')
        else:
            for key in sorted(set(a) - _ANNOTATE_KEYS):
                p.append(f"{label}: [then] annotate.{key} is not a field (label, note)")
            lab = str(a.get("label") or "").strip()
            if not lab or len(lab) > 64:
                p.append(f"{label}: [then] annotate needs a label of 1 to 64 characters")
            annotate = {"label": lab, "note": str(a.get("note") or "")}
    if not notify and annotate is None:
        p.append(f'{label}: give [then] at least one action: notify = "submitter" and/or '
                 f'annotate = {{ label = "..." }}')
    return notify, annotate


# -- evaluating a run -----------------------------------------------------------


def _number(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def matching_rows(a: Automation, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows the condition holds for (all of them with no condition, or ``any_rows``)."""
    if a.when is None or a.when.any_rows:
        return list(rows)
    col = a.when.column
    if rows and col not in rows[0]:
        raise KitError(f"automation {a.name}: the verb {a.verb}'s rows have no column {col!r} "
                       f"(they have {', '.join(rows[0])})")
    out = []
    for r in rows:
        v = _number(r.get(col))
        if v is None:
            continue
        if (a.when.above is not None and v > a.when.above) or (a.when.below is not None and v < a.when.below):
            out.append(r)
    return out


def _row_line(row: dict[str, Any], limit: int = 200) -> str:
    line = "  ".join(f"{k}={v}" for k, v in row.items())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _cli() -> str:
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name or "axi"
    except Exception:  # noqa: BLE001
        return "axi"


def message(site: str, a: Automation, matched: list[dict[str, Any]], *, total: int) -> tuple[str, str]:
    """``(subject, body)``: the kit, the automation, the matched rows (capped), how to stop it."""
    n = len(matched)
    subject = f"Kit {site} · {a.name}: {n} row(s) matched {a.condition_text()}"
    lines = [
        a.description,
        "",
        f"Verb {a.verb} returned {total} row(s); {n} matched {a.condition_text()} "
        f"(this automation runs {a.schedule_text()}).",
    ]
    lines += [f"  {_row_line(r)}" for r in matched[:MESSAGE_ROWS]]
    if n > MESSAGE_ROWS:
        lines.append(f"  … and {n - MESSAGE_ROWS} more")
    lines += [
        "",
        f"To stop it: remove automations/{a.name}.toml from the kit and promote it again, or ask the "
        f"node's operator to run `{_cli()} data kit-retire {site}`.",
    ]
    return subject, "\n".join(lines)


def _row_time(row: dict[str, Any]) -> datetime | None:
    picked = [row[k] for k in _TIME_COLUMNS if k in row] + list(row.values())
    for v in picked:
        if isinstance(v, datetime):
            return v if v.tzinfo else v.replace(tzinfo=UTC)
        if isinstance(v, date):
            return datetime(v.year, v.month, v.day, tzinfo=UTC)
        if isinstance(v, str) and len(v) >= 10 and v[4:5] == "-":
            try:
                t = datetime.fromisoformat(v)
            except ValueError:
                continue
            return t if t.tzinfo else t.replace(tzinfo=UTC)
    return None


def annotation_rows(a: Automation, matched: list[dict[str, Any]], at: datetime) -> list[tuple]:
    """``(ts, channel, label, note, automation)`` for each matched row, capped."""
    assert a.annotate is not None
    out = []
    for r in matched[:MAX_ANNOTATIONS]:
        note = a.annotate.get("note") or ""
        if not note and a.when is not None and not a.when.any_rows:
            note = f"{a.when.column} = {r.get(a.when.column)} ({a.condition_text()})"
        channel = r.get("channel")
        out.append((_row_time(r) or at, str(channel) if channel is not None else None,
                    a.annotate["label"], note, a.name))
    return out


def dry_run(project: KitProject, d: str, a: Automation, verbs: list[Any]) -> dict[str, Any]:
    """What one run would do against the sandbox. Sends and writes nothing."""
    from .runner import run_verb

    verb = next(v for v in verbs if v.name == a.verb)
    rows = run_verb(project, d, verb, a.params)
    matched = matching_rows(a, rows)
    would: list[str] = []
    if matched:
        if a.notify:
            subject, body = message(project.tenant, a, matched, total=len(rows))
            would.append(f"would notify the submitter: {subject}\n" + body)
        if a.annotate:
            n = min(len(matched), MAX_ANNOTATIONS)
            would.append(f"would annotate {n} row(s) {a.annotate['label']!r} in "
                         f"{project.gold_schema}.annotations")
    else:
        would.append(f"nothing: {len(rows)} row(s), none matched {a.condition_text()}")
    return {"schedule": a.schedule_text(), "rows": len(rows), "matched": len(matched), "would": would}


# -- the node side ----------------------------------------------------------------


def owner(site: str) -> str:
    """The identity a site's automations run under: its kit role, in its site's context."""
    return f"@kit_{slug(site)}:{site}"


def envelope(site: str, name: str) -> dict[str, Any]:
    return {"action": FIRE_ACTION, "params": {"site": site, "automation": name},
            "owner": owner(site), "role": f"kit_{slug(site)}"}


def _pulse_rows() -> list[Any]:
    from axiom.extensions.builtins.schedule import store as pulse_store
    from axiom.extensions.builtins.schedule.db_models import ScheduleDefinition

    with pulse_store.session_scope() as s:
        rows = (s.query(ScheduleDefinition)
                .filter(ScheduleDefinition.action == FIRE_ACTION)
                .filter(ScheduleDefinition.state != "cancelled")
                .order_by(ScheduleDefinition.created_at).all())
        for r in rows:
            s.expunge(r)
        return rows


def _row_key(row: Any) -> tuple[str, str]:
    params = (row.capability_envelope or {}).get("params") or {}
    return str(params.get("site") or ""), str(params.get("automation") or "")


def registered(dsn: str, *, site: str | None = None) -> list[dict[str, Any]]:
    """The automations in the kit registry (every site's, or one site's)."""
    from . import host

    host.ensure_registry(dsn)
    sql = (f"SELECT site, name, description, spec, submitted_by, bundle_id, last_run_at, last_outcome, "
           f"last_rows_matched, last_error FROM {host.SCHEMA}.automations")
    with host._connect(dsn) as conn, conn.cursor() as cur:
        if site:
            cur.execute(sql + " WHERE site = %s ORDER BY name", (site,))
        else:
            cur.execute(sql + " ORDER BY site, name")
        return host._rows(cur)


def sync_cadences(dsn: str, *, now: datetime | None = None) -> dict[str, str]:
    """Make PULSE hold exactly one active cadence per registered automation. Idempotent.

    Registered if missing, re-timed if its schedule changed, and cancelled when
    its automation is gone (re-approved without it, or the kit retired).
    """
    from axiom.extensions.builtins.schedule import api as pulse_api
    from axiom.extensions.builtins.schedule.api import ScheduleId, _cadence_payload

    now = now or datetime.now(UTC)
    want = {(r["site"], r["name"]): Automation.from_record(r["name"], r["spec"]) for r in registered(dsn)}
    have: dict[tuple[str, str], list[Any]] = {}
    for row in _pulse_rows():
        have.setdefault(_row_key(row), []).append(row)
    report: dict[str, str] = {}
    for (site, name), a in want.items():
        tag, rows, cadence = f"{site}/{name}", have.get((site, name), []), a.cadence()
        if not rows:
            sid = pulse_api.register(
                envelope=envelope(site, name), cadence=cadence, action=FIRE_ACTION,
                description=f"kit automation {site}/{name}: {a.verb} {a.schedule_text()}",
                extension="data_platform", retry_policy={"max_attempts": 1},
                misfire_policy="fire_once", now=now,
            )
            report[tag] = f"registered:{sid}"
            continue
        row, outcome = rows[0], "unchanged"
        if row.state == "paused":
            pulse_api.resume(ScheduleId(row.id), now=now)
            outcome = "resumed"
        if row.cadence_kind != cadence.kind or (row.cadence_payload or {}) != _cadence_payload(cadence):
            pulse_api.reschedule(ScheduleId(row.id), cadence=cadence, now=now)
            outcome = "rescheduled"
        for extra in rows[1:]:
            pulse_api.cancel(ScheduleId(extra.id), now=now)
        report[tag] = f"{outcome}:{row.id}"
    for key, rows in have.items():
        if key not in want:
            for r in rows:
                pulse_api.cancel(ScheduleId(r.id), now=now)
            report[f"{key[0]}/{key[1]}"] = "cancelled"
    return report


def sync_quietly(dsn: str) -> dict[str, str]:
    """:func:`sync_cadences`, with a failure reported rather than raised.

    The registry is the record, committed before this runs; the orchestrator
    re-projects it at startup, so a scheduler that was unreachable for this call
    catches up there.
    """
    try:
        return sync_cadences(dsn)
    except Exception as exc:  # noqa: BLE001 - reported to the operator in the result
        return {"error": f"the scheduler was not updated ({str(exc).splitlines()[0]}); the "
                         f"orchestrator re-projects automations when it starts"}


def schedule_state(site: str | None) -> dict[tuple[str, str], dict[str, Any]]:
    """``{(site, name): {state, next_fire_at}}`` from PULSE, best effort."""
    try:
        rows = _pulse_rows()
    except Exception:  # noqa: BLE001 - visibility, not a gate
        return {}
    return {_row_key(r): {"state": r.state, "next_fire_at": r.next_fire_at}
            for r in rows if site is None or _row_key(r)[0] == site}


Notifier = Callable[[str, str, str], list[dict[str, Any]]]


def herald_notify(recipient: str, subject: str, body: str) -> list[dict[str, Any]]:
    """The inbox of ``recipient`` always; the node's Teams channel too when it has one.

    Teams posts to a shared channel, whose members are not necessarily allowed
    to read this site's rows, so it gets the subject only and points at the
    inbox; the rows go to the submitter's inbox alone.
    """
    from axiom.extensions.builtins.notifications.send import (
        ChannelPreferences,
        NotificationPayload,
        Priority,
        SendContext,
        send,
    )
    from axiom.governance import Classification

    ctx = SEND_CONTEXT() if SEND_CONTEXT else SendContext.default()
    common = dict(actor=ACTOR, recipient=recipient, classification=Classification.INTERNAL,
                  priority=Priority.NORMAL, intent="data.kit_automation")
    # The inbox keeps one line of text per message, so the whole message is it.
    receipts = [send(ctx, payload=NotificationPayload(summary=f"{subject}\n{body}", body=body),
                     channel_prefs=ChannelPreferences(ordered_channels=("inbox",)), **common)]
    teams = next((p for p in ctx.registry.all() if getattr(p, "name", None) == "teams"), None)
    if teams is not None and "teams" in ctx.channel_configs:
        only = SendContext()
        only.registry.register(teams)
        only.channel_configs = {"teams": ctx.channel_configs["teams"]}
        receipts.append(send(only, payload=NotificationPayload(
            summary=f"{subject} (the rows are in {recipient}'s inbox)"),
            channel_prefs=ChannelPreferences(ordered_channels=("teams",)), **common))
    return [{"channel": r.channel_selected, "outcome": r.outcome, "error": r.error, "id": r.id}
            for r in receipts]


#: Tests bind a factory returning a ``SendContext`` with their own inbox store.
SEND_CONTEXT: Callable[[], Any] | None = None


def _annotate(dsn: str, site: str, a: Automation, matched: list[dict[str, Any]], at: datetime) -> int:
    from . import host

    rows = annotation_rows(a, matched, at)
    schema, role = f"gold_{slug(site)}", f"kit_{slug(site)}"
    with host._connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f'SET LOCAL ROLE "{role}"')  # the site's role writes its own table, or nothing
        cur.executemany(
            f'INSERT INTO "{schema}".annotations (ts, channel, label, note, automation) '
            f"VALUES (%s,%s,%s,%s,%s)",
            rows,
        )
    return len(rows)


def _record(dsn: str, site: str, name: str, *, outcome: str, matched: int | None, error: str | None) -> None:
    from . import host

    with host._connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            f"UPDATE {host.SCHEMA}.automations SET last_run_at = now(), last_outcome = %s, "
            f"last_rows_matched = %s, last_error = %s WHERE site = %s AND name = %s",
            (outcome, matched, error, site, name),
        )


def fire(dsn: str, *, site: str, name: str, notifier: Notifier | None = None,
         now: datetime | None = None) -> dict[str, Any]:
    """One run of one promoted automation: verb as the site's role, condition, actions, record."""
    from . import host

    now = now or datetime.now(UTC)
    host.ensure_registry(dsn)
    with host._connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT spec, submitted_by FROM {host.SCHEMA}.automations WHERE site=%s AND name=%s",
                    (site, name))
        row = cur.fetchone()
    if row is None:
        return {"site": site, "automation": name, "outcome": "not registered",
                "note": "the automation was retired or replaced; its cadence is cancelled at the next sync"}
    spec, submitted_by = row
    a = Automation.from_record(name, spec if isinstance(spec, dict) else json.loads(spec))
    try:
        rows = host.run_registered_verb(dsn, site=site, name=a.verb, params=a.params)
        matched = matching_rows(a, rows)
        done: list[str] = []
        sent: list[dict[str, Any]] = []
        if matched and a.annotate:
            done.append(f"annotated {_annotate(dsn, site, a, matched, now)}")
        if matched and a.notify:
            subject, body = message(site, a, matched, total=len(rows))
            sent = (notifier or herald_notify)(submitted_by, subject, body)
            failed = [s for s in sent if s.get("outcome") not in ("succeeded", "suppressed_duplicate")]
            if failed:
                raise KitError(f"the message to {submitted_by} was not delivered: "
                               + "; ".join(f"{s.get('channel')}: {s.get('error')}" for s in failed))
            done.append(f"notified {submitted_by}")
        outcome = ", ".join(done) if done else "no match"
    except Exception as exc:  # noqa: BLE001 - recorded, then raised so PULSE dead-letters it
        reason = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        _record(dsn, site, name, outcome="error", matched=None, error=reason)
        raise
    _record(dsn, site, name, outcome=outcome, matched=len(matched), error=None)
    return {"site": site, "automation": name, "rows": len(rows), "matched": len(matched),
            "outcome": outcome, "sent": sent}


__all__ = [
    "ACTOR", "Automation", "FIRE_ACTION", "MIN_INTERVAL_SECONDS", "When", "annotation_rows",
    "cron_problem", "dry_run", "envelope", "fire", "herald_notify", "load", "matching_rows", "message",
    "owner", "parse_every", "registered", "schedule_state", "sync_cadences", "sync_quietly",
]
