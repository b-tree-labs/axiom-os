# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Calibration audit for typed-decision endpoints.

Evaluates ANY endpoint speaking the ``{state, questions} -> distributions``
shape (a "typed decision": choose one option from a caller-defined schema,
with a probability distribution over the options) and produces:

- a reliability curve (stated confidence vs empirical accuracy, binned),
- ECE / MCE / Brier score,
- a gate verdict (pass / fail against an ECE threshold + minimum volume).

The question it answers, per the coopetition study of calibrated
typed-decision APIs: *does 90 % stated confidence mean right 90 % of the
time?* — measured, not taken from a marketing page.

Adapters
--------
- ``mock``     — deterministic, seeded, offline. Used by the tests; its
                 miscalibration is tunable so the metrics can be verified
                 against known ground truth.
- ``ollama``   — local baseline over HTTP (default ``http://localhost:11434``,
                 loopback only unless you point it elsewhere): a readout-style
                 prompt asks a local model for a JSON distribution over the
                 options. No third-party service involved.
- ``external`` — the ``--endpoint`` seam where an external provider adapter
                 would plug. **Ships as a stub.** It performs NO network I/O:
                 invoking it raises ``ExternalCallBlocked`` until (a) a
                 credential is configured AND (b) an explicit human approval
                 flag is set AND (c) a concrete provider adapter has been
                 written. No account creation, no third-party calls, ever,
                 from this file as shipped.

Calibration math is vendored below: ``postrule`` 1.3.0 (checked) exports no
``TemperatureScaler`` / ECE / reliability primitives, so a minimal,
test-covered implementation lives here until the postrule calibration
module lands.

Datasets: ``ag_news`` / ``trec6`` load via the optional HuggingFace
``datasets`` package (public text classification sets; downloading them is
a dataset fetch, not a decision-API call). ``jsonl:<path>`` loads local
records of the form ``{"text": ..., "label": ...}``. Tests use synthetic
in-memory examples only.

Usage:

    PYTHONPATH=src python scripts/calibration_audit.py \
        --adapter mock --dataset synthetic --limit 500 --out report.json

    PYTHONPATH=src python scripts/calibration_audit.py \
        --adapter ollama --model llama3.2 --dataset ag_news --limit 200
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Decision shape: {state, questions} -> distributions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    """One typed decision: pick an option, with a distribution."""

    id: str
    text: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class Answer:
    """One decision result. ``distribution`` maps option -> probability."""

    id: str
    choice: str
    distribution: dict[str, float]

    @property
    def confidence(self) -> float:
        return self.distribution.get(self.choice, 0.0)


class ProtocolError(ValueError):
    """The endpoint's answer violates the typed-decision contract."""


def validate_answer(answer: Answer, question: Question, *, tol: float = 1e-3) -> Answer:
    """Enforce the contract: choice within schema, distribution over the
    schema's options, probabilities summing to ~1. Schema-invalid output is
    an audit *finding*, not something to silently normalize away."""
    if answer.id != question.id:
        raise ProtocolError(f"answer id {answer.id!r} != question id {question.id!r}")
    if answer.choice not in question.options:
        raise ProtocolError(
            f"choice {answer.choice!r} not in schema options {question.options!r}"
        )
    extra = set(answer.distribution) - set(question.options)
    if extra:
        raise ProtocolError(f"distribution has options outside the schema: {sorted(extra)}")
    total = sum(answer.distribution.values())
    if abs(total - 1.0) > tol:
        raise ProtocolError(f"distribution sums to {total:.4f}, not 1.0 (tol {tol})")
    if any(p < 0 for p in answer.distribution.values()):
        raise ProtocolError("distribution has negative probabilities")
    return answer


# ---------------------------------------------------------------------------
# Calibration math (vendored — postrule 1.3.0 exports none of this; see
# module docstring)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReliabilityBin:
    lo: float
    hi: float
    count: int
    avg_confidence: float
    accuracy: float


def reliability_curve(
    confidences: list[float], correct: list[bool], *, n_bins: int = 10
) -> list[ReliabilityBin]:
    """Equal-width bins over [0, 1]; per-bin mean confidence and accuracy."""
    if len(confidences) != len(correct):
        raise ValueError("confidences and correct must be the same length")
    bins: list[list[tuple[float, bool]]] = [[] for _ in range(n_bins)]
    for conf, ok in zip(confidences, correct, strict=True):
        idx = min(n_bins - 1, int(conf * n_bins))  # conf==1.0 -> last bin
        bins[idx].append((conf, ok))
    out: list[ReliabilityBin] = []
    for i, members in enumerate(bins):
        lo, hi = i / n_bins, (i + 1) / n_bins
        if members:
            avg_conf = sum(c for c, _ in members) / len(members)
            acc = sum(1 for _, ok in members if ok) / len(members)
        else:
            avg_conf, acc = 0.0, 0.0
        out.append(ReliabilityBin(lo=lo, hi=hi, count=len(members),
                                  avg_confidence=avg_conf, accuracy=acc))
    return out


