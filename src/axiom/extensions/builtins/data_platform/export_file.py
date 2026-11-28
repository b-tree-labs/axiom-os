# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A window of rows written to a file a person can open, with what a citation needs beside it.

One writer for every surface that hands data out: the terminal, an agent, chat, the web app
and the HTTP door all call :func:`write`. A file that leaves the platform has to be able to
say, months later and on another machine:

* where it came from (site, dataset, the filter and the order that were applied);
* what it holds (columns, units where the rows carry them, the first and last time);
* whether it is everything that was asked for (``complete``, and why not when it is not);
* whether it is measured, modelled or both (``source_class``, a list, never collapsed);
* how well each value is known, in one of three kinds and never by silence
  (``uncertainty``): its sources are in a file beside it, or only a magnitude is in the row,
  or nothing reported one. A missing uncertainty is never written as zero;
* and whether it is the same data as last time (``sha256``, over the rows and not the file).

Four formats, one set of rows. ``csv`` opens everywhere and is the default. ``jsonl``
streams. ``parquet`` and ``xlsx`` carry the provenance inside themselves as well, because the
failure this guards against is the file and its sidecar being separated. The sidecar
(``<name>.provenance.json``) is written beside every format, since it is the only thing a
plain CSV can have.

The hash is over what the cells say, not over the file (scheme ``cells-text/1``): each row,
in the order written, as the JSON list of its cells as text, with every number written the
one way a float prints. So the same rows hash the same as csv, jsonl, parquet or xlsx, under
any file name, and one changed value changes it. :func:`verify` recomputes it from a file.

Rows are consumed as they arrive, so ``csv`` and ``jsonl`` never hold the window in memory.
``parquet`` and ``xlsx`` are written whole and refuse a window above :data:`BUFFERED_ROW_MAX`.
"""

from __future__ import annotations

import csv
import datetime as _dt
import decimal
import hashlib
import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: format -> the extension it writes. ``csv`` is first and the default.
FORMATS: dict[str, str] = {"csv": ".csv", "jsonl": ".jsonl", "parquet": ".parquet", "xlsx": ".xlsx"}

#: Formats whose files carry the provenance internally as well as in the sidecar.
EMBEDS_PROVENANCE = frozenset({"parquet", "xlsx"})

#: Formats that can be written row by row. The others are held in memory until written.
STREAMS = frozenset({"csv", "jsonl"})

#: The most rows a format that is written whole will hold. A spreadsheet cannot open more
#: than 1,048,576 rows, and a window this size held as Python objects is a few hundred MB.
BUFFERED_ROW_MAX = 1_000_000

HASH_SCHEME = "cells-text/1"
SIDECAR_SUFFIX = ".provenance.json"
METADATA_KEY = b"axiom.provenance"
SCHEMA = "axiom.export/1"


class ExportError(RuntimeError):
    """Nothing usable was written, and why."""


@dataclass(frozen=True)
class Export:
    data_path: Path
    sidecar_path: Path
    rows: int
    sha256: str
    provenance: dict[str, Any]


def _plain(value: Any) -> Any:
    """A cell as JSON can hold it, the same on every machine."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, _dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_dt.UTC)
            return value.isoformat().replace("+00:00", "Z")
        return value.isoformat()
    if isinstance(value, _dt.date | _dt.time):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_plain(v) for v in value]
    return str(value)


def _line(row: Mapping[str, Any], columns: Sequence[str]) -> list[Any]:
    return [_plain(row.get(c)) for c in columns]


