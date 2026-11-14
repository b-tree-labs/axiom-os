# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The pure pieces of flag targeting: cohort rules, rollout rules, buckets, overrides (ADR-257 D5).

No database and no I/O here. :mod:`.resolver` wires these to the stored cohorts and
rollouts (``flags`` schema) and to the attribute catalogue (:mod:`.attributes`).
The design is ported from a rule engine proven in a product built on this platform;
see ``docs/specs/spec-flags.md`` §7.

Rule grammar (JSON)::

    {"and": [rule, ...]}  or {"all": [...]}       every branch
    {"or":  [rule, ...]}  or {"any": [...]}       some branch
    {"not": rule}
    {"attr": "<name>", "op": "<op>", "value": v}  attribute test
    {"in_cohort": "<key>"}  or {"cohort": "<key>"}  membership of another cohort

Operators: ``eq neq in not_in contains gt gte lt lte exists``. Strings compare without
case. A list-valued attribute (``role``) matches ``eq`` / ``in`` / ``contains`` when any of
its items does.

Three-valued logic (LOOP-F3): an attribute with no resolver, a resolver that fails, or a
resolver with no value for this subject is UNKNOWN (``None``). ``and`` is False if any
branch is False, ``or`` is True if any branch is True, ``not`` keeps UNKNOWN. A final
UNKNOWN is not a member, so a rule never admits anybody on a fact the platform does not
have, even through ``not``. ``exists`` is the one test that answers for a missing value
(False), and it too is UNKNOWN when the attribute has no resolver at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .flag_registry import BUCKET_SUBJECTS, UNIT_RE, normalize_value

LEAF_OPS = frozenset({"eq", "neq", "in", "not_in", "contains", "gt", "gte", "lt", "lte", "exists"})
_LIST_VALUE_OPS = frozenset({"in", "not_in"})
_NUMERIC_OPS = frozenset({"gt", "gte", "lt", "lte"})
MAX_RULE_DEPTH = 8
MAX_RULE_NODES = 200
MAX_COHORT_DEPTH = 8
MAX_ROLLOUT_RULES = 50
_AND, _OR = ("and", "all"), ("or", "any")
_COMBINATORS = _AND + _OR + ("not",)
_COHORT_LEAF_KEYS = ("in_cohort", "cohort")
ROLLOUT_RULE_KEYS = frozenset({"cohort", "match", "value", "percentage", "active_from", "active_until", "note",
                               "split", "bucket_by"})
MAX_SPLIT = 20


class RuleError(ValueError):
    """A cohort rule, rollout rule or override is malformed; the message says why."""


@dataclass(frozen=True)
class Attr:
    """One attribute of a subject. ``available=False``: no resolver could answer."""

    available: bool
    value: Any = None


@dataclass
class RuleResult:
    #: True, False, or None (UNKNOWN).
    value: bool | None
    #: Attributes this evaluation needed and did not have, in first-seen order.
    unknown: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ validation


def _cohort_leaf_key(rule: Mapping[str, Any]) -> str | None:
    for k in _COHORT_LEAF_KEYS:
        if k in rule:
            return k
    return None


