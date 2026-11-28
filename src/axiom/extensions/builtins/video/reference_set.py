# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A reference set: what ``reference-match`` compares a frame with (spec-video §Reference set).

A directory the consumer builds, named by an absolute path: :data:`MANIFEST`
(format :data:`FORMAT`) and the pictures it names, side by side. The manifest gives
a name and a version, the working size, masks for text burned into the picture,
the landmarks (the parts of the view that never move) with their picture, one or
more regions with two or more labelled references each, and the guard limits.
Every picture is a 2-D ``.npy`` array of BT.601 luma of its box at the working
size, loaded without pickle and checked against the SHA-256 the manifest records.

The platform reads a set and never writes one it is given;
:func:`write_reference_set` is for a consumer's builder, and fills in the hashes.
Nothing here knows what a picture shows: every label is the set's, and only
:data:`RESERVED_LABELS` belong to the trigger. numpy is imported when a set is
loaded or written, not at import.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .frame import require_numpy
from .triggers import TriggerConfigError

#: The format this version reads and writes. A change to the format is a new string.
FORMAT = "reference-set/1"
#: The manifest's file name inside a set's directory.
MANIFEST = "reference-set.json"
#: Labels the trigger reports itself, so no set may declare them.
RESERVED_LABELS = ("changing", "unknown")

#: ``[x0, y0, x1, y1]``, fractions of the frame.
Box = tuple[float, float, float, float]
#: A region's name and a reference's label: lowercase letters, digits, ``-`` and ``_``.
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
#: A side whose standard deviation is below this many luma levels has no contrast.
FLAT_STD = 0.5

_TOP = {"format", "name", "version", "working_size", "masks", "landmarks", "regions", "guards"}
_TOP_OPTIONAL = {"masks", "provenance"}
_LANDMARKS = {"bounds", "exclude", "image", "sha256"}
_REGION = {"name", "bounds", "exclude", "margin", "references"}
_REFERENCE = {"label", "image", "sha256", "min_match"}
_GUARDS = {"view", "exposure", "clipped"}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReferenceSetError(TriggerConfigError):
    """A reference set that cannot be used as written. The message names the field or the
    file; ``fix`` says what to change."""


def box_pixels(box: Box, width: int, height: int) -> tuple[slice, slice]:
    """``(rows, cols)`` of ``box`` in an image of ``width`` by ``height``: Python's ``round``
    of each edge times the side."""
    x0, y0, x1, y1 = box
    return slice(round(y0 * height), round(y1 * height)), slice(
        round(x0 * width), round(x1 * width)
    )


def visible(
    box: Box, exclude: tuple[Box, ...], masks: tuple[Box, ...], width: int, height: int
) -> Any:
    """Which pixels of ``box``'s crop are compared: the box without its excludes and the masks,
    as a boolean array the crop's shape."""
    np = require_numpy()
    full = np.zeros((height, width), dtype=bool)
    rows, cols = box_pixels(box, width, height)
    full[rows, cols] = True
    for other in (*exclude, *masks):
        r, c = box_pixels(other, width, height)
        full[r, c] = False
    return full[rows, cols]


@dataclass(frozen=True)
class Picture:
    """One picture of the set: its file, its hash, and its luma as ``float32``."""

    file: str
    sha256: str
    array: Any = field(repr=False, compare=False)


@dataclass(frozen=True)
class Reference:
    """A label and the picture of its region that reads it, with the least match it needs."""

    label: str
    min_match: float
    picture: Picture


@dataclass(frozen=True)
class Region:
    """A part of the view, read as one of its references' labels or none."""

    name: str
    box: Box
    exclude: tuple[Box, ...]
    margin: float
    references: tuple[Reference, ...]

    @property
    def labels(self) -> tuple[str, ...]:
        return tuple(r.label for r in self.references)


@dataclass(frozen=True)
class Landmarks:
    """The parts of the view that never move, and their picture: the view guard."""

    box: Box
    exclude: tuple[Box, ...]
    picture: Picture


@dataclass(frozen=True)
class Guards:
    """``view``: the least landmark match; ``exposure``: the mean luma allowed, ``(low,
    high)``; ``clipped``: the largest fraction of pixels at 2 or below or 253 or above."""

    view: float
    exposure: tuple[float, float]
    clipped: float


