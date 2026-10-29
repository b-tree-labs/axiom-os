# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A 3D scene for a chart: geometry, scalars, and what the reader did to it.

``data.scene`` is one of

- ``mesh``: ``{points: [[x,y,z],...], cells: [[i,j,k,...],...]}``, polygons;
- ``points``: ``[[x,y,z],...]``, for a 3D scatter;
- ``grid``: ``{x: [...], y: [...], z: [[...]]}`` for a surface (z over x,y), or
  ``{x, y, z, values}`` with ``values`` flat (x fastest) for a volume;
- ``ref``: a data reference a figure source resolves into one of the above, for
  a mesh too large to carry inline;

plus ``scalars`` (``{name: {unit, association: cell|point, values}}``), an
optional ``elements`` list (one element id per cell, what a pick reports and
what an exploded view moves apart), and ``length_unit``.

The operations here are geometry only and deterministic: :func:`surface_mesh`,
:func:`volume_slice`, :func:`cut` (cutaway at a slice plane, or clip planes),
:func:`explode` and :func:`project` (an orthographic camera preset to 2D, with a
painter's order). They serve the static fallback the server draws (exports,
chat thumbnails, a browser with no WebGL) and nothing in the browser depends on
them: vtk.js reads the same document.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: Inline geometry larger than this belongs behind a ``ref``.
INLINE_POINTS_MAX = 250_000
INLINE_CELLS_MAX = 250_000

#: Camera presets: (azimuth, elevation) in degrees. ``iso`` looks down at 30
#: degrees from a corner, the view an engineering drawing uses.
CAMERAS = {"iso": (45.0, 30.0), "top": (0.0, 90.0), "side": (0.0, 0.0), "front": (90.0, 0.0)}


class SceneError(ValueError):
    """A scene that cannot be read, and why."""


@dataclass
class Scene:
    points: list[tuple[float, float, float]] = field(default_factory=list)
    cells: list[tuple[int, ...]] = field(default_factory=list)
    #: name -> (unit, association, values)
    scalars: dict[str, tuple[str, str, list[float | None]]] = field(default_factory=dict)
    elements: list[str] | None = None
    #: cells are markers (a scatter), not polygons
    markers: bool = False
    length_unit: str = ""


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def check(raw: Any) -> list[str]:
    """Every problem with a ``data.scene`` block."""
    if not isinstance(raw, Mapping):
        return ["data.scene: must be a table"]
    forms = [k for k in ("mesh", "points", "grid", "ref") if k in raw]
    if len(forms) != 1:
        return ["data.scene: exactly one of mesh, points, grid or ref"]
    problems: list[str] = []
    extra = sorted(set(raw) - {"mesh", "points", "grid", "ref", "scalars", "elements", "length_unit", "name"})
    if extra:
        problems.append(f"data.scene: unknown key(s) {', '.join(extra)}")
    if "ref" in raw:
        if not isinstance(raw["ref"], str) or not raw["ref"]:
            problems.append("data.scene.ref: a data reference")
        return problems
    try:
        scene = read(raw)
    except SceneError as exc:
        return [*problems, f"data.scene: {exc}"]
    if len(scene.points) > INLINE_POINTS_MAX or len(scene.cells) > INLINE_CELLS_MAX:
        problems.append(f"data.scene: {len(scene.points)} points and {len(scene.cells)} cells is past the inline "
                        f"bound ({INLINE_POINTS_MAX}); carry it as a ref")
    return problems


