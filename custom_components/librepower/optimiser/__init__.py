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
instead of ~9,300, and models degradation cost explicitly via ``cycle_cost``.

It originally expressed the LP through cvxpy rather than hand-built
constraint matrices - simpler to read and extend. That's since been replaced
with a hand-built ``scipy.optimize.milp`` (HiGHS) formulation of the same
problem: cvxpy's default solver stack (osqp, clarabel, qdldl, sparsediffpy)
publishes no musllinux or 32-bit-ARM wheels, and a Home Assistant container
has no C/C++/Rust toolchain to build them from source, so it could never
install on those hosts. scipy has far broader wheel coverage and already
bundles HiGHS. See MODIFICATIONS.md for the detail.
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