def expected_calibration_error(curve: list[ReliabilityBin]) -> float:
    """ECE: count-weighted mean |accuracy - confidence| over the bins."""
    n = sum(b.count for b in curve)
    if n == 0:
        raise ValueError("cannot compute ECE over zero samples")
    return sum(
        (b.count / n) * abs(b.accuracy - b.avg_confidence) for b in curve if b.count
    )


def max_calibration_error(curve: list[ReliabilityBin]) -> float:
    """MCE: worst per-bin |accuracy - confidence| (occupied bins only)."""
    gaps = [abs(b.accuracy - b.avg_confidence) for b in curve if b.count]
    return max(gaps) if gaps else 0.0


def brier_score(confidences: list[float], correct: list[bool]) -> float:
    """Mean squared error of the chosen-option confidence vs the outcome."""
    if not confidences:
        raise ValueError("cannot compute Brier score over zero samples")
    return sum(
        (c - (1.0 if ok else 0.0)) ** 2
        for c, ok in zip(confidences, correct, strict=True)
    ) / len(confidences)


@dataclass(frozen=True)
class GateVerdict:
    passed: bool
    reasons: tuple[str, ...]
    ece: float
    mce: float
    brier: float
    accuracy: float
    n: int


def gate_verdict(
    confidences: list[float],
    correct: list[bool],
    *,
    n_bins: int = 10,
    ece_threshold: float = 0.05,
    min_samples: int = 100,
) -> tuple[GateVerdict, list[ReliabilityBin]]:
    """Pass iff enough samples AND ECE within threshold. Reasons name every
    failed criterion — a failed gate must say why."""
    curve = reliability_curve(confidences, correct, n_bins=n_bins)
    ece = expected_calibration_error(curve)
    reasons: list[str] = []
    if len(confidences) < min_samples:
        reasons.append(f"insufficient volume: n={len(confidences)} < {min_samples}")
    if ece > ece_threshold:
        reasons.append(f"ECE {ece:.4f} exceeds threshold {ece_threshold}")
    verdict = GateVerdict(
        passed=not reasons,
        reasons=tuple(reasons),
        ece=ece,
        mce=max_calibration_error(curve),
        brier=brier_score(confidences, correct),
        accuracy=sum(correct) / len(correct),
        n=len(confidences),
    )
    return verdict, curve


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


class MockAdapter:
    """Deterministic offline adapter for tests and dry runs.

    ``accuracy`` sets the true empirical correctness rate;
    ``stated_confidence`` sets the confidence it *claims*. Setting them
    equal yields a (near-)calibrated endpoint; a gap produces a known
    miscalibration the metrics must detect.
    """

    name = "mock"

    def __init__(
        self,
        *,
        seed: int = 20260919,
        accuracy: float = 0.8,
        stated_confidence: float = 0.8,
    ) -> None:
        self._seed = seed
        self._accuracy = accuracy
        self._stated = stated_confidence

    def decide(self, state: str, questions: list[Question],
               gold: dict[str, str] | None = None) -> list[Answer]:
        gold = gold or {}
        answers: list[Answer] = []
        for q in questions:
            # Per-question deterministic randomness: hash(state, id, seed).
            digest = hashlib.sha256(
                f"{self._seed}:{state}:{q.id}".encode()
            ).digest()
            rng = random.Random(digest)
            gold_label = gold.get(q.id)
            if gold_label is not None and rng.random() < self._accuracy:
                choice = gold_label
            else:
                wrong = [o for o in q.options if o != gold_label] or list(q.options)
                choice = rng.choice(wrong)
            rest = [o for o in q.options if o != choice]
            leftover = 1.0 - self._stated
            dist = {choice: self._stated}
            for o in rest:
                dist[o] = leftover / len(rest) if rest else 0.0
            answers.append(validate_answer(Answer(id=q.id, choice=choice,
                                                  distribution=dist), q))
        return answers


