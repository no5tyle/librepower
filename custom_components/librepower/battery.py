# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""The contract between core and any battery adapter repo.

This is the boundary that makes the multi-repo split possible. Core never
imports a brand-specific module (no ``from .powerwall import ...`` here or
anywhere else in core after this change) - it only knows about the shapes
defined in this file. A battery adapter repo (e.g. ``librepower-powerwall``)
depends on core (``"dependencies": ["librepower"]`` in its manifest.json),
imports these types, and constructs/raises them.

Cross-repo import mechanism, and its honest limitation
------------------------------------------------------
Home Assistant loads every custom_component under the shared
``custom_components`` namespace package, so a dependent integration doing
``from custom_components.librepower.battery import BatteryClient`` works in
practice, and manifest ``dependencies`` guarantees core loads first. This is
an established community pattern (several HACS ecosystems do exactly this),
but it is not an HA-core-blessed stable API - it relies on custom_components
being important-by-path, which is true today but isn't a documented
guarantee. Worth re-verifying if a future HA release changes integration
loading. This has not been tested against a live two-integration HA install;
see the battery repo's own README for what still needs real-hardware
verification.

Why physical specs live in the adapter, not here
-----------------------------------------------------
Capacity and max charge/discharge power are properties of the *battery*, not
the optimiser. A Sigenergy install and a Powerwall install have different
numbers; asking for them in core's own setup, disconnected from which
battery is actually registered, was the wrong place for that config to live.
``async_get_capabilities()`` exists so the adapter reports its own numbers
(typically user-entered during the adapter's setup, since most local battery
protocols - pypowerwall included - don't expose nameplate capacity directly)
rather than the user re-entering them into core blind.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class BatteryError(Exception):
    """Base error for any battery adapter."""


class BatteryAuthError(BatteryError):
    """The adapter's credentials were rejected."""


class BatteryUnreachableError(BatteryError):
    """The battery could not be contacted."""


class BatteryReadOnlyError(BatteryError):
    """A write was attempted while the adapter is in shadow/read-only mode."""


class BatteryControlUnavailableError(BatteryError):
    """Control is enabled but this adapter cannot currently write.

    Generalises what was ``PowerwallV1rRequiredError`` - the Powerwall
    adapter raises a subclass of this for its specific reason (missing v1r
    pairing); a different adapter would raise this same base type for its
    own reason. Core's coordinator catches the base type and doesn't need to
    know which adapter or which specific cause applies.
    """


@dataclass(slots=True)
class BatterySnapshot:
    """One coherent read of the battery system's live state.

    All power values are Watts, signed from the *site's* perspective:
      - ``grid_w``    positive = importing, negative = exporting
      - ``battery_w`` positive = discharging, negative = charging
      - ``solar_w``   always >= 0
      - ``load_w``    always >= 0
    """

    soc: float  # 0.0 - 1.0
    solar_w: float
    battery_w: float
    grid_w: float
    load_w: float
    grid_connected: bool = True

    @property
    def soc_pct(self) -> float:
        return self.soc * 100.0


@dataclass(frozen=True, slots=True)
class BatteryCapabilities:
    """The physical limits an adapter reports about its own battery."""

    capacity_wh: float
    max_charge_w: float
    max_discharge_w: float


@runtime_checkable
class BatteryClient(Protocol):
    """What core requires of any registered battery adapter.

    ``@runtime_checkable`` allows ``isinstance(client, BatteryClient)`` as a
    structural check - useful at registration time to fail loudly if an
    adapter doesn't actually satisfy the contract, rather than failing later
    with an AttributeError deep in the coordinator.
    """

    @property
    def read_only(self) -> bool: ...

    async def async_get_snapshot(self) -> BatterySnapshot: ...

    async def async_get_capabilities(self) -> BatteryCapabilities: ...

    async def async_set_backup_reserve(self, reserve: float) -> None: ...

    async def async_set_grid_export(self, rule: str) -> None: ...

    async def async_close(self) -> None: ...
