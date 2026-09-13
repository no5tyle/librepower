# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""The one coordinator.

PowerSync's equivalent file is ~900KB. This one is intentionally small, and the
constraint that keeps it small is a rule: **this class orchestrates, it does not
compute.** Telemetry comes from whatever battery adapter registers (see
battery.py for the contract - core itself imports no brand-specific module),
prices from ``pricing/``, forecasting from ``load_forecast.py``, the LP from
``optimiser/``. If this file starts growing a solver or a device protocol,
that logic is in the wrong place.

No battery until one registers
-------------------------------
Since the repo split, core no longer constructs a battery client itself - it
starts with none, and a separate battery-adapter integration (e.g.
``librepower-powerwall``) calls ``async_set_battery()`` once it has connected.
Every method that needs a battery checks for this and fails informatively
rather than crashing; ``control_mode`` reports ``"waiting"`` until then.

Two update loops, deliberately separate:
  - fast: telemetry, every 30s, feeds the sensors and the load forecaster
  - slow: prices + re-solve, every 5 min, on a receding horizon
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .battery import (
    BatteryAlreadyRegisteredError,
    BatteryClient,
    BatteryControlUnavailableError,
    BatteryError,
    BatteryReadOnlyError,
    BatterySnapshot,
)
from .const import (
    ACTION_CHARGE,
    ACTION_DISCHARGE,
    ACTION_EXPORT,
    ACTION_IDLE,
    ACTION_SELF_CONSUMPTION,
    DOMAIN,
    OPTIMISE_HORIZON_HOURS,
    OPTIMISE_INTERVAL_MINUTES,
    SOLAR_FORECAST_SOURCE_CLIMATOLOGY,
    SOLAR_FORECAST_SOURCE_OPEN_METEO,
    UPDATE_INTERVAL_TELEMETRY,
)
from .load_forecast import LoadForecaster
from .open_meteo import OpenMeteoClient, OpenMeteoError
from .optimiser import BatteryOptimiser, OptimizationConfig, OptimizationResult
from .pricing import PriceForecast, PricingError, PricingProvider
from .solar_forecast import HistoricalSolarForecaster
from .solar_geometry import clear_sky_ghi_estimate

_LOGGER = logging.getLogger(__name__)

SLOT_COUNT = int(OPTIMISE_HORIZON_HOURS * 60 / OPTIMISE_INTERVAL_MINUTES)


@dataclass(slots=True)
class LibrePowerData:
    """Everything the entities render. One object, replaced atomically."""

    snapshot: BatterySnapshot | None = None
    prices: PriceForecast | None = None
    plan: OptimizationResult | None = None
    plan_created: datetime | None = None
    current_action: str = ACTION_SELF_CONSUMPTION
    last_error: str | None = None
    # "waiting" until a battery adapter registers, then "shadow" or "active".
    control_mode: str = "waiting"
    solar_forecast_source: str = SOLAR_FORECAST_SOURCE_CLIMATOLOGY

    @property
    def has_plan(self) -> bool:
        return self.plan is not None and self.plan.success


