# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""LibrePower — local-first battery optimisation for Home Assistant."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .battery import BatteryClient
from .const import (
    CONF_BACKUP_RESERVE,
    CONF_BRIDGE_EXPORT_ENTITY,
    CONF_BRIDGE_FORECAST_ATTRIBUTE,
    CONF_BRIDGE_IMPORT_ENTITY,
    CONF_BRIDGE_PRICE_FIELD,
    CONF_BRIDGE_START_TIME_FIELD,
    CONF_CYCLE_COST,
    CONF_PROVIDER,
    CONF_WEATHER_AWARE_SOLAR,
    DEFAULT_BACKUP_RESERVE,
    DEFAULT_CYCLE_COST,
    DEFAULT_WEATHER_AWARE_SOLAR,
    DOMAIN,
    OPTIMISE_HORIZON_HOURS,
    OPTIMISE_INTERVAL_MINUTES,
    PROVIDER_ENTITY_BRIDGE,
    PROVIDER_FIXED_TARIFF,
    STORAGE_KEY_LOAD_HISTORY,
    STORAGE_KEY_SOLAR_HISTORY,
    STORAGE_SAVE_INTERVAL_MINUTES,
    UPDATE_INTERVAL_OPTIMISE,
)
from .coordinator import LibrePowerCoordinator
from .load_forecast import LoadForecaster
from .open_meteo import OpenMeteoClient
from .optimiser import OptimizationConfig
from .solar_forecast import HistoricalSolarForecaster
from .storage import LearningStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a LibrePower config entry.

    Since the repo split, core no longer connects to any specific battery
    here - it has no idea what brand is attached, or whether one is attached
    yet at all. A separate battery-adapter integration (e.g.
    ``librepower-powerwall``) does that, then calls
    ``coordinator.async_set_battery()`` once it has connected. See battery.py
    and coordinator.py's module docstring for the full contract.

    This means core's own setup can never fail because a battery isn't
    reachable - that's the adapter's problem to raise ConfigEntryNotReady
    over, not core's. Core's entities simply read "waiting" until a battery
    shows up; sensor.py already handles coordinator.data being sparse.
    """
    pricing = _build_pricing_client(hass, entry)

    latitude = hass.config.latitude
    longitude = hass.config.longitude

    # Learned history: restore from disk if present, start fresh otherwise.
    # A failed load never blocks startup - see storage.py.
    load_store = LearningStore(hass, entry.entry_id, STORAGE_KEY_LOAD_HISTORY)
    solar_store = LearningStore(hass, entry.entry_id, STORAGE_KEY_SOLAR_HISTORY)

    load_data = await load_store.async_load()
    loads = (
        LoadForecaster.from_dict(load_data, OPTIMISE_INTERVAL_MINUTES)
        if load_data
        else LoadForecaster(OPTIMISE_INTERVAL_MINUTES)
    )

    solar_data = await solar_store.async_load()
    solar = (
        HistoricalSolarForecaster.from_dict(
            solar_data, latitude, longitude, OPTIMISE_INTERVAL_MINUTES
        )
        if solar_data
        else HistoricalSolarForecaster(latitude, longitude, OPTIMISE_INTERVAL_MINUTES)
    )

    # Open-Meteo needs no key, but stays opt-in per this project's own rule
    # that cloud dependencies are never required by default - see const.py.
    weather_aware_solar = entry.options.get(
        CONF_WEATHER_AWARE_SOLAR, DEFAULT_WEATHER_AWARE_SOLAR
    )
    open_meteo = OpenMeteoClient(
        async_get_clientsession(hass), latitude=latitude, longitude=longitude
    )

    # battery_capacity_wh/max_charge_w/max_discharge_w are deliberately not
    # set here - they use the vendored engine's own placeholder defaults
    # until a battery registers and coordinator.async_set_battery() applies
    # its real reported BatteryCapabilities. See battery.py's docstring.
    optimiser_config = OptimizationConfig(
        backup_reserve=entry.options.get(
            CONF_BACKUP_RESERVE, DEFAULT_BACKUP_RESERVE
        ),
        cycle_cost=entry.options.get(CONF_CYCLE_COST, DEFAULT_CYCLE_COST),
        interval_minutes=OPTIMISE_INTERVAL_MINUTES,
        horizon_hours=OPTIMISE_HORIZON_HOURS,
    )

    coordinator = LibrePowerCoordinator(
        hass,
        pricing=pricing,
        optimiser_config=optimiser_config,
        loads=loads,
        solar=solar,
        latitude=latitude,
        longitude=longitude,
        open_meteo=open_meteo,
        weather_aware_solar=weather_aware_solar,
    )
    # Not async_config_entry_first_refresh() - that raises ConfigEntryNotReady
    # on failure, and "no battery has registered yet" is the normal state
    # right after core is first set up, not a setup failure. async_refresh()
    # logs and records the failure without raising; sensors read the
    # resulting "waiting" state until a battery adapter registers.
    await coordinator.async_refresh()

    # Solve once immediately so entities are populated before the first tick,
    # if a battery is already registered (e.g. HA restarted with the adapter
    # already configured). A no-op if not - async_refresh_plan bails cleanly
    # when there's no telemetry yet.
    await coordinator.async_refresh_plan()

    entry.async_on_unload(
        async_track_time_interval(
            hass, coordinator.async_refresh_plan, UPDATE_INTERVAL_OPTIMISE
        )
    )

    async def _async_save_learned_history(_now=None) -> None:
        # Both learners' to_dict() are cheap, synchronous, in-memory dumps -
        # safe to call directly from this scheduled callback.
        await load_store.async_save(coordinator.loads.to_dict())
        await solar_store.async_save(coordinator.solar.to_dict())

    entry.async_on_unload(
        async_track_time_interval(
            hass,
            _async_save_learned_history,
            timedelta(minutes=STORAGE_SAVE_INTERVAL_MINUTES),
        )
    )
    # Also flush on unload/restart, not just on the hourly timer - otherwise
    # up to an hour of learned history is lost on every planned HA restart.
    entry.async_on_unload(lambda: hass.async_create_task(_async_save_learned_history()))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


def _build_pricing_client(hass: HomeAssistant, entry: ConfigEntry):
    """Instantiate the configured pricing provider.

    Deliberately a small factory rather than a plugin registry — with two
    providers, indirection would cost more than it saves.
    """
    provider = entry.data.get(CONF_PROVIDER, PROVIDER_FIXED_TARIFF)

    if provider == PROVIDER_ENTITY_BRIDGE:
        from .pricing.entity_bridge import (
            AMBER_PROFILE,
            EntityBridgeFieldMap,
            EntityBridgeProvider,
        )

        # Options-flow overrides (LibrePowerOptionsFlow.async_step_bridge_fields)
        # for a source integration that isn't Amber-shaped; each falls back to
        # the validated Amber profile's own value when unset, so an
        # Amber-only entry (no overrides ever saved) still gets AMBER_PROFILE
        # verbatim.
        field_map = EntityBridgeFieldMap(
            forecast_attribute=entry.options.get(
                CONF_BRIDGE_FORECAST_ATTRIBUTE, AMBER_PROFILE.forecast_attribute
            ),
            start_time_field=entry.options.get(
                CONF_BRIDGE_START_TIME_FIELD, AMBER_PROFILE.start_time_field
            ),
            price_field=entry.options.get(
                CONF_BRIDGE_PRICE_FIELD, AMBER_PROFILE.price_field
            ),
        )
        return EntityBridgeProvider(
            hass,
            import_entity_id=entry.data[CONF_BRIDGE_IMPORT_ENTITY],
            export_entity_id=entry.data[CONF_BRIDGE_EXPORT_ENTITY],
            field_map=field_map,
        )

    # Fixed tariff is schedule-driven and needs the rates from options.
    from .pricing.fixed_tariff import FixedTariffProvider

    schedule = _schedule_from_options(entry)
    return FixedTariffProvider(schedule, dt_util_timezone(hass))


def _schedule_from_options(entry: ConfigEntry):
    """Rebuild a fixed-tariff ToU schedule from stored options."""
    from datetime import time

    from .pricing.fixed_tariff import TouSchedule, TouWindow

    windows = []
    for raw in entry.options.get("tou_windows", []):
        try:
            start_h, start_m = (int(p) for p in raw["start"].split(":"))
            end_h, end_m = (int(p) for p in raw["end"].split(":"))
        except (KeyError, ValueError):
            _LOGGER.warning("Skipping malformed ToU window: %s", raw)
            continue
        windows.append(
            TouWindow(
                start=time(start_h, start_m),
                end=time(end_h, end_m),
                import_price=float(raw.get("import_price", 0.0)),
                export_price=float(raw.get("export_price", 0.0)),
            )
        )

    return TouSchedule(
        windows=windows,
        default_import_price=float(entry.options.get("default_import_price", 0.0)),
        default_export_price=float(entry.options.get("default_export_price", 0.0)),
    )


def dt_util_timezone(hass: HomeAssistant):
    """The site's local timezone, which ToU windows are expressed in."""
    import homeassistant.util.dt as dt_util

    return dt_util.DEFAULT_TIME_ZONE


