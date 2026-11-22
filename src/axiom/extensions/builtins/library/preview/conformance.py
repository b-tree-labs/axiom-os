# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The previewer conformance suite: what every contributed previewer must pass (ADR-213 D12).

A previewer renders untrusted content for a person who did not write it. The
suite checks the five properties that make that safe, on the contributor's own
fixtures:

1. **No network.** Sockets are refused while it runs; a previewer that reaches
   out fails, because a preview that phones home leaks what was uploaded.
2. **Bounded time.** It finishes within its manifest's ``timeout_s``.
3. **Bounded memory.** Its peak Python allocation stays under ``max_memory_mb``.
4. **Sanitized output.** The representation validates (:func:`.contract.validate`):
   no script in ``html``, media only from the item's own content, nothing a
   client would execute. A fixture that carries an injection attempt is added
   automatically and must come back valid or declined (``None``), never raise. Every other string is data: clients
   render it as text, never as markup, which the appkit renderer suite checks.
5. **Accessible and theme-safe.** Columns, series, axes, nodes and frames are
   named; colours are theme tokens, never raw values, so it works in light and dark.

Use from a test::

    from axiom.extensions.builtins.library.preview.conformance import assert_conformant
    assert_conformant(my_previewer, manifest, [ctx1, ctx2])
"""

from __future__ import annotations

import socket
import threading
import time
import tracemalloc
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.library.preview.contract import (
    PreviewContext,
    PreviewerManifest,
    RepresentationError,
    previewer_from_dict,
    validate,
)

DEFAULT_MAX_MEMORY_MB = 256
INJECTION = (
    '<script>alert("x")</script><img src=x onerror=alert(1)> javascript:alert(1) '
    '<iframe src="https://example.invalid"></iframe>'
)


class NetworkUsed(RuntimeError):
    pass


@dataclass
class Report:
    previewer: str
    problems: list[str] = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


@contextmanager
def _no_network():
    real_connect = socket.socket.connect
    real_create = socket.create_connection

    def refuse(*_a: Any, **_k: Any):
        raise NetworkUsed("previewers may not use the network")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.create_connection = real_create  # type: ignore[assignment]


def _run(fn: Callable[[PreviewContext], Any], ctx: PreviewContext, timeout: float) -> tuple[Any, float, int, BaseException | None]:
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn(ctx)
        except BaseException as exc:  # noqa: BLE001 - reported
            box["error"] = exc

    tracemalloc.start()
    t0 = time.monotonic()
    th = threading.Thread(target=target, daemon=True)
    with _no_network():
        th.start()
        th.join(timeout)
    elapsed = time.monotonic() - t0
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if th.is_alive():
        return None, elapsed, peak, TimeoutError(f"did not finish within {timeout}s")
    return box.get("value"), elapsed, peak, box.get("error")


def _injection_context(base: PreviewContext, tmp: Path) -> PreviewContext:
    """The first fixture's identity, with content and extracted values that try to inject."""
    path = tmp / ("inject-" + base.file_name)
    path.write_bytes(INJECTION.encode() + b"\n" + base.path.read_bytes()[:4096])
    poisoned = {
        name: {**res, "summary": INJECTION, "metadata": {**(res.get("metadata") or {}), "title": INJECTION}}
        for name, res in (base.extractions or {}).items()
    }
    return PreviewContext(
        path=path, file_name=base.file_name, size=path.stat().st_size, sha256="0" * 64, mime=base.mime,
        extractions=poisoned, max_rows=base.max_rows,
    )


def check(
    fn: Callable[[PreviewContext], Any],
    manifest: PreviewerManifest | dict[str, Any],
    contexts: list[PreviewContext],
    *,
    max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
    tmp_dir: Path | None = None,
) -> Report:
    m = manifest if isinstance(manifest, PreviewerManifest) else previewer_from_dict(manifest)
    report = Report(previewer=m.name)
    if not contexts:
        report.problems.append("no fixtures: a previewer is checked on at least one file it previews")
        return report
    runs = list(contexts)
    if tmp_dir is not None:
        runs.append(_injection_context(contexts[0], tmp_dir))
    for i, ctx in enumerate(runs):
        label = f"{m.name} on {ctx.file_name}" + (" (injection)" if i == len(contexts) else "")
        value, elapsed, peak, error = _run(fn, ctx, m.timeout_s)
        report.checked += 1
        if isinstance(error, NetworkUsed):
            report.problems.append(f"{label}: used the network")
            continue
        if isinstance(error, TimeoutError):
            report.problems.append(f"{label}: {error}")
            continue
        if error is not None:
            report.problems.append(f"{label}: raised {type(error).__name__}: {error}")
            continue
        if peak > max_memory_mb * 1024 * 1024:
            report.problems.append(f"{label}: peak memory {peak // (1024 * 1024)} MB over {max_memory_mb} MB")
        if value is None:
            continue  # "not mine after all" is allowed; the next previewer is tried
        try:
            validate(value, where=label)
        except RepresentationError as exc:
            report.problems.append(str(exc))
            continue
        if value.get("type") not in m.produces:
            report.problems.append(f"{label}: returned {value.get('type')!r}, which its manifest does not declare")
        if elapsed > m.timeout_s:
            report.problems.append(f"{label}: took {elapsed:.1f}s, over {m.timeout_s}s")
    return report


def assert_conformant(
    fn: Callable[[PreviewContext], Any],
    manifest: PreviewerManifest | dict[str, Any],
    contexts: list[PreviewContext],
    *,
    tmp_dir: Path | None = None,
    max_memory_mb: int = DEFAULT_MAX_MEMORY_MB,
) -> Report:
    report = check(fn, manifest, contexts, tmp_dir=tmp_dir, max_memory_mb=max_memory_mb)
    if not report.ok:
        raise AssertionError(f"{report.previewer} is not conformant:\n  " + "\n  ".join(report.problems))
    return report


__all__ = ["INJECTION", "NetworkUsed", "Report", "assert_conformant", "check"]
