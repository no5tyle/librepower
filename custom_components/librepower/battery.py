# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""The published contract between core and any battery adapter repo.

This is the boundary that makes the multi-repo split possible, and the
interface documented in the top-level README's "Battery adapter interface"
section for anyone building support for a new brand. Core never imports a
brand-specific module - it only knows about the shapes defined here. A
battery adapter repo (e.g. ``librepower-powerwall``) depends on core
(``"dependencies": ["librepower"]`` in its manifest.json), imports these
types, and constructs/raises them.

Cross-repo import mechanism, and its honest limitation
------------------------------------------------------
Home Assistant loads every custom_component under the shared
``custom_components`` namespace package, so a dependent integration doing
``from custom_components.librepower.battery import BatteryClient`` works in
practice, and manifest ``dependencies`` guarantees core loads first. This is
an established community pattern, but it is not an HA-core-blessed stable
API - it relies on custom_components being importable-by-path, which is true
today but isn't a documented guarantee.

Two independent command channels
---------------------------------
Battery disposition (charge/discharge/hold/release) and export policy
(curtail/allow) are deliberately separate, not one combined enum. They
compose: charging the battery *and* curtailing export at the same time is a
real scenario (soak up excess solar during a negative-price event without any
of it leaking onto the grid), and a single five-way state can't express that.

Curtailment levels are named by effect, not mechanism, on purpose - "soft"
and "strong" instead of Tesla's own "battery_ok/pv_only/never" vocabulary,
because that vocabulary is Tesla-specific and doesn't map cleanly onto every
brand's actual export-control primitive (some only expose a numeric export
cap, not a categorical mode). Each adapter picks its own mechanism:

  - soft:   stay grid-connected, just forbid export (Powerwall: export
            rule = never - the Gateway curtails production internally
            since there's nowhere for the surplus to go)
  - strong: curtail by any means necessary, including full islanding if
            that's what it takes (Powerwall: go_off_grid - last resort,
            no import either)

Targets, not bare signals
--------------------------
``async_charge``/``async_discharge``/``async_hold`` all take a target SOC
(0-1), not a bare direction. The core optimiser runs a real 48-hour linear
program specifically to work out *how much*; reducing that to a directionless
signal and asking every adapter to reinvent "how much is enough" would throw
away the actual reason to run an optimiser, and would very likely mean each
adapter quietly duplicating that logic anyway - the exact problem this split
exists to avoid. The real cost of this choice: a single target SOC per
re-solve still can't express *pacing* (gently over three hours vs immediately
at max rate) - a known, accepted limitation, not an oversight.

``async_release`` is distinct from ``async_hold``: hold pins the battery at a
level; release means "stop overriding, return to your own native automatic
behaviour" - important for a clean handoff when LibrePower is disabled or a
battery adapter is removed, so a battery doesn't get stuck at whatever level
was last commanded.

Why physical specs live in the adapter, not here
-----------------------------------------------------
Capacity, charge/discharge limits, and efficiency are properties of the
*battery*, not the optimiser. A Sigenergy install and a Powerwall install
have different numbers; asking for them in core's own setup, disconnected
from which battery is actually registered, was the wrong place for that
config to live. ``async_get_capabilities()`` exists so the adapter reports
its own numbers rather than the user re-entering them into core blind.

Efficiency specifically is a pre-measurement default, not a promise
---------------------------------------------------------------------
``charge_efficiency``/``discharge_efficiency`` are typically a manufacturer
nameplate figure the adapter reports at setup - better than one universal
0.90 assumed for every brand, but still a static guess. Real efficiency
degrades with age and shifts with rate and temperature. A future
telemetry-based learning feature is expected to supersede these fields once
enough observed charge/discharge history exists; that will need its own
design for how a learned value and an adapter-reported default reconcile,
deliberately not resolved here.