def _said(value: Any) -> str:
    """What a cell says, the same whichever format it was read back from.

    A format without types (csv) cannot tell the number 5 from the text "5", and a
    spreadsheet turns 5.0 into 5. So anything that reads as a number is written the way a
    float prints, nothing is the empty text, and a nested value is its JSON.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, int | float):
        return repr(float(value))
    if isinstance(value, list | dict):
        return json.dumps(value, sort_keys=True)
    text = str(value)
    try:
        number = float(text)
    except ValueError:
        return text
    return repr(number) if number == number and abs(number) != float("inf") else text


def _hash_update(h, cells: list[Any]) -> None:
    h.update(json.dumps([_said(c) for c in cells], separators=(",", ":")).encode("utf-8"))
    h.update(b"\n")


def sidecar_path(data_path: Path | str) -> Path:
    path = Path(data_path)
    return path.with_name(path.stem + SIDECAR_SUFFIX)


def _missing(fmt: str, module: str) -> ExportError:
    return ExportError(
        f"the {fmt} format needs {module}, which is not installed here. Install it, or "
        "export csv: every tool reads csv, and the provenance is the same either way"
    )


def _cell_text(value: Any) -> Any:
    """A cell for a format without nested values: a list or mapping becomes JSON text."""
    return json.dumps(value, sort_keys=True) if isinstance(value, list | dict) else value


def write(
    rows: Iterable[Mapping[str, Any]],
    path: Path | str,
    *,
    columns: Sequence[str],
    fmt: str = "csv",
    site: str = "",
    dataset: str = "",
    request: Mapping[str, Any] | None = None,
    units: Mapping[str, str] | None = None,
    time_column: str | None = None,
    complete: bool = True,
    bounded: Sequence[str] = (),
    exported_by: str = "",
    surface: str = "",
    note: str = "",
    uncertainty_columns: Mapping[str, str] | None = None,
    uncertainty_sources: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
    finish: Callable[[], Mapping[str, Any]] | None = None,
) -> Export:
    """Write ``rows`` as ``fmt`` at ``path`` and the provenance sidecar beside it.

    ``columns`` fixes the order of the file's columns and of the hash. ``request`` is what
    was asked for (the filter, the sort, the window), kept so that the same export can be
    made again. ``complete`` is False when the rows are fewer than what was asked for, and
    ``bounded`` says why. A caller that only learns that while the rows arrive gives ``finish``:
    it is called once every row is written, and what it returns (``complete``, ``bounded``) is
    recorded. ``extra`` is kept as given, for what the source knows about itself.

    ``uncertainty_columns`` maps a value column to the column that holds its uncertainty.
    The provenance then counts, per value column, the rows that state one and the rows that
    do not. With none given, the provenance says that nothing reported an uncertainty.
    ``uncertainty_sources`` describes a file written beside this one that holds the sources of
    those uncertainties (``file``, ``rows``, ``sha256``, ``complete``); with it the kind is
    ``sources_known``.
    """
    if fmt not in FORMATS:
        raise ExportError(f"unknown format {fmt!r}: one of {', '.join(sorted(FORMATS))}")
    columns = [str(c) for c in columns]
    if not columns:
        raise ExportError("an export needs at least one column")
    data_path = Path(path)
    data_path.parent.mkdir(parents=True, exist_ok=True)

    h = hashlib.sha256()
    count = 0
    first_ts: Any = None
    last_ts: Any = None
    classes: set[str] = set()
    seen_units: dict[str, str] = {}
    held: list[list[Any]] = []
    stated = {v: [0, 0] for v in (uncertainty_columns or {})}  # value column -> [stated, not]

    def take(row: Mapping[str, Any]) -> list[Any]:
        nonlocal count, first_ts, last_ts
        cells = _line(row, columns)
        _hash_update(h, cells)
        count += 1
        if time_column and (stamp := _plain(row.get(time_column))) is not None:
            text = str(stamp)
            first_ts = text if first_ts is None or text < first_ts else first_ts
            last_ts = text if last_ts is None or text > last_ts else last_ts
        for value_column, tally in stated.items():
            if row.get(value_column) is None:
                continue
            u = row.get(uncertainty_columns[value_column])
            # Zero is not "exact": a zero or missing uncertainty is one nobody stated.
            tally[0 if isinstance(u, int | float | decimal.Decimal) and u > 0 else 1] += 1
        if row.get("source_class"):
            classes.add(str(row["source_class"]))
        # A long table names its quantity in a row, not a column: channel, then its unit.
        name, unit = row.get("channel"), row.get("unit")
        if name and unit and str(name) not in seen_units:
            seen_units[str(name)] = str(unit)
        return cells

    if fmt == "csv":
        with data_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(columns)
            for row in rows:
                writer.writerow(["" if v is None else _cell_text(v) for v in take(row)])
    elif fmt == "jsonl":
        with data_path.open("w", encoding="utf-8") as fh:
            for row in rows:
                cells = take(row)
                fh.write(json.dumps(dict(zip(columns, cells)), separators=(",", ":")) + "\n")
    else:
        for row in rows:
            if count >= BUFFERED_ROW_MAX:
                raise ExportError(
                    f"{fmt} is written whole and holds at most {BUFFERED_ROW_MAX:,} rows. "
                    "Narrow the window, or export csv or jsonl, which have no such limit"
                )
            held.append(take(row))

    if count == 0:
        data_path.unlink(missing_ok=True)
        raise ExportError(
            "no rows matched, so nothing was written: an empty file with a confident "
            "provenance beside it is a citable claim about data that does not exist"
        )

    provenance: dict[str, Any] = {
        "schema": SCHEMA,
        "site": site,
        "dataset": dataset,
        "request": _plain(dict(request or {})),
        "columns": columns,
        "units": {**seen_units, **{str(k): str(v) for k, v in (units or {}).items()}},
        "rows": count,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "time_column": time_column,
        # A list: a window holding measured and predicted rows says so.
        "source_class": sorted(classes),
        "complete": bool(complete),
        "bounded": [str(b) for b in bounded],
        "sha256": h.hexdigest(),
        "hash_scheme": HASH_SCHEME,
        "format": fmt,
        "exported_at": _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "exported_by": exported_by,
        "surface": surface,
    }
    if note:
        provenance["note"] = note
    if extra:
        provenance.update({str(k): _plain(v) for k, v in extra.items() if str(k) not in provenance})
    if finish is not None:
        late = finish()
        provenance["complete"] = bool(late.get("complete", provenance["complete"]))
        provenance["bounded"] = [str(b) for b in late.get("bounded", provenance["bounded"])]
        # A caller that can only find the sources once it has seen the rows says so here.
        uncertainty_sources = late.get("uncertainty_sources", uncertainty_sources)
    provenance["uncertainty"] = _uncertainty_said(
        uncertainty_columns or {}, stated, uncertainty_sources
    )

    if fmt == "parquet":
        _write_parquet(held, columns, data_path, provenance)
    elif fmt == "xlsx":
        _write_xlsx(held, columns, data_path, provenance)

    side = sidecar_path(data_path)
    side.write_text(json.dumps(provenance, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return Export(data_path, side, count, provenance["sha256"], provenance)


#: The three kinds an uncertainty is reported in (never zero for unknown).
SOURCES_KNOWN, MAGNITUDE_ONLY, NONE_REPORTED = "sources_known", "magnitude_only", "none_reported"


def _uncertainty_said(
    columns: Mapping[str, str],
    stated: Mapping[str, list[int]],
    sources: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """How well the file's values are known, as the file itself can say it."""
    per = {
        value: {"from": columns[value], "stated": n[0], "not_stated": n[1]}
        for value, n in stated.items()
    }
    if sources and sources.get("rows"):
        return {
            "kind": SOURCES_KNOWN,
            "columns": per,
            "sources_file": str(sources.get("file") or ""),
            "sources_rows": int(sources["rows"]),
            "sources_sha256": str(sources.get("sha256") or ""),
            "sources_complete": bool(sources.get("complete", True)),
            "sources_qualified_by": sources.get("origin"),
            "note": (
                "the sources of these uncertainties are in the file named here: one row per "
                "source of one value, with its symbol and coefficient. Two values that share "
                "a symbol share that source, which is what a mean or a difference has to "
                "account for. A value with no row there has a magnitude at most"
                + ("" if sources.get("origin") else
                   ". The symbols are not qualified by a site, so do not combine this file "
                   "with another site's")
            ),
        }
    if any(v["stated"] for v in per.values()):
        return {
            "kind": MAGNITUDE_ONLY,
            "columns": per,
            "sources_file": None,
            "note": (
                "each row carries a magnitude and not what it comes from. Rows that share a "
                "source (one instrument's calibration) are not independent, so a mean or a "
                "difference computed from this file as if they were is too confident. Ask "
                "the platform for the combined figure, or export the sources"
            ),
        }
    return {
        "kind": NONE_REPORTED,
        "columns": per,
        "sources_file": None,
        "note": (
            "nothing reported an uncertainty for these values. That is not zero: how well "
            "they are known is not stated"
        ),
    }


