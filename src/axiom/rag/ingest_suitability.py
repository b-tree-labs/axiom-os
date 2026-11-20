# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Refuse to embed what retrieval cannot use, and refuse to embed it twice.

A live corpus reached 5,304,997 chunks across 22,355 source files. The median
file contributed 15 chunks. **Five files contributed 52.7% of the entire
index**, the largest of them 1,036,802 chunks on its own — windows of this:

    168     167.9   167.9   167.8   167.7   167.8   167.8   167.8 ...

Sensor readings ingested as prose, sliced into overlapping windows, each
embedded into a 768-dimension float32 vector and indexed. It is structured data
and belongs in the signals tables. Embedding it destroys the structure and
answers no question a vector search is good at, while costing roughly 70 GB and
burying the retrievable content underneath itself.

**The windows are not duplicates.** Each differs in the last decimal place, so
a duplicate check would never have caught them — which is why the rules here
are about what a chunk *is*, not whether it has been seen before. Deduplication
is included because it is cheap and the column for it already existed, but it
is the smaller half by an order of magnitude.

Deleting the rows treats the symptom: nothing stopped this being embedded, so
it would recur on the next ingest. This is the upstream half.

**The guard cuts both ways, and the second direction is the dangerous one.** A
filter that admits nothing looks cautious and silently stops the corpus working.
Engineering prose is full of figures, and refusing it would drop exactly the
quantitative content the platform exists to answer over. Every threshold here is
named, adjustable, and reported with the numbers behind it, because a refusal a
human cannot check is one they will switch off entirely.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Above this share of numeric tokens, a chunk is data rather than prose.
#: Deliberately high: the offending telemetry scores near 1.0, while engineering
#: prose dense with figures sits well below. A lower bound would start refusing
#: tables and results sections that retrieval genuinely wants.
NUMERIC_FRACTION_LIMIT = 0.80

#: Below this ratio of distinct tokens to total, a chunk is a repeated reading.
#: A window of one value repeated forty times carries no retrievable
#: distinction, and a million of them crowd out everything else.
DISTINCT_TOKEN_RATIO_LIMIT = 0.20

#: Shorter than this and there is nothing to retrieve on.
MIN_TOKENS = 4

#: Above this share of isolated single *letters*, the text is a failed OCR pass
#: rather than writing. Found by running the guard over a real corpus: a 1966
#: scanned report produced chunks reading "i o n s tlnat gave t h e I-owest".
#: English does use one-letter words, so the bar is high enough that ordinary
#: prose — full of "a", "I" and short function words — is never near it.
#:
#: **Letters only, deliberately.** The first version counted any one-character
#: token and refused reactor status consoles — fixed-width tables of one-digit
#: readings — as "a failed OCR pass". Single digits are the numeric rule's
#: business, and a guard that names the wrong defect gets switched off.
SINGLE_CHAR_TOKEN_LIMIT = 0.40

_NUMERIC = re.compile(r"^[+-]?(\d[\d,]*\.?\d*|\.\d+)([eE][+-]?\d+)?[%°]?$")


@dataclass(frozen=True)
class Verdict:
    """Whether a chunk should be embedded, and the figures behind the answer."""

    suitable: bool
    reason: str
    tokens: int
    numeric_fraction: float
    distinct_token_ratio: float | None


def numeric_fraction(text: str) -> float:
    """Share of whitespace-separated tokens that are numbers.

    Token-based, not character-based: a long number is one token, and counting
    characters would make any document carrying a few timestamps look like
    telemetry.
    """
    tokens = str(text or "").split()
    if not tokens:
        return 0.0
    numeric = sum(1 for t in tokens if _NUMERIC.match(t.strip("()[]{},;:")))
    return round(numeric / len(tokens), 4)


def assess_chunk(
    text: str,
    *,
    numeric_limit: float = NUMERIC_FRACTION_LIMIT,
    distinct_limit: float = DISTINCT_TOKEN_RATIO_LIMIT,
    min_tokens: int = MIN_TOKENS,
) -> Verdict:
    """Should this chunk be embedded?"""
    tokens = str(text or "").split()
    count = len(tokens)
    if count < min_tokens:
        return Verdict(
            False, f"empty or too short: {count} tokens < {min_tokens}", count, 0.0, None
        )

    fraction = numeric_fraction(text)
    distinct_ratio = round(len(set(tokens)) / count, 4)

    singles = sum(
        1 for t in tokens if len(stripped := t.strip(".,;:()[]{}\"'")) == 1 and stripped.isalpha()
    )
    single_ratio = singles / count
    if single_ratio >= SINGLE_CHAR_TOKEN_LIMIT:
        return Verdict(
            False,
            f"{single_ratio:.0%} of tokens are isolated single letters (limit "
            f"{SINGLE_CHAR_TOKEN_LIMIT:.0%}): this is a failed OCR pass, not "
            f"text — re-extract the source rather than embedding this",
            count,
            fraction,
            distinct_ratio,
        )

    if fraction >= numeric_limit:
        return Verdict(
            False,
            f"{fraction:.0%} of tokens are numeric (limit {numeric_limit:.0%}): this "
            f"is measurement data, which belongs in the signals tables rather than "
            f"a vector index",
            count,
            fraction,
            distinct_ratio,
        )
    if distinct_ratio <= distinct_limit:
        return Verdict(
            False,
            f"only {distinct_ratio:.0%} of tokens are distinct (limit "
            f"{distinct_limit:.0%}): a repeated reading carries no retrievable "
            f"distinction",
            count,
            fraction,
            distinct_ratio,
        )
    return Verdict(True, "", count, fraction, distinct_ratio)