def validate_rule(
    rule: Any,
    *,
    known_attributes: Iterable[str] | None = None,
    known_cohorts: Iterable[str] | None = None,
) -> None:
    """Raise :class:`RuleError` unless ``rule`` is well formed and within the size limits.

    ``None`` for ``known_attributes`` / ``known_cohorts`` skips that name check.
    """
    attrs = None if known_attributes is None else set(known_attributes)
    cohorts = None if known_cohorts is None else set(known_cohorts)
    nodes = [0]

    def go(r: Any, depth: int) -> None:
        if depth > MAX_RULE_DEPTH:
            raise RuleError(f"Rule nests deeper than {MAX_RULE_DEPTH} levels.")
        nodes[0] += 1
        if nodes[0] > MAX_RULE_NODES:
            raise RuleError(f"Rule has more than {MAX_RULE_NODES} nodes.")
        if not isinstance(r, dict):
            raise RuleError("A rule must be a JSON object.")
        combinators = [k for k in _COMBINATORS if k in r]
        if combinators:
            if len(r) != 1:
                raise RuleError("A combinator rule has exactly one key: and/all, or/any or not.")
            key = combinators[0]
            if key == "not":
                go(r["not"], depth + 1)
                return
            branches = r[key]
            if not isinstance(branches, list) or not branches:
                raise RuleError(f"'{key}' needs a non-empty list of rules.")
            for b in branches:
                go(b, depth + 1)
            return
        leaf = _cohort_leaf_key(r)
        if leaf is not None:
            if len(r) != 1:
                raise RuleError(f"A '{leaf}' leaf has exactly one key.")
            name = r[leaf]
            if not isinstance(name, str) or not name:
                raise RuleError(f"'{leaf}' needs a cohort key.")
            if cohorts is not None and name not in cohorts:
                raise RuleError(f"Unknown cohort '{name}'.")
            return
        name, op = r.get("attr"), r.get("op")
        if not isinstance(name, str) or not name:
            raise RuleError("An attribute test needs 'attr'.")
        if attrs is not None and name not in attrs:
            raise RuleError(f"Unknown attribute '{name}' (the catalogue: {', '.join(sorted(attrs))}).")
        if op not in LEAF_OPS:
            raise RuleError(f"Unknown operator '{op}' (use one of {', '.join(sorted(LEAF_OPS))}).")
        extra = set(r) - {"attr", "op", "value"}
        if extra:
            raise RuleError(f"Unexpected keys in rule: {sorted(extra)}.")
        if op == "exists":
            return
        if "value" not in r:
            raise RuleError(f"Operator '{op}' needs a value.")
        if op in _LIST_VALUE_OPS and not isinstance(r["value"], list):
            raise RuleError(f"Operator '{op}' needs a list value.")
        if op in _NUMERIC_OPS and (isinstance(r["value"], bool) or not isinstance(r["value"], int | float)):
            raise RuleError(f"Operator '{op}' needs a number.")

    go(rule, 0)


def cohort_refs(rule: Any) -> set[str]:
    """Every cohort key a rule names, at any depth."""
    out: set[str] = set()

    def go(r: Any) -> None:
        if not isinstance(r, dict):
            return
        leaf = _cohort_leaf_key(r)
        if leaf is not None and isinstance(r[leaf], str):
            out.add(r[leaf])
            return
        for k in _AND + _OR:
            for b in r.get(k) or []:
                go(b)
        if "not" in r:
            go(r["not"])

    go(rule)
    return out


def find_cycle(start: str, graph: Mapping[str, Iterable[str]]) -> list[str] | None:
    """A reference cycle reachable from ``start`` as ``[start, ..., start]``, else ``None``."""
    path: list[str] = []
    on_path: set[str] = set()
    done: set[str] = set()

    def go(k: str) -> list[str] | None:
        if k in on_path:
            return path[path.index(k):] + [k]
        if k in done:
            return None
        path.append(k)
        on_path.add(k)
        for nxt in sorted(graph.get(k) or ()):
            found = go(nxt)
            if found:
                return found
        path.pop()
        on_path.discard(k)
        done.add(k)
        return None

    return go(start)


def check_reference_depth(start: str, graph: Mapping[str, Iterable[str]]) -> None:
    """Raise when the cohort reference chain from ``start`` nests deeper than allowed.
    Run after :func:`find_cycle` found none."""

    def depth(k: str, seen: tuple[str, ...]) -> int:
        if len(seen) > MAX_COHORT_DEPTH:
            raise RuleError(
                f"Cohort '{start}' nests other cohorts more than {MAX_COHORT_DEPTH} levels deep "
                f"({' -> '.join(seen)})."
            )
        return 1 + max((depth(n, (*seen, n)) for n in graph.get(k) or ()), default=0)

    depth(start, (start,))


def validate_cohort_graph(key: str, rules: Any, stored: Mapping[str, Any]) -> None:
    """Refuse a cohort whose references would form a cycle (naming its path) or nest too deep.

    ``stored`` maps every other stored cohort key to its rules.
    """
    graph = {k: cohort_refs(v) for k, v in stored.items() if k != key}
    graph[key] = cohort_refs(rules)
    cycle = find_cycle(key, graph)
    if cycle:
        raise RuleError(f"Cohort '{key}' would form a reference cycle: {' -> '.join(cycle)}.")
    check_reference_depth(key, graph)


# ------------------------------------------------------------------ evaluation


def _norm(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    if isinstance(v, list | tuple | set | frozenset):
        return list(v)
    return [v]


def _missing(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, list | tuple | set | frozenset) and not v)


