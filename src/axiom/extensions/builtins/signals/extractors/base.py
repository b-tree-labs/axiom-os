# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Base extractor ABC for axi signal."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..models import Extraction


class BaseExtractor(ABC):
    """Abstract base class for all signal extractors.

    Each extractor reads from a specific source type (GitLab JSON,
    voice memo, transcript, freetext) and produces an Extraction
    containing a list of Signals.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this extractor (e.g., 'gitlab_diff')."""
        ...

    @abstractmethod
    def extract(self, source: Path, **kwargs) -> Extraction:
        """Extract signals from a source file.

        Args:
            source: Path to the source file or directory.
            **kwargs: Extractor-specific options.

        Returns:
            Extraction containing signals and any errors.
        """
        ...

    def can_handle(self, path: Path) -> bool:
        """Whether this extractor can process the given file.

        Override in subclasses for more specific checks.
        """
        return path.exists()