One battery per core entry, for now - and this is enforced, not aspirational
--------------------------------------------------------------------------------
Multiple *brands* (one repo each) is exactly what this split was built for.
Multiple *physical batteries combined under one site* is a different,
harder feature - real (Tesla sells multi-Powerwall systems), but out of
scope until a single battery is confirmed working end to end on real
hardware. ``async_set_battery`` raises ``BatteryAlreadyRegisteredError`` on a
second registration attempt rather than silently discarding the first -
loud and explicit until real multi-battery support exists. Two independent
core entries are today's workaround for two independently-managed batteries.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

CurtailmentLevel = Literal["soft", "strong"]


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

    Generic across every command method - any of async_charge/discharge/
    hold/release/curtail_export/allow_export can raise this. A specific
    adapter raises its own subclass for its specific reason (e.g. the
    Powerwall adapter's PowerwallV1rRequiredError for missing v1r pairing);
    core's coordinator catches the base type and doesn't need to know which
    adapter or which specific cause applies.
    """


class BatteryAlreadyRegisteredError(BatteryError):
    """A second battery tried to register against a core entry that already
    has one. See this module's docstring - multiple physical batteries per
    core entry is deliberately not supported yet, and this is the loud
    failure mode instead of silently discarding the first registration.
    """


@dataclass(slots=True)
class BatterySnapshot:
    """One coherent read of the battery system's live state.

    All power values are Watts, signed from the *site's* perspective:
      - ``grid_w``    positive = importing, negative = exporting
      - ``battery_w`` positive = discharging, negative = charging
      - ``solar_w``   always >= 0
      - ``load_w``    always >= 0

    This is the ground-truth feedback loop: whatever was last commanded,
    this is what the battery is *actually* doing right now. Core compares
    the two to notice drift or failure - it does not otherwise track
    whether a command "succeeded" beyond the write call not raising.
    """

    soc: float  # 0.0 - 1.0
    solar_w: float
    battery_w: float
    grid_w: float
    load_w: float
    timestamp: datetime  # when this reading was taken - must be tz-aware.
    grid_connected: bool = True
    # "ok" is the only value core currently treats specially (as in: not
    # faulted). Anything else is surfaced to the user as-is; adapters are
    # free to report a more specific string ("fault", "updating",
    # "comms_lost", ...) without core needing to know the full vocabulary
    # of any given brand.
    operational_status: str = "ok"
    # 0.0-1.0, 1.0 = no measurable degradation. None if this adapter's
    # hardware/protocol has no way to report it - not every brand can.
    state_of_health: float | None = None

    @property
    def soc_pct(self) -> float:
        return self.soc * 100.0


@dataclass(frozen=True, slots=True)
class BatteryCapabilities:
    """The physical limits and defaults an adapter reports about its battery.

    charge_efficiency/discharge_efficiency are a pre-measurement default -
    see this module's docstring section on why a static number here is
    provisional, not a long-term answer.
    """

    capacity_wh: float
    max_charge_w: float
    max_discharge_w: float
    charge_efficiency: float = 0.90
    discharge_efficiency: float = 0.90


@runtime_checkable
class BatteryClient(Protocol):
    """What core requires of any registered battery adapter.

    ``@runtime_checkable`` allows ``isinstance(client, BatteryClient)`` as a
    structural check - useful at registration time to fail loudly if an
    adapter doesn't actually satisfy the contract, rather than failing later
    with an AttributeError deep in the coordinator.

    Two independent command channels - see module docstring for why they're
    separate rather than one combined enum, and why charge/discharge/hold
    take a target SOC rather than being bare directional signals.
    """

    @property
    def read_only(self) -> bool: ...

    async def async_get_snapshot(self) -> BatterySnapshot: ...

    async def async_get_capabilities(self) -> BatteryCapabilities: ...

    # -- battery disposition channel -----------------------------------
    async def async_charge(self, target_soc: float) -> None: ...
    async def async_discharge(self, target_soc: float) -> None: ...
    async def async_hold(self, soc: float) -> None: ...
    async def async_release(self) -> None: ...

    # -- export policy channel, independent of disposition ---------------
    async def async_curtail_export(self, level: CurtailmentLevel) -> None: ...
    async def async_allow_export(self) -> None: ...

    async def async_close(self) -> None: ...