def _leaf(rule: Mapping[str, Any], have: Any) -> bool | None:
    op, want = rule["op"], rule.get("value")
    if op == "exists":
        return not _missing(have)
    if _missing(have):
        return None  # no data for this subject: UNKNOWN (LOOP-F3)
    haves = {_norm(h) for h in _as_list(have)}
    if op == "eq":
        return _norm(want) in haves
    if op == "neq":
        return _norm(want) not in haves
    if op == "in":
        return bool(haves & {_norm(w) for w in want})
    if op == "not_in":
        return not (haves & {_norm(w) for w in want})
    if op == "contains":
        return any(_norm(w) in haves for w in _as_list(want))
    try:
        h, w = float(have), float(want)
    except (TypeError, ValueError):
        return None
    return {"gt": h > w, "gte": h >= w, "lt": h < w, "lte": h <= w}[op]


def evaluate(
    rule: Mapping[str, Any],
    lookup: Callable[[str], Attr],
    cohort_lookup: Callable[[str], bool | None] | None = None,
) -> RuleResult:
    """Evaluate a validated rule with three-valued logic.

    ``lookup(attr) -> Attr``; ``cohort_lookup(key) -> True | False | None`` answers cohort
    leaves (no lookup, or ``None``, is UNKNOWN).
    """
    unknown: list[str] = []

    def note(name: str) -> None:
        if name not in unknown:
            unknown.append(name)

    def go(r: Mapping[str, Any]) -> bool | None:
        for key in _AND:
            if key in r:
                vals = [go(b) for b in r[key]]
                if any(v is False for v in vals):
                    return False
                return None if any(v is None for v in vals) else True
        for key in _OR:
            if key in r:
                vals = [go(b) for b in r[key]]
                if any(v is True for v in vals):
                    return True
                return None if any(v is None for v in vals) else False
        if "not" in r:
            v = go(r["not"])
            return None if v is None else not v
        leaf = _cohort_leaf_key(r)
        if leaf is not None:
            return cohort_lookup(r[leaf]) if cohort_lookup is not None else None
        attr = lookup(r["attr"])
        if not attr.available:
            note(r["attr"])
            return None
        value = _leaf(r, attr.value)
        if value is None:
            note(r["attr"])
        return value

    return RuleResult(value=go(rule), unknown=unknown)


# ------------------------------------------------------------------ percentage


def bucket(flag_key: str, subject_id: Any) -> float:
    """A stable position in [0, 100) for (flag, subject): same inputs, same bucket, on every
    node and in every session (LOOP-F5). Salted by the flag key, so the same subjects are not
    the first ten percent of every rollout."""
    digest = hashlib.sha256(f"{flag_key}:{subject_id}".encode()).digest()
    return (int.from_bytes(digest[:8], "big") % 10000) / 100.0


# ------------------------------------------------------------------ time windows


