# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Chat provider factory — auto-detect available deps, instantiate best provider.

Follows the Publisher Factory pattern. Detects rich/prompt_toolkit at runtime
and falls back to ANSI/basic providers when deps are missing.
"""

from __future__ import annotations

from .providers.base import InputProvider, RenderProvider


def _rich_available() -> bool:
    """Check if rich is importable."""
    try:
        import rich  # noqa: F401

        return True
    except ImportError:
        return False


def _ptk_available() -> bool:
    """Check if prompt_toolkit is importable."""
    try:
        import prompt_toolkit  # noqa: F401

        return True
    except ImportError:
        return False


def create_render_provider(force: str | None = None) -> RenderProvider:
    """Create the best available render provider.

    Args:
        force: "rich" or "ansi" to override auto-detection.
    """
    if force == "ansi" or (force is None and not _rich_available()):
        from .providers.ansi_render import AnsiRenderProvider

        return AnsiRenderProvider()

    if force == "rich" or (force is None and _rich_available()):
        try:
            from .providers.rich_render import RichRenderProvider

            return RichRenderProvider()
        except ImportError:
            from .providers.ansi_render import AnsiRenderProvider

            return AnsiRenderProvider()

    from .providers.ansi_render import AnsiRenderProvider

    return AnsiRenderProvider()


def create_input_provider(force: str | None = None) -> InputProvider:
    """Create the best available input provider.

    Args:
        force: "ptk" or "basic" to override auto-detection.
    """
    if force == "basic" or (force is None and not _ptk_available()):
        from .providers.basic_input import BasicInputProvider

        return BasicInputProvider()

    if force == "ptk" or (force is None and _ptk_available()):
        try:
            from .providers.ptk_input import PTKInputProvider

            return PTKInputProvider()
        except ImportError:
            from .providers.basic_input import BasicInputProvider

            return BasicInputProvider()

    from .providers.basic_input import BasicInputProvider

    return BasicInputProvider()