class LibrePowerCoordinator(DataUpdateCoordinator[LibrePowerData]):
    """Polls the registered battery, refreshes prices, and re-solves the schedule."""

    def __init__(
        self,
        hass: HomeAssistant,
        pricing: PricingProvider,
        optimiser_config: OptimizationConfig,
        loads: LoadForecaster,
        solar: HistoricalSolarForecaster,
        latitude: float,
        longitude: float,
        open_meteo: OpenMeteoClient | None = None,
        weather_aware_solar: bool = False,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL_TELEMETRY,
        )
        # No battery at construction time - core doesn't know about any
        # specific brand. A battery adapter integration calls
        # async_set_battery() once it has connected; see battery.py.
        self._battery: BatteryClient | None = None
        self._pricing = pricing
        self._optimiser = BatteryOptimiser(optimiser_config)
        self._opt_config = optimiser_config
        # Both forecasters are constructed by __init__.py (which restores
        # persisted state from storage.py before handing them over) rather
        # than here - this class orchestrates their use, it doesn't own their
        # lifecycle. See the loads/solar properties below: __init__.py reads
        # them back out on a save timer via the same objects, not a copy.
        self._loads = loads
        self._solar = solar
        self._lat = latitude
        self._lon = longitude
        self._open_meteo = open_meteo
        self._weather_aware_solar = weather_aware_solar
        self._last_control_warning: datetime | None = None
        self.data = LibrePowerData()

    @property
    def loads(self) -> LoadForecaster:
        """Exposed so __init__.py can persist learned state on a save timer."""
        return self._loads

    @property
    def solar(self) -> HistoricalSolarForecaster:
        """Exposed so __init__.py can persist learned state on a save timer."""
        return self._solar

    @property
    def battery_ready(self) -> bool:
        return self._battery is not None

    async def async_set_battery(self, battery: BatteryClient) -> None:
        """Called by a battery adapter integration once it has connected.

        Raises BatteryAlreadyRegisteredError on a second call - see
        battery.py's docstring on why multiple physical batteries per core
        entry isn't supported yet, and why this is a loud failure rather
        than silently discarding the first registration.

        Fetches the adapter's reported physical capabilities (capacity, max
        charge/discharge) and applies them to the optimiser config - these
        are properties of the specific battery hardware, not something core
        should be asking the user to type in blind. See battery.py's
        docstring for why this is user-entered in the adapter, not
        auto-detected from the device.
        """
        if self._battery is not None:
            raise BatteryAlreadyRegisteredError(
                "A battery is already registered with this LibrePower "
                "instance. Multiple batteries per core entry are not yet "
                "supported - use a separate LibrePower core instance per "
                "battery for now."
            )
        self._battery = battery
        try:
            caps = await battery.async_get_capabilities()
        except BatteryError as err:
            _LOGGER.warning(
                "Battery registered but capabilities unavailable, keeping "
                "existing optimiser limits: %s",
                err,
            )
        else:
            self._opt_config.battery_capacity_wh = caps.capacity_wh
            self._opt_config.max_charge_w = caps.max_charge_w
            self._opt_config.max_discharge_w = caps.max_discharge_w
            self._opt_config.charge_efficiency = caps.charge_efficiency
            self._opt_config.discharge_efficiency = caps.discharge_efficiency
        # Kick the fast loop immediately rather than waiting up to 30s for
        # the next scheduled tick - the person just finished setup and
        # entities should populate right away.
        await self.async_request_refresh()

    @property
    def _control_mode(self) -> str:
        """Whether we are permitted to write to the battery."""
        if self._battery is None:
            return "waiting"
        return "shadow" if self._battery.read_only else "active"

    # -- fast loop ------------------------------------------------------------

    async def _async_update_data(self) -> LibrePowerData:
        """Telemetry tick. Must stay cheap — it runs every 30 seconds."""
        if self._battery is None:
            raise UpdateFailed(
                "No battery adapter registered yet - install a LibrePower "
                "battery integration (e.g. librepower-powerwall) and "
                "complete its setup."
            )

        try:
            snapshot = await self._battery.async_get_snapshot()
        except BatteryError as err:
            raise UpdateFailed(f"Battery read failed: {err}") from err

        now = datetime.now(timezone.utc)
        self._loads.observe(now, snapshot.load_w)
        self._solar.observe(now, snapshot.solar_w)

        previous = self.data or LibrePowerData()
        return LibrePowerData(
            snapshot=snapshot,
            prices=previous.prices,
            plan=previous.plan,
            plan_created=previous.plan_created,
            current_action=self._action_now(previous.plan, previous.plan_created),
            last_error=previous.last_error,
            control_mode=self._control_mode,
            solar_forecast_source=previous.solar_forecast_source,
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
        solar, solar_source = await self._async_solar_forecast(start)
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
                solar_forecast_source=solar_source,
            )
        )

        # Executing is best-effort and must never take down the coordinator:
        # a write failure means "the plan didn't reach the battery this tick",
        # not "the integration is broken". The next tick tries again.
        try:
            await self._async_execute_action(
                self._action_now(plan, created), plan, created, snapshot
            )
        except BatteryReadOnlyError:
            pass  # expected and silent in shadow mode; no need to log every tick
        except BatteryControlUnavailableError as err:
            # Distinct from a transient failure: this will not resolve on its
            # own on the next tick, so warn once per hour rather than every
            # 5 minutes, and say what actually needs to happen.
            if self._last_control_warning is None or (
                datetime.now(timezone.utc) - self._last_control_warning
            ).total_seconds() > 3600:
                _LOGGER.warning(
                    "Battery control unavailable: %s. Control mode is 'active' "
                    "but writes cannot reach the battery.",
                    err,
                )
                self._last_control_warning = datetime.now(timezone.utc)
            self._record_error(f"Control unavailable: {err}")
        except BatteryError as err:
            _LOGGER.warning("Could not apply plan to battery: %s", err)
            self._record_error(f"Control write failed: {err}")

    async def _async_execute_action(
        self,
        action: str,
        plan: OptimizationResult,
        created: datetime,
        snapshot: BatterySnapshot,
    ) -> None:
        """Translate the current plan slot into a disposition command.

        Every adapter is required to support async_charge/discharge/hold/
        release, each taking a target SOC where relevant (see battery.py's
        docstring for why a target, not a bare direction) - core decides
        *what* it wants, the adapter decides *how* to achieve it on its own
        hardware (e.g. Powerwall's async_charge pads the reserve above
        current SOC internally to force grid charge; core doesn't need to
        know that's how Powerwall does it).

        Export policy (curtail_export/allow_export) is a separate channel -
        not called from here yet. The LP has no curtailment signal of its
        own to drive it; see optimiser/MODIFICATIONS.md item 6.
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

        if action == ACTION_CHARGE:
            await self._battery.async_charge(target_soc)
        elif action in (ACTION_DISCHARGE, ACTION_EXPORT):
            await self._battery.async_discharge(target_soc)
        elif action == ACTION_SELF_CONSUMPTION:
            # Not "discharge toward a target" - self-consumption means don't
            # actively hold or force anything, let the battery serve load
            # naturally. release() is the correct signal for that, distinct
            # from hold(): hold pins a level, release stops overriding.
            await self._battery.async_release()
        else:  # ACTION_IDLE
            await self._battery.async_hold(snapshot.soc)

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

    async def _async_solar_forecast(
        self, start: datetime
    ) -> tuple[list[float], str]:
        """Solar forecast in Watts, plus which source actually produced it.

        The history-based model (self._solar) always runs - it's zero-config,
        zero-network, and is exactly what should come out when weather-aware
        mode is off or Open-Meteo is unreachable. When weather-aware mode is
        on and Open-Meteo succeeds, its forecast GHI is turned into a
        clearness index (forecast GHI / clear-sky GHI estimate, both using
        the same crude clear-sky model - see solar_geometry.py for why that
        consistency is what makes a crude model fine) and passed in as a
        weather adjustment. The history model still does all the site
        calibration; Open-Meteo only adjusts for today's actual weather.
        """
        clearness_index = None
        source = SOLAR_FORECAST_SOURCE_CLIMATOLOGY

        if self._weather_aware_solar and self._open_meteo is not None:
            try:
                ghi_forecast = await self._open_meteo.async_get_ghi_forecast(
                    OPTIMISE_HORIZON_HOURS
                )
                clearness_index = self._build_clearness_index(ghi_forecast, start)
                source = SOLAR_FORECAST_SOURCE_OPEN_METEO
            except OpenMeteoError as err:
                _LOGGER.warning(
                    "Open-Meteo unavailable, falling back to history-only "
                    "solar forecast: %s",
                    err,
                )

        forecast = self._solar.forecast(start, SLOT_COUNT, clearness_index=clearness_index)
        return forecast, source

    def _build_clearness_index(
        self, ghi_forecast: list[tuple[datetime, float]], start: datetime
    ) -> list[float]:
        """Map Open-Meteo's hourly GHI onto our slot grid as a clearness ratio.

        Open-Meteo returns hourly points; our slots are OPTIMISE_INTERVAL_MINUTES
        apart. Same forward-hold approach as PriceForecast.resample: advance to
        the most recent Open-Meteo hour at or before each slot's timestamp,
        rather than interpolating - a preceding-hour mean shouldn't be
        smoothed into something falsely more precise than it is.
        """
        if not ghi_forecast:
            return [1.0] * SLOT_COUNT

        step = timedelta(minutes=OPTIMISE_INTERVAL_MINUTES)
        result: list[float] = []
        cursor = 0
        for i in range(SLOT_COUNT):
            slot_time = start + step * i
            while (
                cursor + 1 < len(ghi_forecast)
                and ghi_forecast[cursor + 1][0] <= slot_time
            ):
                cursor += 1
            _, ghi = ghi_forecast[cursor]
            clear_sky = clear_sky_ghi_estimate(slot_time, self._lat, self._lon)
            if clear_sky <= 0.0:
                result.append(1.0)  # sun is down; kt is meaningless, forecast will be 0 regardless
            else:
                result.append(ghi / clear_sky)
        return result

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
                solar_forecast_source=current.solar_forecast_source,
            )
        )

    async def async_shutdown_client(self) -> None:
        if self._battery is not None:
            await self._battery.async_close()