def parse_ts(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuleError(f"Not an ISO-8601 timestamp: {value!r}") from exc
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def rule_active(rule: Mapping[str, Any], now: datetime | None = None) -> bool:
    """A rollout rule applies only inside its optional [active_from, active_until) window."""
    t = now or datetime.now(UTC)
    start, end = parse_ts(rule.get("active_from")), parse_ts(rule.get("active_until"))
    return (start is None or t >= start) and (end is None or t < end)


# ------------------------------------------------------------------ rollout rules


def validate_split(split: Any, normalize: Callable[[Any], str | None], where: str) -> list[dict[str, Any]]:
    """A weighted split across values (LOOP-F17): ``[{variant, weight}, ...]``, weights > 0."""
    if not isinstance(split, list) or not split:
        raise RuleError(f"{where}: split is a non-empty list of {{variant, weight}}.")
    if len(split) > MAX_SPLIT:
        raise RuleError(f"{where}: a split has at most {MAX_SPLIT} parts.")
    out: list[dict[str, Any]] = []
    for j, part in enumerate(split):
        if not isinstance(part, dict) or set(part) - {"variant", "weight"}:
            raise RuleError(f"{where}: split[{j}] is {{variant, weight}}.")
        name = normalize(part.get("variant"))
        if name is None:
            raise RuleError(f"{where}: split[{j}] names {part.get('variant')!r}, not a declared variant of the flag.")
        w = part.get("weight")
        if isinstance(w, bool) or not isinstance(w, int | float) or w <= 0:
            raise RuleError(f"{where}: split[{j}] weight must be a number above 0.")
        out.append({"variant": name, "weight": w})
    return out


def pick_variant(flag_key: str, unit_id: str, split: list[Mapping[str, Any]]) -> tuple[str, float]:
    """The variant a unit gets from a weighted split, and its position in [0, 100).

    Salted apart from the admission bucket, so who is admitted by a percentage and which
    variant they get are independent; the same unit always lands on the same variant."""
    position = bucket(f"{flag_key}#split", unit_id)
    total = float(sum(p["weight"] for p in split))
    edge = 0.0
    for part in split:
        edge += 100.0 * float(part["weight"]) / total
        if position < edge:
            return str(part["variant"]), position
    return str(split[-1]["variant"]), position


def validate_rollout_rules(
    rules: Any,
    *,
    known_cohorts: Iterable[str],
    known_attributes: Iterable[str] | None = None,
    flag: Any = None,
) -> list[dict[str, Any]]:
    """The rules, normalised (``value`` as the flag spells it), or :class:`RuleError`.

    A rule targets one ``cohort`` or a ``match`` expression, never both; neither means
    everyone. ``percentage`` (0 to 100, default 100) applies after the target matched.
    A rule serves one ``value`` or a weighted ``split`` across values (LOOP-F17), and may
    name its own ``bucket_by`` unit (LOOP-F14). ``flag`` is the declaration the values are
    checked against; without one they are on/off/soon.
    """
    known = set(known_cohorts)
    normalize = flag.normalize if flag is not None else normalize_value
    multivariate = flag is not None and getattr(flag, "multivariate", False)
    spelled = f"one of its variants ({', '.join(flag.values)})" if multivariate else "on, off or soon"
    if not isinstance(rules, list):
        raise RuleError("Rollout rules must be a list (evaluated in order, the first that decides wins).")
    if len(rules) > MAX_ROLLOUT_RULES:
        raise RuleError(f"A rollout has at most {MAX_ROLLOUT_RULES} rules.")
    out: list[dict[str, Any]] = []
    for i, r in enumerate(rules):
        if not isinstance(r, dict):
            raise RuleError(f"Rule {i} must be an object.")
        extra = set(r) - ROLLOUT_RULE_KEYS
        if extra:
            raise RuleError(f"Rule {i} has unexpected keys {sorted(extra)}.")
        if "split" in r and "value" in r:
            raise RuleError(f"Rule {i}: serve a value or a split, not both.")
        norm: dict[str, Any] = {}
        if "split" in r:
            norm["split"] = validate_split(r["split"], normalize, f"Rule {i}")
        else:
            value = normalize(r.get("value"))
            if value is None:
                raise RuleError(f"Rule {i} needs a value: {spelled}.")
            norm["value"] = value
        if r.get("bucket_by") not in (None, ""):
            norm["bucket_by"] = check_unit(r["bucket_by"], known_attributes=known_attributes,
                                           known_cohorts=known, where=f"Rule {i}")
        cohort = r.get("cohort")
        if cohort is not None and "match" in r:
            raise RuleError(f"Rule {i}: use 'cohort' or 'match', not both.")
        if cohort is not None and cohort not in known:
            raise RuleError(f"Rule {i} names unknown cohort '{cohort}'.")
        if "match" in r:
            try:
                validate_rule(r["match"], known_attributes=known_attributes, known_cohorts=known)
            except RuleError as exc:
                raise RuleError(f"Rule {i} match: {exc}") from exc
        pct = r.get("percentage", 100)
        if isinstance(pct, bool) or not isinstance(pct, int | float) or not 0 <= pct <= 100:
            raise RuleError(f"Rule {i} percentage must be between 0 and 100.")
        parse_ts(r.get("active_from"))
        parse_ts(r.get("active_until"))
        out.append({**r, **norm})
    return out


def unit_cohorts(unit: str) -> list[str]:
    """The cohorts a ``cohort:a,b`` unit lists; empty for an attribute unit."""
    return unit.partition(":")[2].split(",") if unit.startswith("cohort:") else []


def check_unit(
    value: Any,
    *,
    known_attributes: Iterable[str] | None = None,
    known_cohorts: Iterable[str] | None = None,
    where: str = "bucket_by",
) -> str | None:
    """The bucketing unit (LOOP-F14), normalised, or :class:`RuleError`.

    Any attribute in the catalogue, or ``cohort:<a>,<b>`` naming stored cohorts.
    ``None`` / ``""`` is "not set here" (the flag's declared unit applies).
    """
    if value in (None, ""):
        return None
    text = str(value).strip().lower()
    if not UNIT_RE.match(text):
        raise RuleError(f"{where}: unit {value!r} is an attribute name or cohort:<key>,<key>.")
    cohorts = unit_cohorts(text)
    if cohorts:
        missing = [c for c in cohorts if known_cohorts is not None and c not in set(known_cohorts)]
        if missing:
            raise RuleError(f"{where}: unit names unknown cohort(s) {', '.join(missing)}.")
        return text
    if known_attributes is not None and text not in set(known_attributes) and text not in BUCKET_SUBJECTS:
        raise RuleError(f"{where}: unit {text!r} is not an attribute in the catalogue.")
    return text


def check_bucket_by(value: Any) -> str | None:
    """The unit with no catalogue to check it against (kept for callers before LOOP-F14)."""
    return check_unit(value)


def unit_key(unit: str, value: Any) -> str | None:
    """What a unit's value buckets as. The principal and account units bucket the bare id
    (as before LOOP-F14, so nobody moves); every other unit buckets ``<unit>=<value>``, so a
    node called ``ana`` and a principal called ``ana`` land apart. ``None`` when UNKNOWN."""
    if value is None or value == "" or (isinstance(value, list | tuple | set | frozenset) and not value):
        return None
    if isinstance(value, list | tuple | set | frozenset):
        value = ",".join(sorted(str(v) for v in value))
    if unit in BUCKET_SUBJECTS:
        return str(value)
    name = "cohort" if unit.startswith("cohort:") else unit
    return f"{name}={value}"


# ------------------------------------------------------------------ durations and local times

_DURATION = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: Any, what: str = "duration") -> int:
    """Seconds, from a number of seconds or ``30m`` / ``1h`` / ``2d`` / ``1w``."""
    if isinstance(value, bool):
        raise RuleError(f"{what} {value!r} is not a duration (30m, 1h, 2d).")
    if isinstance(value, int | float):
        seconds = int(value)
    else:
        text = str(value or "").strip().lower()
        if not text:
            raise RuleError(f"{what} is required (30m, 1h, 2d).")
        unit = text[-1]
        try:
            seconds = int(float(text[:-1]) * _DURATION[unit]) if unit in _DURATION else int(text)
        except ValueError as exc:
            raise RuleError(f"{what} {value!r} is not a duration (30m, 1h, 2d).") from exc
    if seconds < 0:
        raise RuleError(f"{what} cannot be negative.")
    return seconds