@dataclass(frozen=True)
class ReferenceSet:
    """A loaded, checked reference set."""

    path: Path
    name: str
    version: str
    working_size: tuple[int, int]
    masks: tuple[Box, ...]
    landmarks: Landmarks
    regions: tuple[Region, ...]
    guards: Guards
    provenance: Mapping[str, Any] = field(default_factory=dict)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class _Reader:
    """Reads one manifest, naming the field and the file in every refusal."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.manifest = path / MANIFEST
        self.np = require_numpy()
        self.width = self.height = 0
        self.masks: tuple[Box, ...] = ()

    def fail(self, where: str, problem: str, fix: str | None = None) -> ReferenceSetError:
        return ReferenceSetError(
            f"reference set {self.path}: {where} {problem}",
            fix=fix
            or f"correct {where} in {self.manifest}; spec-video §Reference set has the format",
        )

    # ---- shapes --------------------------------------------------------------------

    def table(self, value: Any, where: str, keys: set[str], optional: set[str]) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise self.fail(where, f"is {type(value).__name__}, not an object")
        unknown = sorted(set(value) - keys - optional)
        if unknown:
            raise self.fail(
                where,
                f"has {', '.join(map(repr, unknown))}, which the format has not "
                f"(it has {', '.join(sorted(keys | optional))})",
                fix=f"remove or correct {', '.join(unknown)} in {self.manifest}",
            )
        missing = sorted(keys - optional - set(value))
        if missing:
            raise self.fail(where, f"has no {', '.join(missing)}")
        return dict(value)

    def text(self, value: Any, where: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self.fail(where, f"is {value!r}; it is a non-empty string")
        return value

    def name(self, value: Any, where: str) -> str:
        if not isinstance(value, str) or not NAME.match(value):
            raise self.fail(
                where,
                f"{value!r} is not a name: lowercase letters, digits, - and _, starting with a "
                "letter or digit",
            )
        return value

    def number(self, value: Any, where: str, lo: float, hi: float) -> float:
        if not _is_number(value) or not lo <= value <= hi:
            raise self.fail(where, f"is {value!r}; it is a number from {lo:g} to {hi:g}")
        return float(value)

    def box(self, value: Any, where: str) -> Box:
        if (
            not isinstance(value, (list, tuple))
            or len(value) != 4
            or not all(_is_number(v) for v in value)
        ):
            raise self.fail(
                where, f"is {value!r}; a box is [x0, y0, x1, y1], fractions of the frame"
            )
        x0, y0, x1, y1 = (float(v) for v in value)
        if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
            raise self.fail(
                where,
                f"{list(value)} is empty or outside the frame: 0 <= x0 < x1 <= 1, the same for y",
            )
        rows, cols = box_pixels((x0, y0, x1, y1), self.width, self.height)
        if rows.stop <= rows.start or cols.stop <= cols.start:
            raise self.fail(
                where, f"{list(value)} has no pixels at the working size {self.width}x{self.height}"
            )
        return (x0, y0, x1, y1)

    def boxes(self, value: Any, where: str) -> tuple[Box, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise self.fail(where, f"is {value!r}; it is a list of boxes")
        return tuple(self.box(v, f"{where}[{i}]") for i, v in enumerate(value))

    # ---- pictures ---------------------------------------------------------------------

    def picture(
        self, table: Mapping[str, Any], where: str, box: Box, exclude: tuple[Box, ...]
    ) -> Picture:
        np = self.np
        file = table.get("image")
        if not isinstance(file, str) or not file or Path(file).name != file or file in (".", ".."):
            raise self.fail(
                f"{where} image",
                f"{file!r} is not a file name in the set's directory",
                fix=f"put the picture in {self.path} and give its file name alone",
            )
        if not file.endswith(".npy"):
            raise self.fail(f"{where} image", f"{file!r} is not a .npy file")
        digest = table.get("sha256")
        if not isinstance(digest, str) or not _SHA256.match(digest):
            raise self.fail(f"{where} sha256", f"is {digest!r}; it is the picture's hex SHA-256")
        source = self.path / file
        try:
            raw = source.read_bytes()
        except OSError as exc:
            raise self.fail(
                f"{where} image",
                f"{file} cannot be read: {exc.strerror or exc}",
                fix=f"put {file} in {self.path}, or rebuild the set",
            ) from None
        if hashlib.sha256(raw).hexdigest() != digest:
            raise self.fail(
                f"{where} image",
                f"{file} does not match its sha256 in {MANIFEST}: the picture changed since the set "
                "was built",
                fix=f"rebuild the set, or restore the {file} it was built with",
            )
        try:
            array = np.load(io.BytesIO(raw), allow_pickle=False)
        except Exception as exc:  # noqa: BLE001 - any unreadable picture is refused the same way
            raise self.fail(
                f"{where} image",
                f"{file} is not a numpy array that loads without pickle: {exc}",
                fix=f"save {file} with numpy.save(..., allow_pickle=False), as a 2-D array of luma",
            ) from None
        if not isinstance(array, np.ndarray) or array.dtype.kind not in "iuf" or array.ndim != 2:
            raise self.fail(
                f"{where} image",
                f"{file} is not a 2-D array of real numbers (it is {getattr(array, 'dtype', '?')} "
                f"{getattr(array, 'shape', '?')})",
            )
        rows, cols = box_pixels(box, self.width, self.height)
        expected = (rows.stop - rows.start, cols.stop - cols.start)
        if tuple(array.shape) != expected:
            raise self.fail(
                f"{where} image",
                f"{file} is {tuple(array.shape)}, but its box at the working size "
                f"{self.width}x{self.height} is {expected} (rows, columns)",
                fix=f"take {file} at the working size {self.width}x{self.height}, cropped to its box",
            )
        values = array.astype(np.float32)
        if not np.all(np.isfinite(values)):
            raise self.fail(f"{where} image", f"{file} holds a value that is not finite")
        seen = visible(box, exclude, self.masks, self.width, self.height)
        if not seen.any():
            raise self.fail(
                where, "has no pixels left to compare once its excludes and the masks are left out"
            )
        if float(values[seen].astype(np.float64).std()) < FLAT_STD:
            raise self.fail(
                f"{where} image",
                f"{file} is flat (no contrast where it is compared), so nothing can match it",
                fix=f"take {file} where its box shows something, or move the box",
            )
        return Picture(file=file, sha256=digest, array=values)

    # ---- the set -----------------------------------------------------------------------

    def read(self) -> ReferenceSet:
        if not self.path.is_dir():
            raise ReferenceSetError(
                f"reference set {self.path} does not exist or is not a directory",
                fix=f"build the set there, or point reference_set at the directory that holds {MANIFEST}",
            )
        try:
            text = self.manifest.read_text(encoding="utf-8")
        except OSError:
            raise ReferenceSetError(
                f"reference set {self.path} has no {MANIFEST}",
                fix=f"point reference_set at the directory that holds {MANIFEST}",
            ) from None
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise self.fail(MANIFEST, f"is not JSON: {exc}") from None
        top = self.table(data, MANIFEST, _TOP | {"provenance"}, _TOP_OPTIONAL)
        if top["format"] != FORMAT:
            raise self.fail(
                "format",
                f"is {top['format']!r}; this version reads {FORMAT}",
                fix=f"rebuild the set in format {FORMAT}, or upgrade to a version that reads "
                f"{top['format']!r}",
            )
        name = self.text(top["name"], "name")
        version = self.text(top["version"], "version")
        size = top["working_size"]
        if (
            not isinstance(size, (list, tuple))
            or len(size) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in size)
        ):
            raise self.fail("working_size", f"is {size!r}; it is [width, height] in whole pixels")
        self.width, self.height = int(size[0]), int(size[1])
        self.masks = self.boxes(top.get("masks"), "masks")
        lm = self.table(top["landmarks"], "landmarks", _LANDMARKS, {"exclude"})
        lm_box = self.box(lm["bounds"], "landmarks bounds")
        lm_exclude = self.boxes(lm.get("exclude"), "landmarks exclude")
        landmarks = Landmarks(lm_box, lm_exclude, self.picture(lm, "landmarks", lm_box, lm_exclude))
        regions = self.regions(top["regions"])
        guards = self.guards(top["guards"])
        provenance = top.get("provenance") or {}
        if not isinstance(provenance, Mapping):
            raise self.fail("provenance", f"is {type(provenance).__name__}, not an object")
        return ReferenceSet(
            path=self.path,
            name=name,
            version=version,
            working_size=(self.width, self.height),
            masks=self.masks,
            landmarks=landmarks,
            regions=regions,
            guards=guards,
            provenance=dict(provenance),
        )

    def regions(self, value: Any) -> tuple[Region, ...]:
        if not isinstance(value, (list, tuple)) or not value:
            raise self.fail("regions", f"is {value!r}; a set declares one or more regions")
        out: list[Region] = []
        for i, raw in enumerate(value):
            where = f"regions[{i}]"
            table = self.table(raw, where, _REGION, {"exclude"})
            name = self.name(table["name"], f"{where} name")
            where = f"regions[{i}] ({name})"
            if name in {r.name for r in out}:
                raise self.fail(
                    f"{where} name", f"{name!r} is declared twice; each region has its own"
                )
            box = self.box(table["bounds"], f"{where} bounds")
            exclude = self.boxes(table.get("exclude"), f"{where} exclude")
            margin = self.number(table["margin"], f"{where} margin", 0.0, 2.0)
            refs = table["references"]
            if not isinstance(refs, (list, tuple)) or len(refs) < 2:
                raise self.fail(
                    f"{where} references",
                    "must list two or more references: one picture can only be matched or not, "
                    "and a region reads which of its references it matches",
                    fix=f"add a reference for each label {name} can read",
                )
            references: list[Reference] = []
            for j, ref_raw in enumerate(refs):
                ref_where = f"{where} references[{j}]"
                ref = self.table(ref_raw, ref_where, _REFERENCE, set())
                label = self.name(ref["label"], f"{ref_where} label")
                if label in RESERVED_LABELS:
                    raise self.fail(
                        f"{ref_where} label",
                        f"{label!r} is reserved: the trigger reports {' and '.join(RESERVED_LABELS)} "
                        "itself",
                        fix=f"call the reference something other than {', '.join(RESERVED_LABELS)}",
                    )
                if label in {r.label for r in references}:
                    raise self.fail(
                        f"{ref_where} label",
                        f"{label!r} is declared twice in {name}; each reference reads its own label",
                        fix=f"give each reference of {name} its own label",
                    )
                references.append(
                    Reference(
                        label=label,
                        min_match=self.number(
                            ref["min_match"], f"{ref_where} min_match", -1.0, 1.0
                        ),
                        picture=self.picture(ref, ref_where, box, exclude),
                    )
                )
            out.append(Region(name, box, exclude, margin, tuple(references)))
        return tuple(out)

    def guards(self, value: Any) -> Guards:
        table = self.table(value, "guards", _GUARDS, set())
        view = self.number(table["view"], "guards view", -1.0, 1.0)
        exposure = table["exposure"]
        if (
            not isinstance(exposure, (list, tuple))
            or len(exposure) != 2
            or not all(_is_number(v) for v in exposure)
            or not 0.0 <= exposure[0] < exposure[1] <= 255.0
        ):
            raise self.fail(
                "guards exposure",
                f"is {exposure!r}; it is [low, high] mean luma, 0 <= low < high <= 255",
            )
        clipped = self.number(table["clipped"], "guards clipped", 0.0, 1.0)
        return Guards(view=view, exposure=(float(exposure[0]), float(exposure[1])), clipped=clipped)


def load_reference_set(path: str | Path) -> ReferenceSet:
    """Read and check the set in directory ``path``; :class:`ReferenceSetError` with a fix if
    it cannot work as written."""
    return _Reader(Path(path)).read()


def write_reference_set(
    directory: str | Path, manifest: Mapping[str, Any], images: Mapping[str, Any]
) -> Path:
    """Write ``images`` (file name to 2-D luma array) as ``.npy`` into ``directory`` and the
    manifest beside them with each picture's ``sha256`` filled in; the manifest's path.

    The set is loaded back before this returns, so a set that cannot work is refused here,
    with the same :class:`ReferenceSetError` the trigger would give.
    """
    np = require_numpy()
    folder = Path(directory)
    folder.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for file, image in images.items():
        if Path(file).name != file or not file.endswith(".npy"):
            raise ReferenceSetError(
                f"picture {file!r} is not a .npy file name",
                fix="name each picture by its file name alone, ending in .npy",
            )
        buffer = io.BytesIO()
        np.save(buffer, np.asarray(image, dtype=np.float32), allow_pickle=False)
        raw = buffer.getvalue()
        (folder / file).write_bytes(raw)
        hashes[file] = hashlib.sha256(raw).hexdigest()
    body = copy.deepcopy(dict(manifest))
    body.setdefault("format", FORMAT)
    named = [body.get("landmarks")]
    for region in body.get("regions") or []:
        named += list(region.get("references") or []) if isinstance(region, Mapping) else []
    for table in named:
        if isinstance(table, dict) and table.get("image") in hashes:
            table["sha256"] = hashes[table["image"]]
    path = folder / MANIFEST
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    load_reference_set(folder)
    return path


__all__ = [
    "Box",
    "FLAT_STD",
    "FORMAT",
    "Guards",
    "Landmarks",
    "MANIFEST",
    "NAME",
    "Picture",
    "RESERVED_LABELS",
    "Reference",
    "ReferenceSet",
    "ReferenceSetError",
    "Region",
    "box_pixels",
    "load_reference_set",
    "visible",
    "write_reference_set",
]