def read(raw: Mapping[str, Any]) -> Scene:
    scene = Scene(length_unit=str(raw.get("length_unit") or ""))
    if "mesh" in raw:
        mesh = raw["mesh"] or {}
        scene.points = _points(mesh.get("points"))
        cells = mesh.get("cells") or []
        for i, c in enumerate(cells):
            if not isinstance(c, Sequence) or len(c) < 3:
                raise SceneError(f"mesh.cells[{i}] is not a polygon of three or more point indexes")
            idx = tuple(int(v) for v in c)
            if any(not 0 <= v < len(scene.points) for v in idx):
                raise SceneError(f"mesh.cells[{i}] names a point that is not there")
            scene.cells.append(idx)
    elif "points" in raw:
        scene.points = _points(raw["points"])
        scene.cells = [(i,) for i in range(len(scene.points))]
        scene.markers = True
    elif "grid" in raw:
        grid = raw["grid"] or {}
        if "values" in grid:
            scene = volume_slice(grid, axis="z", at=None, length_unit=scene.length_unit)
        else:
            scene = surface_mesh(grid, length_unit=scene.length_unit)
    for name, s in (raw.get("scalars") or {}).items():
        if not isinstance(s, Mapping):
            raise SceneError(f"scalars.{name} must be a table")
        assoc = s.get("association", "cell")
        if assoc not in ("cell", "point"):
            raise SceneError(f"scalars.{name}.association is cell or point")
        values = [_num(v) for v in s.get("values") or []]
        want = len(scene.cells) if assoc == "cell" else len(scene.points)
        if "grid" in raw and "values" in (raw["grid"] or {}):
            continue  # a volume's scalar is its grid
        if len(values) != want:
            raise SceneError(f"scalars.{name} has {len(values)} values for {want} {assoc}s")
        scene.scalars[name] = (str(s.get("unit") or ""), assoc, values)
    if "grid" in raw and not raw.get("scalars") and "values" not in (raw["grid"] or {}):
        # A surface is coloured by its height unless told otherwise; the grid
        # may name that height and give its unit.
        grid = raw["grid"] or {}
        scene.scalars[str(grid.get("name") or "z")] = (str(grid.get("unit") or ""), "point",
                                                       [p[2] for p in scene.points])
    els = raw.get("elements")
    if els is not None:
        if len(els) != len(scene.cells):
            raise SceneError(f"elements has {len(els)} ids for {len(scene.cells)} cells")
        scene.elements = [str(e) for e in els]
    return scene


def _points(raw: Any) -> list[tuple[float, float, float]]:
    if not isinstance(raw, Sequence) or not raw:
        raise SceneError("points: a non-empty list of [x, y, z]")
    out = []
    for i, p in enumerate(raw):
        if not isinstance(p, Sequence) or len(p) != 3 or any(_num(v) is None for v in p):
            raise SceneError(f"points[{i}] is not [x, y, z] in numbers")
        out.append((float(p[0]), float(p[1]), float(p[2])))
    return out


def surface_mesh(grid: Mapping[str, Any], *, length_unit: str = "") -> Scene:
    """``z`` over ``x``, ``y`` as quads (nulls leave holes)."""
    z = grid.get("z") or []
    if not z or not isinstance(z[0], Sequence):
        raise SceneError("a surface grid has z as rows")
    rows, cols = len(z), len(z[0])
    if rows < 2 or cols < 2:
        raise SceneError("a surface needs at least a 2 by 2 grid")
    xs = list(grid.get("x") or range(cols))
    ys = list(grid.get("y") or range(rows))
    if len(xs) != cols or len(ys) != rows:
        raise SceneError("a surface's x has one value per column and y one per row")
    scene = Scene(length_unit=length_unit)
    index: dict[tuple[int, int], int] = {}
    for r in range(rows):
        for c in range(cols):
            v = _num(z[r][c])
            if v is not None:
                index[(r, c)] = len(scene.points)
                scene.points.append((float(xs[c]), float(ys[r]), v))
    for r in range(rows - 1):
        for c in range(cols - 1):
            quad = [(r, c), (r, c + 1), (r + 1, c + 1), (r + 1, c)]
            if all(q in index for q in quad):
                scene.cells.append(tuple(index[q] for q in quad))
    return scene


def volume_slice(grid: Mapping[str, Any], *, axis: str = "z", at: float | None = None,
                 length_unit: str = "") -> Scene:
    """A plane through a volume, as quads coloured by the field there."""
    xs, ys, zs = (list(grid.get(k) or []) for k in ("x", "y", "z"))
    values = list(grid.get("values") or [])
    nx, ny, nz = len(xs), len(ys), len(zs)
    if nx * ny * nz != len(values) or min(nx, ny, nz) < 2:
        raise SceneError(f"a volume has {nx}x{ny}x{nz} grid points and {len(values)} values")
    axes = {"x": xs, "y": ys, "z": zs}
    line = axes[axis]
    k = len(line) // 2 if at is None else min(range(len(line)), key=lambda i: abs(line[i] - at))

    def val(i: int, j: int, kk: int) -> float | None:
        return _num(values[i + nx * (j + ny * kk)])

    scene = Scene(length_unit=length_unit)
    if axis == "z":
        a_vals, b_vals = xs, ys

        def p(i, j):
            return (xs[i], ys[j], zs[k])

        def v(i, j):
            return val(i, j, k)
    elif axis == "y":
        a_vals, b_vals = xs, zs

        def p(i, j):
            return (xs[i], ys[k], zs[j])

        def v(i, j):
            return val(i, k, j)
    else:
        a_vals, b_vals = ys, zs

        def p(i, j):
            return (xs[k], ys[i], zs[j])

        def v(i, j):
            return val(k, i, j)
    idx = {}
    point_vals = []
    for j in range(len(b_vals)):
        for i in range(len(a_vals)):
            idx[(i, j)] = len(scene.points)
            scene.points.append(tuple(float(c) for c in p(i, j)))  # type: ignore[arg-type]
            point_vals.append(v(i, j))
    for j in range(len(b_vals) - 1):
        for i in range(len(a_vals) - 1):
            scene.cells.append((idx[(i, j)], idx[(i + 1, j)], idx[(i + 1, j + 1)], idx[(i, j + 1)]))
    scene.scalars["value"] = ("", "point", point_vals)
    return scene


