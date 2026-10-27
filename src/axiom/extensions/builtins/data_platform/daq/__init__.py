# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""DAQ producer core (ADR-106/107; spec-signal-ingest-and-producer).

``DAQReader → DAQConsolidator → DAQJournal → {DAQTransmitter, LiveDispatcher}``
— the Journal is written first; every consumer is a cursor into it; the
Transmitter targets the ingest face (``POST /ingest/rows``) and never carries
``site``. Domain-agnostic: a deployment supplies the Reader and the
sensitivity filter.
"""

from .consolidator import DAQConsolidator, SensitivityFilter
from .dispatcher import DispatchResult, LiveDispatcher, Subscriber
from .envelope import (
    ChainFault,
    ConsolidatedRecord,
    JournaledRecord,
    SignalEnvelope,
    canonical_json,
    compute_content_hash,
    verify_chain,
)
from .health import CreditedGuard, SilenceVerdict, merge_details, silence
from .journal import DAQJournal, JournalFull, OverflowPolicy, validate_overflow_policy
from .producer import DAQReader, Producer, run_forever
from .transmitter import DAQTransmitter, TransmitResult, Transport, UrllibTransport

__all__ = [
    "ChainFault",
    "ConsolidatedRecord",
    "CreditedGuard",
    "DAQConsolidator",
    "DAQJournal",
    "DAQReader",
    "DAQTransmitter",
    "DispatchResult",
    "JournalFull",
    "JournaledRecord",
    "LiveDispatcher",
    "OverflowPolicy",
    "Producer",
    "SensitivityFilter",
    "SignalEnvelope",
    "SilenceVerdict",
    "Subscriber",
    "TransmitResult",
    "Transport",
    "UrllibTransport",
    "canonical_json",
    "compute_content_hash",
    "merge_details",
    "run_forever",
    "silence",
    "validate_overflow_policy",
    "verify_chain",
]