def _write_parquet(held: list[list[Any]], columns: list[str], path: Path, provenance: dict) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        raise _missing("parquet", "pyarrow") from None

    arrays = {}
    for i, name in enumerate(columns):
        values = [_cell_text(r[i]) for r in held]
        kinds = {type(v) for v in values if v is not None}
        if kinds <= {int, float} and kinds:
            # A reading voided to NULL stays null: it must not become a zero on the way out.
            arrays[name] = pa.array([None if v is None else float(v) for v in values], pa.float64())
        elif kinds == {bool}:
            arrays[name] = pa.array(values, pa.bool_())
        else:
            arrays[name] = pa.array([None if v is None else str(v) for v in values], pa.string())
    table = pa.table(arrays).replace_schema_metadata(
        {METADATA_KEY: json.dumps(provenance, sort_keys=True).encode("utf-8")}
    )
    pq.write_table(table, path)


def _write_xlsx(held: list[list[Any]], columns: list[str], path: Path, provenance: dict) -> None:
    try:
        from openpyxl import Workbook
    except ImportError:
        raise _missing("xlsx", "openpyxl") from None

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("rows")
    ws.append(columns)
    for cells in held:
        ws.append([_cell_text(v) for v in cells])
    meta = wb.create_sheet("provenance")
    meta.append(["key", "value"])
    for key in sorted(provenance):
        value = provenance[key]
        meta.append([key, json.dumps(value, sort_keys=True) if isinstance(value, dict | list) else str(value)])
    wb.save(path)


