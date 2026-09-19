# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""SQL function signature → capability input spec (one projector, ADR-072).

A database function is a capability: ``pg_get_function_arguments`` is its
signature and ``obj_description`` its docstring. This module turns those two
strings into the same ``inputs`` shape map a ``SkillSpec`` carries, so the
CLI verb, the chat tool and the MCP tool for one SQL function are all
``inputs_to_json_schema(...)`` of one record — provably the same schema from
the same source.

Everything here is a pure function: no database access and no driver
import. Run :data:`PG_FUNCTION_CATALOG_SQL` with whatever session you have
and hand the rows to :func:`catalog_rows_to_specs` (or one row's fields to
:func:`sql_function_to_spec`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from axiom.infra.capability_projection import REQUIRED_MARK, inputs_to_json_schema

# Catalog query a caller runs to feed this module. One row per plain
# function (``prokind = 'f'``) in ``:schema`` (a named bind parameter).
PG_FUNCTION_CATALOG_SQL = """
SELECT n.nspname                          AS schema,
       p.proname                          AS name,
       pg_get_function_arguments(p.oid)   AS args,
       pg_get_function_result(p.oid)      AS result,
       obj_description(p.oid, 'pg_proc')  AS comment
FROM   pg_proc p
JOIN   pg_namespace n ON n.oid = p.pronamespace
WHERE  n.nspname = :schema
  AND  p.prokind = 'f'
ORDER  BY p.proname, p.oid
""".strip()

# Default parameter-name prefix stripped from SQL argument names.
DEFAULT_PARAM_PREFIX = "p_"

_ARG_MODES = frozenset({"IN", "OUT", "INOUT", "VARIADIC"})

# Multi-word Postgres type names that ``format_type`` emits without a name
# in unnamed-parameter signatures (``"double precision, text"``).
_MULTIWORD_TYPES = (
    "double precision",
    "character varying",
    "bit varying",
    "timestamp with time zone",
    "timestamp without time zone",
    "time with time zone",
    "time without time zone",
)

# Normalized Postgres type name → capability input shape (the vocabulary
# ``inputs_to_json_schema`` maps). Anything else (enums, domains, composite
# types) projects to ``str``.
_PG_TYPE_MAP: dict[str, str] = {
    # text-ish
    "text": "str",
    "varchar": "str",
    "character varying": "str",
    "char": "str",
    "character": "str",
    "bpchar": "str",
    "name": "str",
    "citext": "str",
    "uuid": "str",
    "bytea": "str",
    "inet": "str",
    "cidr": "str",
    "macaddr": "str",
    "macaddr8": "str",
    "xml": "str",
    "interval": "str",
    "time": "str",
    "timetz": "str",
    "time with time zone": "str",
    "time without time zone": "str",
    "regclass": "str",
    "regproc": "str",
    "tsvector": "str",
    "tsquery": "str",
    # integers
    "smallint": "int",
    "int2": "int",
    "integer": "int",
    "int": "int",
    "int4": "int",
    "bigint": "int",
    "int8": "int",
    "serial": "int",
    "serial2": "int",
    "serial4": "int",
    "serial8": "int",
    "smallserial": "int",
    "bigserial": "int",
    "oid": "int",
    "xid": "int",
    # reals
    "real": "float",
    "float": "float",
    "float4": "float",
    "float8": "float",
    "double precision": "float",
    "numeric": "float",
    "decimal": "float",
    "money": "float",
    # booleans
    "boolean": "bool",
    "bool": "bool",
    # temporal
    "date": "date",
    "timestamp": "datetime",
    "timestamptz": "datetime",
    "timestamp with time zone": "datetime",
    "timestamp without time zone": "datetime",
    # structured
    "json": "dict",
    "jsonb": "dict",
    "hstore": "dict",
    "anyarray": "list",
}


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


class PgFunctionArgs(NamedTuple):
    """Parsed ``pg_get_function_arguments`` output.

    ``types`` maps parameter name → Postgres type in declaration order
    (``OUT`` parameters excluded; ``INOUT`` and ``VARIADIC`` included).
    ``optional`` holds the names that carry a ``DEFAULT``. ``variadic`` is
    the ``VARIADIC`` parameter's name when the function has one.
    """

    types: dict[str, str]
    optional: frozenset[str]
    variadic: str | None = None


@dataclass(frozen=True)
class CapabilityInputs:
    """A SQL function projected to capability form.

    ``inputs`` is the ``SkillSpec.inputs`` shape map (required inputs carry
    :data:`~axiom.infra.capability_projection.REQUIRED_MARK`), so a
    ``SkillSpec(name=..., fn=..., inputs=spec.inputs, description=spec.description)``
    projects to the CLI, chat and MCP surfaces identically. ``optional`` is
    the same fact as a set, for callers that want it without re-parsing.
    """

    name: str
    inputs: dict[str, str] = field(hash=False)
    optional: frozenset[str] = frozenset()
    description: str = ""

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(n for n in self.inputs if n not in self.optional)

    def to_json_schema(self) -> dict[str, Any]:
        """The one projector's JSON Schema for this capability's inputs."""
        return inputs_to_json_schema(self.inputs)


# ---------------------------------------------------------------------------
# Signature parsing
# ---------------------------------------------------------------------------


def split_top_level(text: str, sep: str = ",") -> list[str]:
    """Split ``text`` on ``sep`` outside parentheses, brackets and quotes."""
    parts: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                # Doubled quote inside a quoted run is an escaped quote.
                if i + 1 < len(text) and text[i + 1] == quote:
                    buf.append(text[i + 1])
                    i += 1
                else:
                    quote = None
        elif ch in ("'", '"'):
            quote = ch
            buf.append(ch)
        elif ch in "([":
            depth += 1
            buf.append(ch)
        elif ch in ")]":
            depth = max(depth - 1, 0)
            buf.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


_DEFAULT_RE = re.compile(r"\bDEFAULT\b", re.IGNORECASE)


def _split_default(arg: str) -> tuple[str, bool]:
    """``"p_x int DEFAULT 1"`` → ``("p_x int", True)``; also the ``= expr`` form.

    Only a top-level (outside quotes/parens) ``DEFAULT`` or ``=`` counts, so
    ``text DEFAULT 'DEFAULT'`` and ``numeric(10,2)`` split correctly.
    """
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(arg):
        ch = arg[i]
        if quote:
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(depth - 1, 0)
        elif depth == 0:
            if ch == "=":
                return arg[:i].strip(), True
            m = _DEFAULT_RE.match(arg, i)
            if m and (i == 0 or arg[i - 1].isspace()):
                return arg[:i].strip(), True
        i += 1
    return arg.strip(), False


def _tokenize(decl: str) -> list[str]:
    """Whitespace-tokenize a declaration, keeping quoted identifiers and
    parenthesized type modifiers intact."""
    tokens: list[str] = []
    buf: list[str] = []
    depth = 0
    quote = False
    i = 0
    while i < len(decl):
        ch = decl[i]
        if quote:
            buf.append(ch)
            if ch == '"':
                if i + 1 < len(decl) and decl[i + 1] == '"':
                    buf.append('"')
                    i += 1
                else:
                    quote = False
        elif ch == '"':
            quote = True
            buf.append(ch)
        elif ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth = max(depth - 1, 0)
            buf.append(ch)
        elif ch.isspace() and depth == 0:
            if buf:
                tokens.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
        i += 1
    if buf:
        tokens.append("".join(buf))
    return tokens


def _unquote_identifier(token: str) -> str:
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1].replace('""', '"')
    return token


