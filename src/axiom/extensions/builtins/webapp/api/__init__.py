# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""FastAPI routers for the ``webapp`` ``/api/v1`` surface."""

from .routers import build_api_router

__all__ = ["build_api_router"]
