# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Database infrastructure management for the domain consumer.

This module provides shared PostgreSQL + pgvector infrastructure used
across all consumer components (Sense, Chat, etc.).

Commands:
    axi db up        Start local K3D cluster with PostgreSQL
    axi db down      Stop local cluster
    axi db delete    Delete cluster and all data
    axi db status    Show cluster and database status
    axi db migrate   Run schema migrations
    axi db bootstrap Full setup from scratch
"""