class OllamaReadoutAdapter:
    """Local baseline: readout-style distribution from a local Ollama model.

    Loopback by default. Prompts the model to emit a JSON object mapping
    each option to a probability; normalizes the result (a generative
    readout rarely sums to exactly 1 — normalization is part of the readout
    method, unlike a typed endpoint where a bad sum is a contract finding).
    """

    name = "ollama"

    def __init__(self, *, base_url: str = "http://localhost:11434",
                 model: str = "llama3.2", timeout: float = 60.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    def decide(self, state: str, questions: list[Question],
               gold: dict[str, str] | None = None) -> list[Answer]:
        import requests

        answers: list[Answer] = []
        for q in questions:
            prompt = (
                "You are a typed-decision readout. Given the STATE and the "
                "QUESTION, output ONLY a JSON object mapping every option to "
                "your probability that it is the correct answer. Probabilities "
                "must sum to 1.\n\n"
                f"STATE:\n{state}\n\nQUESTION: {q.text}\n"
                f"OPTIONS: {json.dumps(list(q.options))}\n\nJSON:"
            )
            resp = requests.post(
                f"{self._base_url}/api/generate",
                json={"model": self._model, "prompt": prompt,
                      "stream": False, "format": "json"},
                timeout=self._timeout,
            )
            resp.raise_for_status()
            raw = json.loads(resp.json().get("response", "{}"))
            dist = {o: max(0.0, float(raw.get(o, 0.0))) for o in q.options}
            total = sum(dist.values())
            if total <= 0:
                dist = {o: 1.0 / len(q.options) for o in q.options}  # uninformative
            else:
                dist = {o: p / total for o, p in dist.items()}
            choice = max(dist, key=dist.get)
            answers.append(validate_answer(Answer(id=q.id, choice=choice,
                                                  distribution=dist), q))
        return answers


class ExternalCallBlocked(RuntimeError):
    """The external-provider seam is present but deliberately not armed."""


class ExternalEndpointStub:
    """The ``--endpoint`` seam for an external typed-decision provider.

    HARD RULE (shipped state): no network calls to any third-party decision
    API and no account creation. ``decide`` refuses until ALL of:

    1. ``CALIBRATION_AUDIT_EXTERNAL_APPROVED=1`` — explicit, per-run human
       approval to talk to an external service;
    2. ``CALIBRATION_AUDIT_API_KEY`` — a credential provisioned by a human
       (this tool never signs up for anything);
    3. a concrete provider adapter implemented in place of the final
       ``raise`` — request/response mapping is provider-specific and lands
       only after 1 and 2 exist to test against.
    """

    name = "external"

    def __init__(self, *, endpoint: str) -> None:
        self._endpoint = endpoint

    def decide(self, state: str, questions: list[Question],
               gold: dict[str, str] | None = None) -> list[Answer]:
        approved = os.environ.get("CALIBRATION_AUDIT_EXTERNAL_APPROVED") == "1"
        credential = os.environ.get("CALIBRATION_AUDIT_API_KEY", "")
        if not (approved and credential):
            raise ExternalCallBlocked(
                "external endpoint calls require a credential AND explicit "
                "approval: set CALIBRATION_AUDIT_API_KEY (human-provisioned; "
                "this tool creates no accounts) and "
                "CALIBRATION_AUDIT_EXTERNAL_APPROVED=1. "
                f"Target would be: {self._endpoint}"
            )
        raise NotImplementedError(
            "provider adapter not implemented: the request/response mapping "
            f"for {self._endpoint} is written only after the credential and "
            "approval above exist. No network I/O is performed by this stub."
        )


# ---------------------------------------------------------------------------
# Datasets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Example:
    text: str
    label: str


#: Public HF text-classification sets and how to read them.
HF_DATASETS = {
    "ag_news": {"path": "ag_news", "split": "test", "text": "text",
                "label": "label",
                "names": ("World", "Sports", "Business", "Sci/Tech")},
    "trec6": {"path": "trec", "split": "test", "text": "text",
              "label": "coarse_label",
              "names": ("ABBR", "ENTY", "DESC", "HUM", "LOC", "NUM")},
}


def load_examples(spec: str, *, limit: int, seed: int) -> tuple[list[Example], tuple[str, ...]]:
    """Return (examples, label options) for a dataset spec.

    ``synthetic``   — offline generated two-class toy set (smoke runs).
    ``jsonl:<path>``— local records: {"text": ..., "label": ...}.
    ``ag_news``/``trec6`` — public HF sets via the optional ``datasets``
    package (network fetch of a public dataset on first use; never a
    decision API).
    """
    if spec == "synthetic":
        rng = random.Random(seed)
        options = ("alpha", "beta")
        examples = [
            Example(text=f"synthetic document {i} token-{rng.randint(0, 9)}",
                    label=options[i % 2])
            for i in range(limit)
        ]
        return examples, options

    if spec.startswith("jsonl:"):
        path = Path(spec.removeprefix("jsonl:"))
        examples = []
        labels: list[str] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                label = str(rec["label"])
                examples.append(Example(text=str(rec["text"]), label=label))
                if label not in labels:
                    labels.append(label)
                if len(examples) >= limit:
                    break
        return examples, tuple(labels)

    if spec in HF_DATASETS:
        cfg = HF_DATASETS[spec]
        try:
            from datasets import load_dataset  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SystemExit(
                f"dataset {spec!r} needs the 'datasets' package: pip install datasets"
            ) from exc
        ds = load_dataset(cfg["path"], split=cfg["split"])
        names = cfg["names"]
        rows = ds.shuffle(seed=seed).select(range(min(limit, len(ds))))
        examples = [
            Example(text=r[cfg["text"]], label=names[int(r[cfg["label"]])])
            for r in rows
        ]
        return examples, tuple(names)

    raise SystemExit(f"unknown dataset spec {spec!r} "
                     f"(expected synthetic, jsonl:<path>, {', '.join(HF_DATASETS)})")


# ---------------------------------------------------------------------------
# Audit runner
# ---------------------------------------------------------------------------


@dataclass
class AuditReport:
    adapter: str
    dataset: str
    verdict: GateVerdict
    curve: list[ReliabilityBin]
    protocol_errors: int
    generated_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "adapter": self.adapter,
            "dataset": self.dataset,
            "n": self.verdict.n,
            "accuracy": round(self.verdict.accuracy, 4),
            "ece": round(self.verdict.ece, 4),
            "mce": round(self.verdict.mce, 4),
            "brier": round(self.verdict.brier, 4),
            "gate": {
                "passed": self.verdict.passed,
                "reasons": list(self.verdict.reasons),
            },
            "protocol_errors": self.protocol_errors,
            "reliability_curve": [
                {"bin": f"{b.lo:.1f}-{b.hi:.1f}", "count": b.count,
                 "avg_confidence": round(b.avg_confidence, 4),
                 "accuracy": round(b.accuracy, 4)}
                for b in self.curve
            ],
        }


