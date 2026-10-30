# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""HDF5 and NetCDF: the structure (groups, datasets, shapes, types, attributes) plus tiny peeks.

The structure is read lazily: no dataset is loaded whole. A peek is the first
few values of a small slice, which is what makes a structure tree useful
without making a 40 GB file a 40 GB read.
"""

from __future__ import annotations

from typing import Any

from axiom.extensions.builtins.library.extract.contract import (
    ExtractionContext,
    ExtractionResult,
    ExtractorUnavailable,
    jsonable,
)

MAX_NODES = 1500
PEEK_VALUES = 8
MAX_ATTRS = 20


def _attrs(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        for i, (k, v) in enumerate(obj.attrs.items()):
            if i >= MAX_ATTRS:
                out["…"] = f"{len(obj.attrs) - MAX_ATTRS} more"
                break
            if isinstance(v, bytes):
                v = v.decode(errors="replace")
            out[str(k)] = jsonable(v) if not hasattr(v, "shape") or getattr(v, "size", 0) <= 16 else f"array{tuple(v.shape)}"
    except Exception:  # noqa: BLE001 - unreadable attributes are skipped
        pass
    return out


def _peek(ds: Any) -> list[Any] | None:
    try:
        if ds.size == 0 or ds.dtype.kind in ("V", "O"):
            return None
        if ds.ndim == 0:
            return [jsonable(ds[()])]
        idx = tuple([slice(0, PEEK_VALUES)] + [0] * (ds.ndim - 1))
        return jsonable(list(ds[idx]))
    except Exception:  # noqa: BLE001
        return None


def hdf5(ctx: ExtractionContext) -> ExtractionResult:
    try:
        import h5py
    except ImportError as exc:
        raise ExtractorUnavailable("h5py is not installed (pip install h5py)") from exc
    count = [0]
    datasets: list[str] = []

    def walk(group: Any, name: str) -> dict[str, Any]:
        node: dict[str, Any] = {"name": name or "/", "kind": "group"}
        attrs = _attrs(group)
        if attrs:
            node["attrs"] = attrs
        children = []
        for key in group:
            count[0] += 1
            if count[0] > MAX_NODES:
                children.append({"name": "…", "kind": "more", "detail": "structure truncated"})
                break
            try:
                obj = group[key]
            except Exception:  # noqa: BLE001 - a dangling external link
                children.append({"name": key, "kind": "link", "detail": "unresolvable"})
                continue
            if isinstance(obj, h5py.Group):
                children.append(walk(obj, key))
            else:
                datasets.append(obj.name)
                child = {
                    "name": key,
                    "kind": "dataset",
                    "shape": list(obj.shape),
                    "dtype": str(obj.dtype),
                    "detail": f"{obj.dtype} {tuple(obj.shape)}",
                }
                a = _attrs(obj)
                if a:
                    child["attrs"] = a
                peek = _peek(obj)
                if peek is not None:
                    child["peek"] = peek
                children.append(child)
        node["children"] = children
        return node

    with h5py.File(str(ctx.path), "r") as f:
        tree = walk(f, "")
        root_attrs = _attrs(f)
    is_netcdf = "_NCProperties" in root_attrs or any(k in root_attrs for k in ("Conventions", "featureType"))
    meta = {"format": "netcdf4" if is_netcdf else "hdf5", "datasets": len(datasets), "attributes": root_attrs}
    return ExtractionResult(
        metadata=meta,
        features={"structure": tree, "dataset_paths": datasets[:200]},
        preview={"type": "tree", "nodes": [_tree_node(tree)]},
        summary=f"{meta['format'].upper()}, {len(datasets)} dataset{'s' if len(datasets) != 1 else ''}",
    )


def netcdf3(ctx: ExtractionContext) -> ExtractionResult:
    """Classic NetCDF (the ``CDF`` signature); NetCDF-4 files are HDF5 and go to :func:`hdf5`."""
    try:
        import netCDF4
    except ImportError as exc:
        raise ExtractorUnavailable("netCDF4 is not installed (pip install netCDF4)") from exc
    with netCDF4.Dataset(str(ctx.path), "r") as ds:
        dims = {k: len(v) for k, v in ds.dimensions.items()}
        variables = []
        for name, var in list(ds.variables.items())[:MAX_NODES]:
            node: dict[str, Any] = {
                "name": name, "kind": "dataset", "shape": list(var.shape), "dtype": str(var.dtype),
                "detail": f"{var.dtype} {tuple(var.dimensions)}",
            }
            attrs = {k: jsonable(var.getncattr(k)) for k in var.ncattrs()[:MAX_ATTRS]}
            if attrs:
                node["attrs"] = attrs
            try:
                if var.size and var.ndim:
                    node["peek"] = jsonable(list(var[tuple([slice(0, PEEK_VALUES)] + [0] * (var.ndim - 1))]))
            except Exception:  # noqa: BLE001
                pass
            variables.append(node)
        gattrs = {k: jsonable(ds.getncattr(k)) for k in ds.ncattrs()[:MAX_ATTRS]}
    tree = {"name": "/", "kind": "group", "attrs": gattrs, "children": variables}
    return ExtractionResult(
        metadata={"format": "netcdf3", "dimensions": dims, "attributes": gattrs, "datasets": len(variables)},
        features={"structure": tree},
        preview={"type": "tree", "nodes": [_tree_node(tree)]},
        summary=f"NetCDF, {len(variables)} variables, dimensions {dims}",
    )


def _tree_node(node: dict[str, Any]) -> dict[str, Any]:
    """The extraction structure as the preview ``tree`` representation."""
    out: dict[str, Any] = {"name": node.get("name") or "/", "kind": node.get("kind", "group")}
    if node.get("detail"):
        out["detail"] = node["detail"]
    if node.get("peek") is not None:
        out["peek"] = node["peek"]
    children = [_tree_node(c) for c in node.get("children") or []]
    attrs = node.get("attrs") or {}
    children = [{"name": f"@{k}", "kind": "attr", "detail": str(v)[:120]} for k, v in attrs.items()] + children
    if children:
        out["children"] = children
    return out


__all__ = ["hdf5", "netcdf3"]
