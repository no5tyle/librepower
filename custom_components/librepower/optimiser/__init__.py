# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Battery scheduling optimiser.

Provenance
----------
``engine.py`` is vendored from ``bolagnaise/powersync-optimiser`` (MIT,
Copyright (c) 2024 Ben Boller). The upstream licence is retained verbatim in
``LICENSE.upstream`` and must stay with this code in any distribution — that
is the MIT licence's one real obligation.

Vendored rather than pip-installed on purpose: upstream ships it as a Flask
service in a Docker add-on, and we want the solver in-process inside the HA
integration. Keep local modifications documented in MODIFICATIONS.md so the
delta against upstream stays auditable.

Why this engine and not PowerSync's
-----------------------------------
PowerSync's optimiser is PolyForm Noncommercial — fine to fork for personal
use, unusable for anything distributable. This one is MIT, is ~940 lines
instead of ~9,300, models degradation cost explicitly via ``cycle_cost``, and
expresses the LP through cvxpy rather than hand-built constraint matrices.
Less to understand, more to build on.
"""
from __future__ import annotations

from .engine import (
    BatteryOptimiser,
    CostFunction,
    OptimizationConfig,
    OptimizationResult,
)

__all__ = [
    "BatteryOptimiser",
    "CostFunction",
    "OptimizationConfig",
    "OptimizationResult",
]
