# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LatexPdfProvider — compile a LaTeX project to PDF via Tectonic.

Unlike :class:`PandocPdfProvider` (markdown → HTML → WeasyPrint, branded),
this provider compiles an *existing LaTeX source* — a ``.tex`` entry file
(optionally a project directory containing ``main.tex``) — into a PDF using
the Tectonic engine. Tectonic is a single self-contained binary that
auto-fetches the packages a document needs, is reproducible, and runs the
XeTeX engine (native UTF-8 — α, →, × compile without inputenc gymnastics).

This is the path for author-supplied LaTeX (e.g. a journal template such as
TMLR) where the document owns its own class/style and we must NOT re-typeset
it through markdown. Local ``.sty``/``.bst``/``.bib`` files in the project
directory and ``\\input``/``\\includegraphics`` are resolved by running the
compile with ``cwd`` set to the entry's directory. See ADR-090.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ...factory import PublisherFactory
from ..base import (
    GenerationOptions,
    GenerationProvider,
    GenerationResult,
)

#: Per-compile wall-clock cap. Tectonic's first run may fetch packages; later
#: runs hit the local cache and are fast. Generous to absorb a cold cache.
_COMPILE_TIMEOUT_S = 600


class LatexPdfProvider(GenerationProvider):
    """Compile a ``.tex`` source (or a project dir with ``main.tex``) to PDF."""

    def __init__(self, config: dict[str, Any] | None = None):
        config = config or {}
        self.tectonic_path = shutil.which("tectonic")

    def _resolve_entry(self, source_path: Path) -> Path:
        """The ``.tex`` entry to compile. A directory resolves to ``main.tex``."""
        if source_path.is_dir():
            entry = source_path / "main.tex"
            if not entry.is_file():
                raise RuntimeError(
                    f"no main.tex in LaTeX project directory {source_path}"
                )
            return entry
        return source_path

    def generate(
        self, source_path: Path, output_path: Path, options: GenerationOptions
    ) -> GenerationResult:
        if not self.tectonic_path:
            raise RuntimeError(
                "tectonic not found. Install from "
                "https://tectonic-typesetting.github.io/ "
                "(macOS: brew install tectonic; Linux: see docs)."
            )

        entry = self._resolve_entry(source_path)
        project_dir = entry.parent
        output_path.parent.mkdir(parents=True, exist_ok=True)
        warnings: list[str] = []

        # Compile into a scratch dir so intermediate files (.aux/.log/.pdf)
        # never litter the source project; copy only the PDF back out.
        with tempfile.TemporaryDirectory() as tmp:
            cmd = [
                self.tectonic_path,
                str(entry),
                "--outdir",
                tmp,
                "--keep-logs",
                "--chatter",
                "minimal",
            ]
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    cwd=str(project_dir),
                    timeout=_COMPILE_TIMEOUT_S,
                )
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    f"tectonic timed out (>{_COMPILE_TIMEOUT_S}s) compiling {entry.name}"
                ) from exc

            if result.returncode != 0:
                # Tectonic's diagnostics go to stderr; surface the tail so the
                # caller sees the actual LaTeX error, not just "exit 1".
                raise RuntimeError(
                    f"tectonic failed (exit {result.returncode}) compiling "
                    f"{entry.name}:\n{result.stderr[-2000:]}"
                )

            # Tectonic warnings (overfull boxes, undefined refs) are non-fatal
            # but worth surfacing for the publisher to log.
            if result.stderr.strip():
                warnings.append(f"tectonic: {result.stderr.strip()[-500:]}")

            produced = Path(tmp) / f"{entry.stem}.pdf"
            if not produced.is_file():
                raise RuntimeError(
                    f"tectonic reported success but produced no PDF for {entry.name}"
                )
            shutil.copyfile(produced, output_path)

        return GenerationResult(
            output_path=output_path,
            format="pdf",
            size_bytes=output_path.stat().st_size,
            warnings=warnings,
        )

    def rewrite_links(self, artifact_path: Path, link_map: dict[str, str]) -> None:
        """PDF is a terminal artifact; links are baked in at compile time."""
        return

    def get_output_extension(self) -> str:
        return ".pdf"

    def supports_watermark(self) -> bool:
        # The document owns its own styling; we don't inject a watermark
        # (that would require editing the author's LaTeX). False keeps the
        # engine from promising a draft stamp it can't honor.
        return False


# Self-register with factory
PublisherFactory.register("generation", "latex-pdf", LatexPdfProvider)