def _looks_like_unnamed_type(tokens: list[str]) -> bool:
    joined = " ".join(tokens).lower()
    return any(joined.startswith(mw) for mw in _MULTIWORD_TYPES)


def strip_param_prefix(name: str, prefix: str) -> str:
    """``p_start`` → ``start`` when ``prefix`` is ``p_``; never empties a name."""
    if prefix and name.startswith(prefix) and len(name) > len(prefix):
        return name[len(prefix) :]
    return name


def parse_pg_function_args(args_text: str, *, prefix: str = DEFAULT_PARAM_PREFIX) -> PgFunctionArgs:
    """Parse ``pg_get_function_arguments(oid)`` output into ``{name: type}``.

    Handles argument modes (``OUT`` parameters are excluded, ``INOUT`` and
    ``VARIADIC`` kept), ``DEFAULT`` / ``= expr`` defaults whose expressions
    may contain commas or parentheses, array and modified types
    (``text[]``, ``numeric(10,2)``), quoted identifiers, and unnamed
    parameters (named ``arg<N>`` by 1-based position). ``prefix`` is
    stripped from every parameter name; a collision after stripping is a
    ``ValueError`` rather than a silently overwritten input.
    """
    types: dict[str, str] = {}
    optional: set[str] = set()
    variadic: str | None = None

    for position, raw_arg in enumerate(split_top_level(args_text or ""), start=1):
        decl, has_default = _split_default(raw_arg)
        tokens = _tokenize(decl)
        if not tokens:
            continue

        mode = "IN"
        if tokens[0].upper() in _ARG_MODES and not tokens[0].startswith('"'):
            mode = tokens.pop(0).upper()
        if not tokens:
            continue

        if len(tokens) == 1 or (not tokens[0].startswith('"') and _looks_like_unnamed_type(tokens)):
            name = f"arg{position}"
            pg_type = " ".join(tokens)
        else:
            name = _unquote_identifier(tokens[0])
            pg_type = " ".join(tokens[1:])

        if mode == "OUT":
            continue

        name = strip_param_prefix(name, prefix)
        if name in types:
            raise ValueError(
                f"parameter name {name!r} is not unique after stripping prefix {prefix!r}"
            )
        types[name] = re.sub(r"\s+", " ", pg_type).strip()
        if has_default:
            optional.add(name)
        if mode == "VARIADIC":
            variadic = name

    return PgFunctionArgs(types=types, optional=frozenset(optional), variadic=variadic)


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------