async def async_register_battery(
    hass: HomeAssistant, core_entry_id: str, battery: BatteryClient
) -> None:
    """The cross-repo entry point. A battery adapter integration calls this
    once it has connected, to attach itself to a running core instance.

    Typical caller (in a battery adapter's own __init__.py, e.g.
    librepower-powerwall), after its own async_connect() succeeds::

        from custom_components.librepower import async_register_battery
        await async_register_battery(hass, core_entry_id, my_powerwall_client)

    ``core_entry_id`` identifies *which* LibrePower core instance to attach
    to - the adapter's own config flow is responsible for letting the user
    pick one if more than one exists (see that repo's config_flow.py).

    Raises KeyError if core_entry_id doesn't correspond to a loaded LibrePower
    entry - the adapter should treat that as "core isn't set up yet" and
    surface ConfigEntryNotReady, not something core itself can recover from.

    See ``async_unregister_battery`` below for the teardown counterpart -
    call that from the adapter's own ``async_unload_entry`` before closing
    its client, so core doesn't keep a stale reference after the adapter is
    gone.
    """
    coordinator: LibrePowerCoordinator = hass.data[DOMAIN][core_entry_id]
    await coordinator.async_set_battery(battery)


async def async_unregister_battery(
    hass: HomeAssistant, core_entry_id: str, battery: BatteryClient
) -> None:
    """The cross-repo teardown counterpart to ``async_register_battery``.

    A battery adapter integration calls this from its own
    ``async_unload_entry``, *before* closing its client, so core's
    coordinator stops holding a reference to a connection that's about to be
    torn down - without this, the coordinator's next telemetry tick would
    call into an already-closed client and fail repeatedly rather than
    cleanly returning to its pre-registration "waiting for a battery" state.

    Typical caller (a battery adapter's own ``__init__.py``)::

        from custom_components.librepower import async_unregister_battery
        await async_unregister_battery(hass, core_entry_id, my_powerwall_client)
        await my_powerwall_client.async_close()

    Unlike ``async_register_battery``, a missing/unloaded core entry is not
    an error here - it just means there's nothing left to unregister from
    (the common case being both integrations removed together, in either
    unload order), so this is a silent no-op rather than raising KeyError.
    """
    coordinator: LibrePowerCoordinator | None = hass.data.get(DOMAIN, {}).get(
        core_entry_id
    )
    if coordinator is None:
        return
    await coordinator.async_unregister_battery(battery)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Tear down a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: LibrePowerCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_shutdown_client()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change, so new tariffs/limits take effect."""
    await hass.config_entries.async_reload(entry.entry_id)
