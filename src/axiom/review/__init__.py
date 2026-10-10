# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Unified human-in-the-loop review framework.

Provides a channel-agnostic data model and interactive CLI runner for
reviewing any kind of AI-generated output: draft documents, STT corrections,
action proposals, etc.

Architecture:
    ReviewItem / ReviewSession / ReviewSessionStore  (models.py)
    ReviewAdapter protocol + ReviewRunner            (runner.py)
    Domain-specific adapters                         (adapters/)
"""