# ---- reading an export back -----------------------------------------------------------------


def read_provenance(data_path: Path | str) -> dict[str, Any]:
    """The provenance of an export: from its sidecar, else from inside a parquet file."""
    path = Path(data_path)
    side = sidecar_path(path)
    if side.is_file():
        return json.loads(side.read_text(encoding="utf-8"))
    if path.suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise _missing("parquet", "pyarrow") from None
        raw = (pq.read_schema(path).metadata or {}).get(METADATA_KEY)
        if raw:
            return json.loads(raw)
    raise ExportError(
        f"{path.name} has no provenance: {side.name} is not beside it. Without it nobody "
        "can say which site the rows came from or whether they were measured"
    )


def read_rows(data_path: Path | str) -> Iterator[dict[str, Any]]:
    """The rows of an export, one mapping per row, in the order written."""
    path = Path(data_path)
    suffix = path.suffix
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader, None) or []
            for cells in reader:
                yield {c: (None if v == "" else v) for c, v in zip(header, cells)}
    elif suffix == ".jsonl":
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)
    elif suffix == ".parquet":
        try:
            import pyarrow.parquet as pq
        except ImportError:
            raise _missing("parquet", "pyarrow") from None
        for batch in pq.ParquetFile(path).iter_batches():
            yield from batch.to_pylist()
    elif suffix == ".xlsx":
        try:
            from openpyxl import load_workbook
        except ImportError:
            raise _missing("xlsx", "openpyxl") from None
        ws = load_workbook(path, read_only=True)["rows"]
        it = ws.iter_rows(values_only=True)
        header = [str(c) for c in next(it, ())]
        for cells in it:
            yield dict(zip(header, cells))
    else:
        raise ExportError(f"{path.name}: not an export format ({', '.join(sorted(FORMATS))})")


def digest(rows: Iterable[Mapping[str, Any]], columns: Sequence[str]) -> tuple[str, int]:
    """The hash of some rows under ``columns``, and how many there were."""
    h = hashlib.sha256()
    count = 0
    for row in rows:
        _hash_update(h, _line(row, columns))
        count += 1
    return h.hexdigest(), count


def verify(data_path: Path | str) -> tuple[bool, str]:
    """Whether a file still holds the rows its provenance describes, and a sentence saying so."""
    path = Path(data_path)
    provenance = read_provenance(path)
    found, count = digest(read_rows(path), provenance.get("columns") or [])
    if count != provenance.get("rows"):
        return False, f"{path.name} holds {count} rows; its provenance says {provenance.get('rows')}"
    if found != provenance.get("sha256"):
        return False, f"{path.name} does not hold the rows its provenance describes: a value differs"
    return True, f"{path.name} holds the {count} rows its provenance describes"


__all__ = [
    "BUFFERED_ROW_MAX",
    "EMBEDS_PROVENANCE",
    "FORMATS",
    "MAGNITUDE_ONLY",
    "NONE_REPORTED",
    "SOURCES_KNOWN",
    "Export",
    "ExportError",
    "digest",
    "read_provenance",
    "read_rows",
    "sidecar_path",
    "verify",
    "write",
]