def zone(name: Any) -> Any:
    """The tz-database zone ``name`` names, else ``None``."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    text = str(name or "").strip()
    if not text:
        return None
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def local_instant(at: Any, timezone: Any, *, now: datetime | None = None) -> datetime:
    """The UTC instant of a wall-clock time in a stated zone (LOOP-F19), or :class:`RuleError`.

    ``at`` may carry its own offset; without one ``timezone`` is required. A wall-clock
    time the zone skips (the clocks jump forward) or passes twice (they fall back) is
    refused rather than guessed, and so is a time that has already passed.
    """
    tz = zone(timezone)
    if timezone not in (None, "") and tz is None:
        raise RuleError(f"{timezone!r} is not a time zone in the tz database (for example America/Chicago).")
    try:
        when = at if isinstance(at, datetime) else datetime.fromisoformat(str(at).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuleError(f"{at!r} is not an ISO-8601 time.") from exc
    if when.tzinfo is None:
        if tz is None:
            raise RuleError("say which time zone the time is in: give timezone (for example Europe/Berlin).")
        early, late = when.replace(tzinfo=tz, fold=0), when.replace(tzinfo=tz, fold=1)
        if early.utcoffset() != late.utcoffset():
            back = early.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
            if back != when:
                raise RuleError(f"{when.isoformat(timespec='minutes')} does not exist in {timezone}: "
                                "the clocks skip it.")
            raise RuleError(f"{when.isoformat(timespec='minutes')} happens twice in {timezone} (the clocks "
                            "fall back); give the time with its UTC offset.")
        when = early
    instant = when.astimezone(UTC)
    if instant <= (now or datetime.now(UTC)):
        raise RuleError(f"{instant.isoformat(timespec='minutes')} is in the past.")
    return instant


# ------------------------------------------------------------------ stage plans (LOOP-F18)

STAGE_STATUSES = ("running", "halted", "done", "rolled_back")
#: The statuses in which a plan's current stage serves; a rolled-back plan serves nothing.
SERVING_STATUSES = ("running", "halted", "done")


def stage_rules(row: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The rollout rules a stage plan's current stage stands for.

    A percentage stage admits that percentage of the plan's unit; ring stages are
    cumulative (stage 2 of internal, canary, early serves all three); ``everyone`` serves
    all. A plan serves its ``value`` or its ``split``.
    """
    if not row or row.get("status") not in SERVING_STATUSES:
        return []
    plan = row.get("plan") or {}
    stages = plan.get("stages") or []
    index = row.get("stage")
    if not isinstance(index, int) or not 0 <= index < len(stages):
        return []
    serve: dict[str, Any] = {"split": plan["split"]} if plan.get("split") else {"value": plan.get("value", "on")}
    unit = {"bucket_by": plan["bucket_by"]} if plan.get("bucket_by") else {}
    current = stages[index]
    if "percentage" in current:
        return [{**serve, **unit, "percentage": current["percentage"]}]
    if any(st.get("everyone") for st in stages[: index + 1]):
        return [{**serve, **unit}]
    return [{"cohort": st["cohort"], **serve, **unit} for st in stages[: index + 1] if st.get("cohort")]