@dataclass
class DedupFilter:
    """Refuses a chunk whose checksum has already been accepted.

    The `checksum` column needed to prevent 1,036,802 copies of one chunk
    already existed on the table and was never consulted.

    It reports what it stopped. A filter that silently drops is
    indistinguishable from a bug, and the first question anyone asks of a short
    corpus is whether ingestion lost something.
    """

    seen: int = 0
    rejected: int = 0
    _checksums: set[str] = field(default_factory=set, repr=False)

    def accept(self, checksum: str) -> bool:
        self.seen += 1
        if checksum in self._checksums:
            self.rejected += 1
            return False
        self._checksums.add(checksum)
        return True

    def summary(self) -> str:
        kept = self.seen - self.rejected
        return (
            f"{self.seen} chunks offered, {kept} kept, {self.rejected} refused "
            f"as duplicates of a chunk already stored"
        )


#: Set to "0" to ingest exactly as before the guard existed. An operator who
#: needs a blocked corpus in tonight has to have a switch; one that only
#: half-disables the guard would be worse than none.
GUARD_ENV = "AXIOM_RAG_INGEST_GUARD"


def guard_enabled() -> bool:
    """Is the ingestion guard on? On unless explicitly disabled."""
    return os.environ.get(GUARD_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


@dataclass
class ScreenReport:
    """What the guard admitted and what it stopped, by reason.

    Every number here is meant to be printed. The corpus this guard exists to
    prevent was 98.25% duplicate, and nobody noticed for months because
    ingestion reported only what it had added.
    """

    offered: int = 0
    kept: int = 0
    refused_unsuitable: int = 0
    refused_duplicate: int = 0
    by_reason: dict[str, int] = field(default_factory=dict)

    @property
    def refused(self) -> int:
        return self.refused_unsuitable + self.refused_duplicate

    def summary(self) -> str:
        """One line, or empty when nothing was offered."""
        if not self.offered:
            return ""
        parts = [f"{self.offered} offered", f"{self.kept} kept"]
        if self.refused_unsuitable:
            worst = ", ".join(
                f"{k}={n}" for k, n in sorted(self.by_reason.items(), key=lambda kv: -kv[1])
            )
            parts.append(f"{self.refused_unsuitable} unsuitable ({worst})")
        if self.refused_duplicate:
            parts.append(f"{self.refused_duplicate} duplicate of a chunk already offered")
        return ", ".join(parts)


def _reason_key(reason: str) -> str:
    for key, needle in (
        ("ocr", "single letters"),
        ("numeric", "numeric"),
        ("repeated-reading", "distinct"),
    ):
        if needle in reason:
            return key
    return "too-short"


def screen_chunks(
    chunks: Sequence[Any] | Iterable[Any],
    *,
    enabled: bool | None = None,
    text_of: Callable[[Any], str] = lambda c: c.text,
) -> tuple[list[Any], ScreenReport]:
    """Filter chunks before they are embedded, and say what was dropped.

    Placed **before** embedding on purpose: a refused chunk should cost no
    embedding call, and on the corpus that motivated this that is 5.2 million
    calls not made.

    *enabled* defaults to :func:`guard_enabled`. Passing ``False`` returns the
    input unchanged with a report that claims nothing, so a disabled guard is
    exactly the old behaviour rather than a quieter new one.
    """
    chunks = list(chunks)
    report = ScreenReport(offered=len(chunks))
    if enabled is None:
        enabled = guard_enabled()
    if not enabled:
        report.kept = len(chunks)
        return chunks, report

    dedup = DedupFilter()
    kept: list[Any] = []
    for chunk in chunks:
        body = text_of(chunk)
        verdict = assess_chunk(body)
        if not verdict.suitable:
            report.refused_unsuitable += 1
            key = _reason_key(verdict.reason)
            report.by_reason[key] = report.by_reason.get(key, 0) + 1
            continue
        digest = hashlib.md5(" ".join(body.split()).encode("utf-8")).hexdigest()
        if not dedup.accept(digest):
            report.refused_duplicate += 1
            continue
        kept.append(chunk)
    report.kept = len(kept)
    return kept, report


# --- the same rules, as SQL ---------------------------------------------------
#
# A reaper removing chunks already stored and a guard keeping new ones out must
# apply identical rules, or the corpus converges on whichever is laxer. Rather
# than maintain the thresholds twice, the SQL is generated from the constants
# above. Editing a limit changes both faces or neither.

#: Token split, kept identical to ``assess_chunk``'s ``str.split()``.
_SQL_TOKENS = r"regexp_split_to_array(btrim(chunk_text), '\s+')"

#: SQL literal for the characters ``assess_chunk`` strips before testing a
#: token's length: ``. , ; : ( ) [ ] { } " '``. The embedded single quote is
#: doubled, as SQL requires.
_SQL_TRIM = "'.,;:()[]{}\"'''"


def _sql_measures() -> str:
    """SQL fragment defining n, numeric_n, distinct_n and single_n.

    Intended for a CTE: ``SELECT id, source_path, <measures> FROM chunks``.
    """
    return f"""
      array_length({_SQL_TOKENS}, 1) AS n,
      (SELECT count(*) FROM unnest({_SQL_TOKENS}) tok
         WHERE tok ~ '{_NUMERIC.pattern}') AS numeric_n,
      (SELECT count(DISTINCT tok) FROM unnest({_SQL_TOKENS}) tok) AS distinct_n,
      (SELECT count(*) FROM unnest({_SQL_TOKENS}) tok
         WHERE btrim(tok, {_SQL_TRIM}) ~ '^[[:alpha:]]$') AS single_n"""


def sql_measure_lateral(text_expr: str = "chunk_text") -> str:
    """LATERAL joins computing n, numeric_n, distinct_n and single_n.

    Use as ``FROM <table> c <lateral> WHERE <predicate>``.

    The token array is built **once per row**. The first version computed it
    inside each of the four measures, and a full pass over 5.3 million chunks
    had not finished in 25 minutes — a maintenance query nobody can afford to
    run is a guard that only ever applies to new content.
    """
    return f"""
  LEFT JOIN LATERAL (
    SELECT regexp_split_to_array(btrim({text_expr}), '\\s+') AS toks
  ) tk ON TRUE
  LEFT JOIN LATERAL (
    SELECT array_length(tk.toks, 1) AS n,
           count(*) FILTER (WHERE tok ~ '{_NUMERIC.pattern}') AS numeric_n,
           count(DISTINCT tok) AS distinct_n,
           count(*) FILTER (WHERE btrim(tok, {_SQL_TRIM}) ~ '^[[:alpha:]]$') AS single_n
    FROM unnest(tk.toks) AS tok
  ) mm ON TRUE"""


def unsuitable_predicate_sql() -> str:
    """WHERE-clause predicate selecting chunks ``assess_chunk`` would refuse.

    Assumes the measures from :func:`_sql_measures` are in scope. Parameter
    free: it is interpolated into a maintenance statement, and a placeholder a
    caller could forget to bind has no place there.
    """
    return (
        f"(n IS NULL OR n < {MIN_TOKENS}"
        f" OR single_n::numeric / n >= {SINGLE_CHAR_TOKEN_LIMIT}"
        f" OR numeric_n::numeric / n >= {NUMERIC_FRACTION_LIMIT}"
        f" OR distinct_n::numeric / n <= {DISTINCT_TOKEN_RATIO_LIMIT})"
    )


def unsuitable_reason_sql() -> str:
    """CASE expression naming WHICH rule refused a chunk.

    The arms follow ``assess_chunk``'s order on purpose. A chunk can trip
    several rules, and if the two faces report different ones then the same row
    is "numeric" to the reaper and "ocr" to ingest.
    """
    return (
        f"CASE WHEN n IS NULL OR n < {MIN_TOKENS} THEN 'too-short'"
        f" WHEN single_n::numeric / n >= {SINGLE_CHAR_TOKEN_LIMIT} THEN 'ocr'"
        f" WHEN numeric_n::numeric / n >= {NUMERIC_FRACTION_LIMIT} THEN 'numeric'"
        f" WHEN distinct_n::numeric / n <= {DISTINCT_TOKEN_RATIO_LIMIT} THEN 'repeated-reading'"
        f" ELSE 'suitable' END"
    )


def reap_plan_sql(schema: str = "public", table: str = "chunks") -> str:
    """Counts, by reason, of what a reap would remove. Read-only.

    This is the statement to run and read before anything is deleted.
    """
    return f"""
SELECT {unsuitable_reason_sql()} AS reason,
       count(*) AS chunks,
       count(DISTINCT c.source_path) AS files
FROM {schema}.{table} c{sql_measure_lateral("c.chunk_text")}
WHERE {unsuitable_predicate_sql()}
GROUP BY 1 ORDER BY 2 DESC"""


__all__ = [
    "DISTINCT_TOKEN_RATIO_LIMIT",
    "MIN_TOKENS",
    "SINGLE_CHAR_TOKEN_LIMIT",
    "NUMERIC_FRACTION_LIMIT",
    "GUARD_ENV",
    "DedupFilter",
    "ScreenReport",
    "Verdict",
    "assess_chunk",
    "guard_enabled",
    "numeric_fraction",
    "reap_plan_sql",
    "screen_chunks",
    "sql_measure_lateral",
    "unsuitable_predicate_sql",
    "unsuitable_reason_sql",
]
