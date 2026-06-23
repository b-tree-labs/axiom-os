# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Export-control / proprietary screening for RAG ingest.

Screens document text and filenames for control markings on EVERY ingest
(not just the community tier), classifies severity, and recommends an action:

- ``controlled`` → **reject/quarantine**. Export-controlled (10 CFR 810, EAR,
  ITAR) or vendor-proprietary/licensed content. Must NOT be ingested to a node
  that is not export-control-authorized — routing it to a "restricted" tier on
  such a node is still exposure, so the recommendation is reject, not route.
- ``sensitive`` → **human review**. OUO/SGI/SUNSI/distribution-limited and
  similar: not for the public (community) tier, gate on a human.
- ``none`` → fine for the community tier.

NOTE: the marker sets below are a conservative engineering default and should
be reviewed by the export-control office. Markers live in document *content*
(cover pages / footers), so callers must pass extracted text, not just paths.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Filename hints (sensitive-level — the controlled determination needs content).
_FILENAME_MARKERS = [
    r"\bSS\b",  # Security Sensitive
    r"\bOUO\b",
    r"\bSGI\b",
    r"\bSUNSI\b",
    r"CONFIDENTIAL",
    r"RESTRICTED",
    r"PROPRIETARY",
]

# Export-controlled / proprietary → reject. Phrases chosen to indicate actual
# control status, not mere topical discussion.
_CONTROLLED_MARKERS = [
    (r"10\s*CFR\s*(Part\s*)?810", "10CFR810"),
    (r"\bEAR99\b", "EAR99"),
    (r"\bECCN\b", "ECCN"),
    (r"Export\s+Administration\s+Regulations", "EAR"),
    (r"\bITAR\b", "ITAR"),
    (r"International\s+Traffic\s+in\s+Arms", "ITAR"),
    (r"\bUSML\b", "USML"),
    (r"22\s*CFR\s*12[0-9]", "ITAR-22CFR"),
    (r"Export[\s\-]Controlled\s+Information", "EC-Info"),
    (r"Export[\s\-]Controlled", "EC"),
    (r"\bUCNI\b", "UCNI"),
    (r"Unclassified\s+Controlled\s+Nuclear\s+Information", "UCNI"),
    (r"Naval\s+Nuclear\s+Propulsion\s+Information", "NNPI"),
    (r"Proprietary\s+Information", "Proprietary"),
    (r"Proprietary\s+and\s+Confidential", "Proprietary"),
    (r"Company\s+Proprietary", "Proprietary"),
    (r"subject\s+to\s+(the\s+)?(executed\s+)?license\s+agreement", "Licensed"),
    (r"DOE\s+Applied\s+Technology", "DOE-AT"),
]

# Sensitive-but-not-export-controlled → human review (keep out of community).
_SENSITIVE_MARKERS = [
    (r"Official\s+Use\s+Only", "OUO"),
    (r"\bOUO\b", "OUO"),
    (r"Safeguards\s+Information", "SGI"),
    (r"\bSUNSI\b", "SUNSI"),
    (r"Distribution\s+Statement\s+[B-F]\b", "DistStmt"),
    (r"DISTRIBUTION\s+LIMITED", "Dist-Limited"),
    (r"NOT\s+FOR\s+PUBLIC\s+RELEASE", "Not-Public"),
    (r"\bCONFIDENTIAL\b", "Confidential"),
]


@dataclass
class ScreeningResult:
    """Result of EC/proprietary screening for a document."""

    path: str
    severity: str = "none"  # none | sensitive | controlled
    allowed_community: bool = True
    allowed_restricted: bool = True
    markers_found: list[str] = field(default_factory=list)
    recommendation: str = "community"  # community | review | reject


def screen_document(
    path: str,
    text: str,
    target_corpus: str = "rag-community",
) -> ScreeningResult:
    """Screen a document for control markers. Runs regardless of target tier.

    Args:
        path: File path (checked for filename markers).
        text: Extracted document text (checked for content markers).
        target_corpus: Informational; screening always runs.

    Returns:
        ScreeningResult with severity + recommendation.
    """
    markers: list[str] = []

    # Normalize separators so word-boundary markers match e.g. "Manual_OUO.pdf".
    path_norm = re.sub(r"[_./\\-]+", " ", path)
    for pattern in _FILENAME_MARKERS:
        if re.search(pattern, path_norm, re.IGNORECASE):
            markers.append(f"filename:{pattern}")

    # Markings are typically in headers/footers; scan both ends.
    header = text[:5000] if text else ""
    footer = text[-2000:] if text and len(text) > 2000 else ""
    check_text = header + "\n" + footer

    controlled_hits = [
        label for pattern, label in _CONTROLLED_MARKERS if re.search(pattern, check_text, re.IGNORECASE)
    ]
    sensitive_hits = [
        label for pattern, label in _SENSITIVE_MARKERS if re.search(pattern, check_text, re.IGNORECASE)
    ]
    markers.extend(f"controlled:{c}" for c in controlled_hits)
    markers.extend(f"sensitive:{s}" for s in sensitive_hits)

    if controlled_hits:
        log.warning("EC screening REJECTED %s (controlled): %s", path, controlled_hits)
        return ScreeningResult(
            path=path,
            severity="controlled",
            allowed_community=False,
            allowed_restricted=False,
            markers_found=markers,
            recommendation="reject",
        )

    if markers:  # filename or content sensitive markers
        log.warning("EC screening flagged %s for review (sensitive): %s", path, markers)
        return ScreeningResult(
            path=path,
            severity="sensitive",
            allowed_community=False,
            allowed_restricted=True,
            markers_found=markers,
            recommendation="review",
        )

    return ScreeningResult(path=path, severity="none", recommendation="community")


def screen_batch(
    documents: list[tuple[str, str]],
    target_corpus: str = "rag-community",
) -> tuple[list[tuple[str, str]], list[ScreeningResult]]:
    """Screen a batch. Returns (allowed_for_community, flagged).

    A document is "allowed" only when screening finds nothing (severity none).
    Anything sensitive or controlled is flagged for routing/review/reject.
    """
    allowed: list[tuple[str, str]] = []
    flagged: list[ScreeningResult] = []

    for path, text in documents:
        result = screen_document(path, text, target_corpus)
        if result.severity == "none":
            allowed.append((path, text))
        else:
            flagged.append(result)

    if flagged:
        controlled = sum(1 for r in flagged if r.severity == "controlled")
        log.warning(
            "EC screening: %d/%d flagged (%d controlled→reject, %d sensitive→review)",
            len(flagged),
            len(documents),
            controlled,
            len(flagged) - controlled,
        )

    return allowed, flagged
