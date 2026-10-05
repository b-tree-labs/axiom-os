# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for the RAG tests.

`ingest_body` exists because of a real failure: a test asserting that an
embedding error does not commit was written with a body of
``"content paragraph. " * 40``. Once the ingestion guard landed, that body was
refused as a repeated reading and the test stopped reaching the code path it
claimed to cover — while still being a green line in the report.

Repetitive filler is the natural thing to type when a test needs "some text of
about this length", and it is exactly the shape the guard refuses. So the
filler is provided here rather than left to each author to reinvent.
"""

from __future__ import annotations

import pytest

_SENTENCES = (
    "During period {i} the reactor was operated at power for {h} hours.",
    "Fuel element inspections were completed on schedule for period {i}.",
    "Coolant inlet temperature averaged {t} degrees against a limit of 48.",
    "No measurable degradation was observed in the reflector assembly.",
    "Reactivity worth measurements for period {i} agreed with prediction.",
)


def ingest_body(paragraphs: int = 8) -> str:
    """Prose that the ingestion guard admits, for tests that just need a document.

    Varied deliberately: every paragraph differs, so the distinct-token ratio
    stays well clear of the guard's floor no matter how many are requested.
    """
    out = []
    for i in range(paragraphs):
        out.append(" ".join(s.format(i=i + 1, h=1200 + i * 7, t=38 + i % 5) for s in _SENTENCES))
    return "\n\n".join(out)


@pytest.fixture
def prose_body():
    """Fixture form of :func:`ingest_body`."""
    return ingest_body