def _normalize_pg_type(pg_type: str) -> str:
    text = pg_type.strip().lower()
    text = re.sub(r"\([^)]*\)", "", text)  # drop modifiers: numeric(10,2)
    text = re.sub(r"\s+", " ", text).strip()
    if "." in text and not text.endswith("[]"):
        text = text.rsplit(".", 1)[-1]  # drop schema qualifier
    elif "." in text:
        base = text[:-2].rsplit(".", 1)[-1]
        text = f"{base}[]"
    return text.strip('"')


def pg_type_to_input_type(pg_type: str) -> str:
    """Map a Postgres type name to the projector's input-shape vocabulary.

    Arrays (``text[]``, ``ARRAY``, ``anyarray``) → ``list``; type modifiers
    and schema qualifiers are ignored; unknown types (enums, domains,
    composites) → ``str``.
    """
    text = _normalize_pg_type(pg_type)
    if text.endswith("[]") or text.startswith("array") or text == "anyarray":
        return "list"
    return _PG_TYPE_MAP.get(text, "str")


# ---------------------------------------------------------------------------
# Spec projection
# ---------------------------------------------------------------------------


def sql_function_to_spec(
    name: str,
    args_text: str,
    comment: str | None = "",
    *,
    prefix: str = DEFAULT_PARAM_PREFIX,
) -> CapabilityInputs:
    """Project one catalog row to a :class:`CapabilityInputs` record.

    Parameters without a ``DEFAULT`` are required (shape carries
    :data:`REQUIRED_MARK`); ``comment`` (``obj_description``) becomes the
    capability description.
    """
    parsed = parse_pg_function_args(args_text, prefix=prefix)
    inputs: dict[str, str] = {}
    for param, pg_type in parsed.types.items():
        shape = pg_type_to_input_type(pg_type)
        if param not in parsed.optional:
            shape += REQUIRED_MARK
        inputs[param] = shape
    return CapabilityInputs(
        name=name,
        inputs=inputs,
        optional=parsed.optional,
        description=(comment or "").strip(),
    )


def catalog_rows_to_specs(
    rows: Iterable[Mapping[str, Any]], *, prefix: str = DEFAULT_PARAM_PREFIX
) -> list[CapabilityInputs]:
    """Project rows of :data:`PG_FUNCTION_CATALOG_SQL` (``name``, ``args``,
    ``comment`` keys) to records, in row order."""
    return [
        sql_function_to_spec(
            str(row["name"]), str(row.get("args") or ""), row.get("comment"), prefix=prefix
        )
        for row in rows
    ]


__all__ = [
    "DEFAULT_PARAM_PREFIX",
    "PG_FUNCTION_CATALOG_SQL",
    "CapabilityInputs",
    "PgFunctionArgs",
    "catalog_rows_to_specs",
    "parse_pg_function_args",
    "pg_type_to_input_type",
    "split_top_level",
    "sql_function_to_spec",
    "strip_param_prefix",
]
