# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""CURIO chunking strategy optimizer.

Tracks which chunking parameters produce the best retrieval quality
and proposes bounded, reversible experiments. Each experiment creates
a new generation via the blue/green pattern.

Parameters optimized:
- min_chunk_size (200-500)
- max_chunk_size (800-3000)
- overlap_sentences (0-3)
- boundary types (heading-only, heading+table, heading+table+code)
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_EXPERIMENTS_DIR = Path.home() / ".axi" / "curio" / "experiments"


@dataclass
class ChunkingExperiment:
    """A bounded chunking parameter experiment."""

    experiment_id: str
    corpus: str
    generation: int
    parameters: dict = field(default_factory=dict)
    status: str = "pending"  # pending, running, evaluated, promoted, discarded
    quality_score: float | None = None
    created_at: str = ""
    evaluated_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ChunkingExperiment:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# Parameter bounds (safety limits)
PARAMETER_BOUNDS = {
    "min_chunk_size": (100, 800),
    "max_chunk_size": (400, 4000),
    "overlap_sentences": (0, 5),
}

# Default parameters
DEFAULT_PARAMETERS = {
    "min_chunk_size": 200,
    "max_chunk_size": 2000,
    "overlap_sentences": 1,
    "chunking_tier": "semantic",
}


def propose_experiment(
    corpus: str,
    current_params: dict | None = None,
    direction: str = "explore",
) -> ChunkingExperiment:
    """Propose a new chunking experiment.

    Args:
        corpus: Corpus tier to experiment on
        current_params: Current best parameters (or defaults)
        direction: "explore" (random variation) or "refine" (small adjustment)

    Returns:
        ChunkingExperiment with proposed parameters
    """
    import secrets

    base = current_params or DEFAULT_PARAMETERS.copy()
    params = base.copy()

    if direction == "explore":
        # Larger variations
        import random

        for key, (lo, hi) in PARAMETER_BOUNDS.items():
            if key in params:
                current = params[key]
                delta = random.randint(-(hi - lo) // 4, (hi - lo) // 4)
                params[key] = max(lo, min(hi, current + delta))
    else:
        # Small refinements (±10%)
        import random

        for key, (lo, hi) in PARAMETER_BOUNDS.items():
            if key in params:
                current = params[key]
                delta = random.randint(-max(1, current // 10), max(1, current // 10))
                params[key] = max(lo, min(hi, current + delta))

    exp_id = f"exp-{secrets.token_hex(4)}"
    return ChunkingExperiment(
        experiment_id=exp_id,
        corpus=corpus,
        generation=0,  # Set when generation is created
        parameters=params,
        status="pending",
        created_at=datetime.now(UTC).isoformat(),
    )


def save_experiment(experiment: ChunkingExperiment) -> None:
    """Persist experiment to disk."""
    _EXPERIMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _EXPERIMENTS_DIR / f"{experiment.experiment_id}.json"
    path.write_text(json.dumps(experiment.to_dict(), indent=2), encoding="utf-8")


def load_experiments(corpus: str | None = None) -> list[ChunkingExperiment]:
    """Load all experiments, optionally filtered by corpus."""
    if not _EXPERIMENTS_DIR.exists():
        return []

    experiments = []
    for f in sorted(_EXPERIMENTS_DIR.glob("exp-*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            exp = ChunkingExperiment.from_dict(data)
            if corpus is None or exp.corpus == corpus:
                experiments.append(exp)
        except Exception:
            continue
    return experiments


def validate_parameters(params: dict) -> tuple[bool, str]:
    """Check that parameters are within bounds."""
    for key, (lo, hi) in PARAMETER_BOUNDS.items():
        if key in params:
            val = params[key]
            if not (lo <= val <= hi):
                return False, f"{key}={val} out of bounds [{lo}, {hi}]"
    return True, "ok"
