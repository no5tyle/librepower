# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""The one coordinator.

PowerSync's equivalent file is ~900KB. This one is intentionally small, and the
constraint that keeps it small is a rule: **this class orchestrates, it does not
compute.** Telemetry lives in ``powerwall.py``, prices in ``pricing/``,
forecasting in ``load_forecast.py``, the LP in ``optimiser/``. If this file
starts growing a solver or a protocol, that logic is in the wrong place.

Two update loops, deliberately separate:
  - fast: telemetry, every 30s, feeds the sensors and the load forecaster
  - slow: prices + re-solve, every 5 min, on a receding horizon
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTION_EXPORT,
    ACTION_IDLE,
    ACTION_SELF_CONSUMPTION,
    DOMAIN,
    OPTIMISE_HORIZON_HOURS,
    OPTIMISE_INTERVAL_MINUTES,
    UPDATE_INTERVAL_TELEMETRY,
)
from .load_forecast import LoadForecaster
from .optimiser import BatteryOptimiser, OptimizationConfig, OptimizationResult
from .powerwall import (
    PowerwallClient,
    PowerwallError,
    PowerwallReadOnlyError,
    PowerwallSnapshot,
    PowerwallV1rRequiredError,
)
from .pricing import PriceForecast, PricingError, PricingProvider

_LOGGER = logging.getLogger(__name__)

SLOT_COUNT = int(OPTIMISE_HORIZON_HOURS * 60 / OPTIMISE_INTERVAL_MINUTES)


@dataclass(slots=True)
class LibrePowerData:
    """Everything the entities render. One object, replaced atomically."""

    snapshot: PowerwallSnapshot | None = None
    prices: PriceForecast | None = None
    plan: OptimizationResult | None = None
    plan_created: datetime | None = None
    current_action: str = ACTION_SELF_CONSUMPTION
    last_error: str | None = None
    control_mode: str = "shadow"

    @property
    def has_plan(self) -> bool:
        return self.plan is not None and self.plan.success