# ------------------------------------------------------------------ overrides


@dataclass(frozen=True)
class OverrideView:
    """How one stored override reads for one flag here and now (LOOP-F10).

    ``status``: ``applied`` | ``expired`` | ``unreasoned_on_protected`` | ``invalid``.
    """

    applies: bool
    value: str | None
    status: str
    reason: str | None = None
    expires_at: str | None = None
    set_by: str | None = None

    def ignored(self, step: str) -> dict[str, Any]:
        return {"step": step, "status": self.status, "value": self.value, "reason": self.reason,
                "expires_at": self.expires_at, "set_by": self.set_by}


def read_override(entry: Any, *, protected: bool, now: datetime | None = None, flag: Any = None) -> OverrideView | None:
    """Read one stored override: a plain value (an older row) or ``{value, variant, reason,
    expires_at, updated_by}``. ``None`` when there is none. For a multivariate ``flag`` the
    value is the stored variant (LOOP-F17)."""
    if entry is None:
        return None
    if not isinstance(entry, Mapping):
        entry = {"value": entry}
    if flag is not None and getattr(flag, "multivariate", False):
        value = flag.normalize(entry.get("variant") or entry.get("value"))
    else:
        value = normalize_value(entry.get("value"))
    reason = (str(entry.get("reason") or "")).strip() or None
    by = entry.get("updated_by") or entry.get("set_by")
    if value is None:
        return OverrideView(False, None, "invalid", reason, None, by)
    try:
        exp = parse_ts(entry.get("expires_at"))
    except RuleError:
        return OverrideView(False, value, "invalid", reason, None, by)
    exp_s = exp.isoformat() if exp else None
    if exp is not None and exp <= (now or datetime.now(UTC)):
        return OverrideView(False, value, "expired", reason, exp_s, by)
    if protected and not reason:
        return OverrideView(False, value, "unreasoned_on_protected", None, exp_s, by)
    return OverrideView(True, value, "applied", reason, exp_s, by)


__all__ = [
    "LEAF_OPS",
    "MAX_COHORT_DEPTH",
    "MAX_ROLLOUT_RULES",
    "MAX_RULE_DEPTH",
    "MAX_RULE_NODES",
    "Attr",
    "OverrideView",
    "RuleError",
    "RuleResult",
    "bucket",
    "check_bucket_by",
    "check_unit",
    "stage_rules",
    "local_instant",
    "parse_duration",
    "pick_variant",
    "unit_cohorts",
    "unit_key",
    "validate_split",
    "zone",
    "check_reference_depth",
    "cohort_refs",
    "evaluate",
    "find_cycle",
    "parse_ts",
    "read_override",
    "rule_active",
    "validate_cohort_graph",
    "validate_rollout_rules",
    "validate_rule",
]