def centroid(scene: Scene, cell: tuple[int, ...]) -> tuple[float, float, float]:
    pts = [scene.points[i] for i in cell]
    n = len(pts)
    return (sum(p[0] for p in pts) / n, sum(p[1] for p in pts) / n, sum(p[2] for p in pts) / n)


def cut(scene: Scene, *, axis: str | None = None, at: float | None = None,
        planes: Sequence[Mapping[str, Any]] = ()) -> list[int]:
    """Cells kept after a cutaway at ``axis = at`` (cells above it go) and clip planes."""
    k = {"x": 0, "y": 1, "z": 2}.get(axis or "", None)
    keep = []
    for ci, cell in enumerate(scene.cells):
        c = centroid(scene, cell)
        if k is not None and at is not None and c[k] > at:
            continue
        drop = False
        for pl in planes:
            n, o = pl.get("normal") or (0, 0, 1), pl.get("origin") or (0, 0, 0)
            if sum((c[i] - o[i]) * n[i] for i in range(3)) > 0:
                drop = True
                break
        if not drop:
            keep.append(ci)
    return keep


def explode(scene: Scene, factor: float) -> dict[int, tuple[float, float, float]]:
    """Per-cell offsets that move each element away from the scene's centre axis."""
    if not factor or not scene.elements:
        return {}
    cx = sum(p[0] for p in scene.points) / len(scene.points)
    cy = sum(p[1] for p in scene.points) / len(scene.points)
    by_el: dict[str, list[int]] = {}
    for ci, el in enumerate(scene.elements):
        by_el.setdefault(el, []).append(ci)
    out = {}
    for cells in by_el.values():
        pts = [scene.points[i] for ci in cells for i in scene.cells[ci]]
        ex = sum(p[0] for p in pts) / len(pts) - cx
        ey = sum(p[1] for p in pts) / len(pts) - cy
        for ci in cells:
            out[ci] = (ex * factor, ey * factor, 0.0)
    return out


def camera_of(camera: Any) -> tuple[float, float]:
    """(azimuth, elevation) for a preset name or ``{azimuth, elevation}``."""
    if isinstance(camera, str):
        return CAMERAS.get(camera, CAMERAS["iso"])
    if isinstance(camera, Mapping):
        return (float(camera.get("azimuth", 45.0)), float(camera.get("elevation", 30.0)))
    return CAMERAS["iso"]


def project(point: tuple[float, float, float], azimuth: float, elevation: float) -> tuple[float, float, float]:
    """Orthographic: (screen x, screen y up, depth toward the viewer)."""
    a, e = math.radians(azimuth), math.radians(elevation)
    x, y, z = point
    # Rotate about z by -azimuth, then tilt by elevation about the screen x axis.
    xr = x * math.cos(a) + y * math.sin(a)
    yr = -x * math.sin(a) + y * math.cos(a)
    sx = xr
    sy = z * math.cos(e) + yr * math.sin(e)
    depth = -yr * math.cos(e) + z * math.sin(e)
    return sx, sy, depth


def normal_light(pts: Sequence[tuple[float, float, float]], azimuth: float = 45.0,
                 elevation: float = 30.0) -> float:
    """Lambert shading factor 0.55..1, lit from the viewer (a headlight)."""
    if len(pts) < 3:
        return 1.0
    (x0, y0, z0), (x1, y1, z1), (x2, y2, z2) = pts[0], pts[1], pts[2]
    ux, uy, uz = x1 - x0, y1 - y0, z1 - z0
    vx, vy, vz = x2 - x0, y2 - y0, z2 - z0
    nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
    norm = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    a, e = math.radians(azimuth), math.radians(elevation)
    view = (math.sin(a) * math.cos(e), -math.cos(a) * math.cos(e), math.sin(e))
    return 0.55 + 0.45 * abs(nx * view[0] + ny * view[1] + nz * view[2]) / norm


__all__ = ["CAMERAS", "INLINE_CELLS_MAX", "INLINE_POINTS_MAX", "Scene", "SceneError", "camera_of", "centroid",
           "check", "cut", "explode", "normal_light", "project", "read", "surface_mesh", "volume_slice"]