class LibrePowerCoordinator(DataUpdateCoordinator[LibrePowerData]):
    """Polls the Powerwall, refreshes prices, and re-solves the schedule."""

    def __init__(
        self,
        hass: HomeAssistant,
        powerwall: PowerwallClient,
        pricing: PricingProvider,
        optimiser_config: OptimizationConfig,
        solar_forecaster=None,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL_TELEMETRY,
        )
        self._powerwall = powerwall
        self._pricing = pricing
        self._optimiser = BatteryOptimiser(optimiser_config)
        self._opt_config = optimiser_config
        self._solar = solar_forecaster
        self._loads = LoadForecaster(OPTIMISE_INTERVAL_MINUTES)
        self._last_v1r_warning: datetime | None = None
        self.data = LibrePowerData()

    @property
    def _control_mode(self) -> str:
        """Whether we are permitted to write to the battery."""
        return "shadow" if self._powerwall.read_only else "active"

    # -- fast loop ------------------------------------------------------------

    async def _async_update_data(self) -> LibrePowerData:
        """Telemetry tick. Must stay cheap — it runs every 30 seconds."""
        try:
            snapshot = await self._powerwall.async_get_snapshot()
        except PowerwallError as err:
            raise UpdateFailed(f"Powerwall read failed: {err}") from err

        now = datetime.now(timezone.utc)
        self._loads.observe(now, snapshot.load_w)

        previous = self.data or LibrePowerData()
        return LibrePowerData(
            snapshot=snapshot,
            prices=previous.prices,
            plan=previous.plan,
            plan_created=previous.plan_created,
            current_action=self._action_now(previous.plan, previous.plan_created),
            last_error=previous.last_error,
            control_mode=self._control_mode,
        )

    # -- slow loop ------------------------------------------------------------

    async def async_refresh_plan(self, _now=None) -> None:
        """Fetch prices and re-solve. Scheduled separately from telemetry."""
        snapshot = self.data.snapshot if self.data else None
        if snapshot is None:
            _LOGGER.debug("Skipping re-solve: no telemetry yet")
            return

        try:
            prices = await self._pricing.async_get_forecast(OPTIMISE_HORIZON_HOURS)
        except PricingError as err:
            _LOGGER.warning("Price refresh failed, keeping previous plan: %s", err)
            self._record_error(f"Prices unavailable: {err}")
            return

        try:
            imports, exports = prices.resample(OPTIMISE_INTERVAL_MINUTES, SLOT_COUNT)
        except PricingError as err:
            self._record_error(str(err))
            return

        start = prices.start or datetime.now(timezone.utc)
        solar = await self._async_solar_forecast(start)
        load = self._loads.forecast(start, SLOT_COUNT)

        # The LP is pure CPU and can take ~1s. Never run it on the event loop.
        plan = await self.hass.async_add_executor_job(
            self._solve, imports, exports, solar, load, snapshot.soc, start
        )

        if not plan.success:
            _LOGGER.warning("Optimiser returned no schedule: %s", plan.status)
            self._record_error(f"Optimiser: {plan.status}")
            return

        created = datetime.now(timezone.utc)
        self.async_set_updated_data(
            LibrePowerData(
                snapshot=snapshot,
                prices=prices,
                plan=plan,
                plan_created=created,
                current_action=self._action_now(plan, created),
                last_error=None,
                control_mode=self._control_mode,
            )
        )

        # Executing is best-effort and must never take down the coordinator:
        # a write failure means "the plan didn't reach the battery this tick",
        # not "the integration is broken". The next tick tries again.
        try:
            await self._async_execute_action(
                self._action_now(plan, created), plan, created, snapshot
            )
        except PowerwallReadOnlyError:
            pass  # expected and silent in shadow mode; no need to log every tick
        except PowerwallV1rRequiredError as err:
            # Distinct from a transient failure: this will not resolve on its
            # own on the next tick, so warn once per hour rather than every
            # 5 minutes, and say what actually needs to happen.
            if self._last_v1r_warning is None or (
                datetime.now(timezone.utc) - self._last_v1r_warning
            ).total_seconds() > 3600:
                _LOGGER.warning(
                    "Battery control unavailable: %s. Control mode is 'active' "
                    "but writes cannot reach the battery.",
                    err,
                )
                self._last_v1r_warning = datetime.now(timezone.utc)
            self._record_error("Control enabled, but v1r pairing is required")
        except PowerwallError as err:
            _LOGGER.warning("Could not apply plan to battery: %s", err)
            self._record_error(f"Control write failed: {err}")

    async def _async_execute_action(
        self,
        action: str,
        plan: OptimizationResult,
        created: datetime,
        snapshot: PowerwallSnapshot,
    ) -> None:
        """Translate the current plan slot into the one lever we pull.

        Backup reserve is deliberately the only control surface:
          - reserve above current SOC  -> holds / charges toward it
          - reserve at the floor       -> permits discharge for load and export
        This can't express every nuance the LP computed (it doesn't itself
        command a charge *rate*), but it's the same lever PowerSync uses for
        the same reason: it's the one write every Powerwall generation and
        firmware version actually honours. Chase more granular control later
        only if reserve-only proves insufficient in practice — not before.
        """
        index = self._slot_index(created)
        if index is None:
            return
        # soc_trajectory[0] is the starting SOC; soc_trajectory[i+1] is the
        # SOC the LP plans to reach *after* slot i. That's the target for
        # slot i, not soc_trajectory[i] — confirmed against the vendored
        # engine's own get_action_at_index, which makes the same +1 shift.
        target_index = index + 1
        if target_index >= len(plan.soc_trajectory):
            return

        target_soc = plan.soc_trajectory[target_index]

        if action in (ACTION_CHARGE,):
            # Hold above current SOC so the battery is *forced* to take grid
            # charge rather than merely being allowed to.
            reserve = max(target_soc, snapshot.soc)
        elif action in (ACTION_DISCHARGE, ACTION_EXPORT, ACTION_SELF_CONSUMPTION):
            # Release down to the LP's planned floor for this slot.
            reserve = target_soc
        else:  # ACTION_IDLE
            reserve = snapshot.soc

        await self._powerwall.async_set_backup_reserve(reserve)

    def _slot_index(self, created: datetime) -> int | None:
        if created is None:
            return None
        elapsed = (datetime.now(timezone.utc) - created).total_seconds() / 60.0
        index = int(elapsed // OPTIMISE_INTERVAL_MINUTES)
        return index if index >= 0 else None

    def _solve(
        self,
        imports: list[float],
        exports: list[float],
        solar: list[float],
        load: list[float],
        soc: float,
        start: datetime,
    ) -> OptimizationResult:
        """Blocking. Runs in executor."""
        return self._optimiser.optimize(
            prices_import=imports,
            prices_export=exports,
            solar_forecast=solar,
            load_forecast=load,
            initial_soc=soc,
            start_time=start,
            config=self._opt_config,
        )

    async def _async_solar_forecast(self, start: datetime) -> list[float]:
        """Solar forecast in Watts, or zeros if none is configured.

        Zeros are the safe default: the optimiser then assumes it must buy all
        energy, which produces conservative (never over-committed) schedules.
        """
        if self._solar is None:
            return [0.0] * SLOT_COUNT
        try:
            return await self._solar.async_forecast(
                start, SLOT_COUNT, OPTIMISE_INTERVAL_MINUTES
            )
        except Exception as err:  # noqa: BLE001 - forecast is optional
            _LOGGER.warning("Solar forecast unavailable, assuming none: %s", err)
            return [0.0] * SLOT_COUNT

    # -- plan interpretation --------------------------------------------------

    def _action_now(
        self, plan: OptimizationResult | None, created: datetime | None
    ) -> str:
        """Which slot of the plan applies right now.

        Read fresh each tick rather than cached, so a stale plan still tracks
        wall-clock time correctly until the next re-solve replaces it.
        """
        if plan is None or created is None or not plan.charge_schedule_w:
            return ACTION_SELF_CONSUMPTION

        elapsed = (datetime.now(timezone.utc) - created).total_seconds() / 60.0
        index = int(elapsed // OPTIMISE_INTERVAL_MINUTES)
        if index < 0 or index >= len(plan.charge_schedule_w):
            return ACTION_SELF_CONSUMPTION

        charge = plan.charge_schedule_w[index]
        export = (
            plan.battery_export_w[index]
            if index < len(plan.battery_export_w)
            else 0.0
        )
        discharge = plan.discharge_schedule_w[index]

        if charge > 50.0:
            return ACTION_CHARGE
        if export > 50.0:
            return ACTION_EXPORT
        if discharge > 50.0:
            return ACTION_SELF_CONSUMPTION
        return ACTION_IDLE

    def _record_error(self, message: str) -> None:
        current = self.data or LibrePowerData()
        self.async_set_updated_data(
            LibrePowerData(
                snapshot=current.snapshot,
                prices=current.prices,
                plan=current.plan,
                plan_created=current.plan_created,
                current_action=current.current_action,
                last_error=message,
                control_mode=self._control_mode,
            )
        )

    async def async_shutdown_client(self) -> None:
        await self._powerwall.async_close()
