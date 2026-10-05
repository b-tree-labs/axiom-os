# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the accelerated batched embed core (no torch/model/DB)."""

from __future__ import annotations

import os

from axiom.extensions.builtins.data_platform.rag_embed.batch_embedder import (
    batch_embed_missing,
    effective_cpu_quota,
    pin_cpu_threads,
)


def test_effective_cpu_quota_positive():
    assert effective_cpu_quota() >= 1


def test_pin_cpu_threads_sets_omp_env_and_leaves_headroom(monkeypatch):
    for v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        monkeypatch.delenv(v, raising=False)
    applied = pin_cpu_threads(32, reserve=1)
    assert applied == 31  # leaves headroom
    assert os.environ["OMP_NUM_THREADS"] == "31"
    assert os.environ["MKL_NUM_THREADS"] == "31"


def test_pin_cpu_threads_floor_of_one(monkeypatch):
    for v in ("OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        monkeypatch.delenv(v, raising=False)
    assert pin_cpu_threads(1, reserve=4) == 1  # never below 1


def test_batch_embed_missing_embeds_all_with_prefix_and_progress():
    batches = [[(1, "alpha"), (2, "beta")], [(3, "gamma")]]
    seen_texts = []
    written = []
    progress = []

    def fetch():
        yield from batches

    def encode(texts):
        seen_texts.extend(texts)
        return [[float(len(t))] for t in texts]

    total = batch_embed_missing(
        fetch, encode, written.extend,
        doc_prefix="search_document: ",
        on_progress=progress.append,
    )

    assert total == 3
    assert seen_texts[0] == "search_document: alpha"  # prefix applied
    assert [cid for cid, _ in written] == [1, 2, 3]
    assert progress == [2, 3]  # cumulative per batch


def test_batch_embed_missing_skips_empty_batches():
    def fetch():
        yield []
        yield [(1, "x")]

    total = batch_embed_missing(fetch, lambda t: [[1.0]] * len(t), lambda rows: None)
    assert total == 1
