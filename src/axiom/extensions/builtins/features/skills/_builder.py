# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The rule builder's verbs, shared by the web builder and chat (dual drive, owner decision).

* ``flags.attributes`` and ``flags.values``: the vocabulary a builder offers (SW-A26): every
  attribute with its group and label, and the values to pick from (``source: null`` = typed).
* ``flags.cohort_draft_*`` and ``flags.rollout_draft_*`` (create, update, get, discard): a
  person's unsigned work on one cohort or rollout, ONE object per (person, site, kind, key) that
  the builder, chat, MCP and the command line all edit. Every write is checked against the rule
  grammar (``check_cohort`` / ``check_rollout``) and refused in words. A draft never applies,
  signs or opens a plan: saving it is the preview, then a person's signature (DC-10).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ._flags import _service, default_account
from ._loop import _actor, _declared, _fail, _json, _list, _Refused, _targeting

KINDS = ("cohort", "rollout")
#: The surface a save came from, as a draft records it.
_VIA = {"web": "web", "mcp": "mcp", "chat": "chat", "agent_tool": "chat"}


# ------------------------------------------------------------------ the vocabulary


def attributes_list(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.attributes``: every attribute a rule may name, as a builder's menu shows it."""
    from .. import vocabulary

    return SkillResult(value={"attributes": vocabulary.rows()})


def values(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.values``: the values to pick for one attribute, or ``source: null`` to type one."""
    from .. import vocabulary

    try:
        limit = int(params.get("limit") or 50)
    except (TypeError, ValueError):
        return SkillResult(ok=False, errors=["limit is a whole number"])
    try:
        return SkillResult(value=vocabulary.values(str(params.get("attribute") or "").strip(),
                                                   str(params.get("query") or ""), limit))
    except vocabulary.UnknownAttribute as exc:
        return SkillResult(ok=False, errors=[str(exc)])


# ------------------------------------------------------------------ waiting for approval (Q16)


def _subject_of(change: dict[str, Any]) -> tuple[str, str]:
    kind = "cohort" if str(change.get("capability") or "").split(".", 1)[-1].startswith("cohort") else "flag"
    return kind, str((change.get("params") or {}).get("key") or "")


def waiting(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.waiting``: the changes waiting for a second person's approval (F23), optionally for one
    ``subject`` (``cohort:<key>`` or ``flag:<key>``). Only changes in awaiting_approval are listed."""
    from .. import asks

    subject = str(params.get("subject") or "").strip()
    want = tuple(subject.split(":", 1)) if subject else None
    if want is not None and (len(want) != 2 or want[0] not in ("cohort", "flag")):
        return SkillResult(ok=False, errors=["subject is cohort:<key> or flag:<key>"])
    try:
        ts = _targeting(_service())
    except _Refused:
        return SkillResult(value={"waiting": []})
    names = {a["principal"]: a["name"] for a in asks.site_admins()}
    rows = []
    for c in ts.changes(statuses=["awaiting_approval"]):
        kind, key = _subject_of(c)
        if want is not None and (kind, key) != want:
            continue
        rows.append({"kind": kind, "key": key, "author": c["author"],
                     "author_name": names.get(c["author"]) or c["author"], "checked_at": c["created_at"],
                     "change_id": c["id"]})
    return SkillResult(value={"waiting": rows})


def rule_depth_audit(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    """``flags.rule_depth_audit``: every stored cohort and rollout rule that nests groups deeper than
    one level (Q14). Reads only: nothing is rewritten."""
    from .. import targeting as t

    try:
        ts = _targeting(_service())
    except _Refused as exc:
        return SkillResult(value={"too_deep": [], "checked": {"cohorts": 0, "rollouts": 0}, "targeting": str(exc)})

    def deep(rule: Any) -> bool:
        try:
            t.validate_rule(rule)
        except t.RuleError as exc:
            return str(exc) == t.TOO_DEEP
        return False

    stored, rollouts = ts.cohorts(), ts.all_rollouts()
    found = [{"kind": "cohort", "key": k, "why": t.TOO_DEEP} for k, c in sorted(stored.items())
             if c.get("rules") is not None and deep(c["rules"])]
    for r in rollouts:
        if any("match" in rule and deep(rule["match"]) for rule in r.get("rules") or []):
            found.append({"kind": "rollout", "key": r["key"], "environment": r["environment"], "why": t.TOO_DEEP})
    return SkillResult(value={"too_deep": found, "checked": {"cohorts": len(stored), "rollouts": len(rollouts)},
                              "targeting": ""})


# ------------------------------------------------------------------ drafts


def _store():
    from axiom.extensions.builtins.flags.store import DraftStore

    return DraftStore()


def _owner(ctx: SkillContext | None) -> tuple[str, str]:
    actor = _actor(ctx)
    return actor, actor.partition(":")[2] or default_account() or ""


def _via(ctx: SkillContext | None) -> str:
    return _VIA.get(str(getattr(ctx, "surface", None) or ""), "cli")


def _cohort_body(key: str, raw: Any) -> dict[str, Any]:
    from .. import targeting as t

    body = _json(raw, "body") or {}
    if not isinstance(body, dict):
        raise _Refused("body is an object: {name, description, rules, principals, accounts}")
    out = {"name": str(body.get("name") or ""), "description": str(body.get("description") or ""),
           "rules": _json(body.get("rules"), "rules"), "principals": _list(body.get("principals")),
           "accounts": _list(body.get("accounts"))}
    try:
        _targeting(_service()).check_cohort(key, rules=out["rules"])
    except t.RuleError as exc:
        raise _Refused(str(exc)) from exc
    return out


def _rollout_body(key: str, raw: Any) -> dict[str, Any]:
    from .. import targeting as t

    svc = _service()
    _declared(svc, key)
    body = _json(raw, "body") or {}
    if not isinstance(body, dict):
        raise _Refused("body is an object: {rules, bucket_by, environment}")
    rules = _json(body.get("rules"), "rules")
    if rules is None:
        rules = []
    bucket_by = str(body.get("bucket_by") or "").strip().lower() or None
    try:
        _targeting(svc).check_rollout(rules, bucket_by=bucket_by, flag=svc.get_registry()[key])
    except t.RuleError as exc:
        raise _Refused(str(exc)) from exc
    return {"rules": rules, "bucket_by": bucket_by, "environment": str(body.get("environment") or "") or None}


_BODIES: dict[str, Callable[[str, Any], dict[str, Any]]] = {"cohort": _cohort_body, "rollout": _rollout_body}


def _key(params: dict[str, Any]) -> str:
    key = str(params.get("key") or "").strip()
    if not key:
        raise _Refused("name it: key is required")
    return key


def _saved_body(kind: str, key: str, environment: Any = None) -> dict[str, Any] | None:
    """What is saved now for ``key``, as a draft body: a draft of a saved cohort or rollout starts
    from it (SW-A33)."""
    from ._loop import _env

    ts = _targeting(_service())
    if kind == "cohort":
        c = ts.cohort(key)
        return None if c is None else {k: c.get(k) for k in ("name", "description", "rules", "principals",
                                                                "accounts")}
    env = _env({"environment": environment})
    r = ts.rollout(key, env)
    return None if r is None or r.get("rules") is None else {"rules": r["rules"], "bucket_by": r.get("bucket_by"),
                                                             "environment": env}


def _summary(kind: str, before: dict[str, Any] | None, after: dict[str, Any]) -> str:
    """One line of what an amendment changed, as the receipt shows it (SW-A34)."""
    from .. import vocabulary
    from ..attributes import rule_attrs

    if before is None:
        return "Started the draft"
    said: list[str] = []
    old, new = rule_attrs(before.get("rules")), rule_attrs(after.get("rules"))
    said += [f"Removed the {vocabulary.label_of(a).lower()} condition" for a in sorted(old - new)]
    said += [f"Added a {vocabulary.label_of(a).lower()} condition" for a in sorted(new - old)]
    if not said and before.get("rules") != after.get("rules"):
        said.append("Changed the rule")
    for field_name, noun in (("principals", "person"), ("accounts", "account")):
        was, now = set(before.get(field_name) or ()), set(after.get(field_name) or ())
        for verb, names in (("Added", sorted(now - was)), ("Removed", sorted(was - now))):
            if names:
                said.append(f"{verb} {', '.join(names)}" if len(names) <= 2 else f"{verb} {len(names)} {noun}s")
    if before.get("name") != after.get("name") and after.get("name"):
        said.append(f"Renamed it {after['name']}")
    if kind == "rollout" and before.get("bucket_by") != after.get("bucket_by"):
        said.append("Changed what a percentage buckets by")
    return "; ".join(said[:2]) or "Updated the draft"


def _offers(kind: str, key: str, draft: dict[str, Any], before: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The card chat's answer carries (SW-A31): Open in builder and Apply, offered; and when chat
    amended a draft, the receipt the page shows with Undo (SW-A34)."""
    rev = draft["revision"]
    out: list[dict[str, Any]] = []
    if before is not None:
        out.append({"type": "flag_draft.amended", "auto": True, "kind": kind, "key": key, "revision": rev,
                    "summary": _summary(kind, before, draft["body"]), "before": before})
    out += [{"type": "flag_draft.open", "label": "Open in builder", "kind": kind, "key": key, "revision": rev},
            {"type": "flag_draft.apply", "label": "Apply", "kind": kind, "key": key, "revision": rev}]
    return out


def _save(kind: str, create: bool | None) -> Callable[[dict[str, Any], SkillContext], SkillResult]:
    def save(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
        from axiom.extensions.builtins.flags.store import DraftConflict

        try:
            key = _key(params)
            raw = params.get("body")
            if raw in (None, "", {}) and create is not False:
                raw = _saved_body(kind, key, (_json(params.get("body"), "body") or {}).get("environment")
                                  if isinstance(params.get("body"), dict) else None) or {}
            body = _BODIES[kind](key, raw)
            revision = params.get("revision")
            revision = None if revision in (None, "") else int(revision)
        except (_Refused, ValueError, TypeError) as exc:
            return _fail(exc)
        principal, site = _owner(ctx)
        prior = _store().get(principal, site, kind, key)
        try:
            draft = _store().save(principal, site, kind, key, body, by=principal, via=_via(ctx), revision=revision,
                                  create=create)
        except DraftConflict as exc:
            return SkillResult(ok=False, errors=[f"stale draft: {exc}"], value={"draft": exc.draft})
        except LookupError as exc:
            return SkillResult(ok=False, errors=[str(exc).strip("'\"")])
        value: dict[str, Any] = {"draft": draft}
        if _via(ctx) == "chat":
            value["ui_commands"] = _offers(kind, key, draft, prior["body"] if prior else None)
        return SkillResult(value=value,
                           actions_taken=[f"saved the {kind} draft {key} (revision {draft['revision']}); "
                                          "nothing applies until the person presses Apply"])

    save.__name__ = f"{kind}_draft_{'create' if create else 'update'}"
    return save


def _get(kind: str) -> Callable[[dict[str, Any], SkillContext], SkillResult]:
    def get(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
        try:
            key = _key(params)
        except _Refused as exc:
            return _fail(exc)
        principal, site = _owner(ctx)
        draft = _store().get(principal, site, kind, key)
        if draft is None:
            return SkillResult(ok=False, errors=[f"no {kind} draft {key!r} for {principal}"])
        return SkillResult(value={"draft": draft})

    get.__name__ = f"{kind}_draft_get"
    return get


def _discard(kind: str) -> Callable[[dict[str, Any], SkillContext], SkillResult]:
    def discard(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
        try:
            key = _key(params)
        except _Refused as exc:
            return _fail(exc)
        principal, site = _owner(ctx)
        if not _store().discard(principal, site, kind, key):
            return SkillResult(ok=False, errors=[f"no {kind} draft {key!r} for {principal}"])
        return SkillResult(value={"discarded": {"kind": kind, "key": key}},
                           actions_taken=[f"discarded the {kind} draft {key}"])

    discard.__name__ = f"{kind}_draft_discard"
    return discard


cohort_draft_create, cohort_draft_update = _save("cohort", True), _save("cohort", False)
rollout_draft_create, rollout_draft_update = _save("rollout", True), _save("rollout", False)
cohort_draft_get, rollout_draft_get = _get("cohort"), _get("rollout")
cohort_draft_discard, rollout_draft_discard = _discard("cohort"), _discard("rollout")


# ------------------------------------------------------------------ the rule grammar, for a model


def rule_schema() -> dict[str, Any]:
    """The cohort rule grammar as JSON Schema ``$defs``, exactly what ``check_cohort`` accepts."""
    from .. import attributes
    from ..targeting import LEAF_OPS

    ref = {"$ref": "#/$defs/rule"}
    many = {"type": "array", "items": ref, "minItems": 1}
    names = sorted(n for n, a in attributes.catalogue().items() if not a.deprecated)

    def one(key: str, schema: dict[str, Any], why: str) -> dict[str, Any]:
        return {"type": "object", "properties": {key: schema}, "required": [key], "additionalProperties": False,
                "description": why}

    rule = {
        "description": ("A cohort rule. Combine rules with and/all or or/any (a list) and not (one rule); test "
                        "one attribute with {attr, op, value}; read another cohort with {in_cohort: key}. Groups "
                        "nest one level only: a group may hold groups, and no deeper; put a deeper group in its "
                        "own cohort and name it with in_cohort (not adds no level around one test). Time reads "
                        "one clock, named in the rule: site_time / site_weekday read the site's time zone (the "
                        "site must have site.timezone set), my_time / my_weekday read the person's own clock; "
                        "times are decimal hours (09:30 is 9.5), weekdays mon to sun. An attribute nobody can "
                        "know for a person makes the test unknown, which never admits them."),
        "anyOf": [
            one("and", many, "every rule holds"), one("all", many, "every rule holds"),
            one("or", many, "at least one rule holds"), one("any", many, "at least one rule holds"),
            one("not", ref, "the rule does not hold"),
            {"type": "object", "required": ["attr", "op"], "additionalProperties": False,
             "description": "one attribute test; flags.values lists the values to pick for an attribute",
             "properties": {
                 "attr": {"type": "string", "enum": names, "description": "an attribute from flags.attributes"},
                 "op": {"type": "string", "enum": sorted(LEAF_OPS),
                        "description": "in/not_in take a list; gt/gte/lt/lte a number; exists no value"},
                 "value": {"description": "what the attribute is compared with"}}},
            one("in_cohort", {"type": "string"}, "a member of another stored cohort"),
        ],
    }
    rollout_rule = {
        "type": "object", "additionalProperties": False,
        "description": "one rollout rule; the first rule that decides wins",
        "properties": {
            "cohort": {"type": "string", "description": "a stored cohort this rule admits"},
            "match": ref,
            "value": {"type": "string", "description": "on | off | soon, or a variant"},
            "split": {"type": "array", "items": {"type": "object", "properties": {
                "variant": {"type": "string"}, "weight": {"type": "number"}}}},
            "percentage": {"type": "number", "minimum": 0, "maximum": 100},
            "bucket_by": {"type": "string"}, "active_from": {"type": "string", "format": "date-time"},
            "active_until": {"type": "string", "format": "date-time"}, "note": {"type": "string"}},
    }
    return {"rule": rule, "rollout_rule": rollout_rule}


def _draft_schema(kind: str, *, body: bool, revision: bool) -> Callable[[], dict[str, Any]]:
    def build() -> dict[str, Any]:
        props: dict[str, Any] = {"key": {"type": "string", "description": (
            "the cohort key (a lowercase identifier)" if kind == "cohort" else "the flag key")}}
        if body:
            if kind == "cohort":
                props["body"] = {"type": "object", "additionalProperties": False, "properties": {
                    "name": {"type": "string"}, "description": {"type": "string"},
                    "rules": {"$ref": "#/$defs/rule"},
                    "principals": {"type": "array", "items": {"type": "string"}},
                    "accounts": {"type": "array", "items": {"type": "string"}}}}
            else:
                props["body"] = {"type": "object", "additionalProperties": False, "properties": {
                    "rules": {"type": "array", "items": {"$ref": "#/$defs/rollout_rule"}},
                    "bucket_by": {"type": ["string", "null"]}, "environment": {"type": ["string", "null"]}}}
        if revision:
            props["revision"] = {"type": "integer", "description": (
                "the revision you last read; a save from an older one is refused with the current draft")}
        required = ["key", "body"] if body and revision else ["key"]
        return copy.deepcopy({"type": "object", "properties": props, "required": required, "$defs": rule_schema()})

    return build


DRAFT_SCHEMAS = {
    (kind, verb): _draft_schema(kind, body=verb in ("create", "update"), revision=verb == "update")
    for kind in KINDS for verb in ("create", "update", "get", "discard")
}


__all__ = [
    "DRAFT_SCHEMAS",
    "KINDS",
    "attributes_list",
    "cohort_draft_create",
    "cohort_draft_discard",
    "cohort_draft_get",
    "cohort_draft_update",
    "rollout_draft_create",
    "rollout_draft_discard",
    "rollout_draft_get",
    "rollout_draft_update",
    "rule_schema",
    "rule_depth_audit",
    "values",
    "waiting",
]