def run_audit(
    adapter,
    examples: list[Example],
    options: tuple[str, ...],
    *,
    dataset_name: str = "?",
    n_bins: int = 10,
    ece_threshold: float = 0.05,
    min_samples: int = 100,
    question_text: str = "Which category does this text belong to?",
) -> AuditReport:
    """Drive the adapter over the examples; score calibration; gate it."""
    confidences: list[float] = []
    correct: list[bool] = []
    protocol_errors = 0
    for i, ex in enumerate(examples):
        q = Question(id=f"q{i}", text=question_text, options=options)
        try:
            answers = adapter.decide(ex.text, [q], gold={q.id: ex.label})
        except ProtocolError:
            protocol_errors += 1
            continue
        answer = validate_answer(answers[0], q)
        confidences.append(answer.confidence)
        correct.append(answer.choice == ex.label)
    verdict, curve = gate_verdict(
        confidences, correct,
        n_bins=n_bins, ece_threshold=ece_threshold, min_samples=min_samples,
    )
    return AuditReport(
        adapter=adapter.name,
        dataset=dataset_name,
        verdict=verdict,
        curve=curve,
        protocol_errors=protocol_errors,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--adapter", choices=["mock", "ollama", "external"],
                    default="mock")
    ap.add_argument("--endpoint", default=None,
                    help="external typed-decision endpoint URL (stub seam; "
                         "requires credential + explicit approval — see "
                         "ExternalEndpointStub)")
    ap.add_argument("--model", default="llama3.2", help="ollama model name")
    ap.add_argument("--ollama-url", default="http://localhost:11434")
    ap.add_argument("--dataset", default="synthetic",
                    help="synthetic | jsonl:<path> | ag_news | trec6")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--bins", type=int, default=10)
    ap.add_argument("--ece-threshold", type=float, default=0.05)
    ap.add_argument("--min-samples", type=int, default=100)
    ap.add_argument("--seed", type=int, default=20260919)
    ap.add_argument("--mock-accuracy", type=float, default=0.8)
    ap.add_argument("--mock-confidence", type=float, default=0.8)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    if args.adapter == "mock":
        adapter = MockAdapter(seed=args.seed, accuracy=args.mock_accuracy,
                              stated_confidence=args.mock_confidence)
    elif args.adapter == "ollama":
        adapter = OllamaReadoutAdapter(base_url=args.ollama_url, model=args.model)
    else:
        if not args.endpoint:
            ap.error("--adapter external requires --endpoint")
        adapter = ExternalEndpointStub(endpoint=args.endpoint)

    examples, options = load_examples(args.dataset, limit=args.limit,
                                      seed=args.seed)
    report = run_audit(
        adapter, examples, options,
        dataset_name=args.dataset, n_bins=args.bins,
        ece_threshold=args.ece_threshold, min_samples=args.min_samples,
    )
    text = json.dumps(report.to_dict(), indent=2)
    print(text)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"\nwrote {args.out}", file=sys.stderr)
    return 0 if report.verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
