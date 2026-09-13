# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""LibrePower — local-first battery optimisation for Home Assistant."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_AMBER_SITE_ID,
    CONF_CONTROL_ENABLED,
    CONF_AMBER_TOKEN,
    CONF_BACKUP_RESERVE,
    CONF_BATTERY_CAPACITY_WH,
    CONF_CYCLE_COST,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PASSWORD,
    CONF_MAX_CHARGE_W,
    CONF_MAX_DISCHARGE_W,
    CONF_PROVIDER,
    DEFAULT_BACKUP_RESERVE,
    DEFAULT_BATTERY_CAPACITY_WH,
    DEFAULT_CONTROL_ENABLED,
    DEFAULT_CYCLE_COST,
    DEFAULT_MAX_CHARGE_W,
    DEFAULT_MAX_DISCHARGE_W,
    DOMAIN,
    OPTIMISE_HORIZON_HOURS,
    OPTIMISE_INTERVAL_MINUTES,
    PROVIDER_AMBER,
    UPDATE_INTERVAL_OPTIMISE,
)
from .coordinator import LibrePowerCoordinator
from .optimiser import OptimizationConfig
from .powerwall import PowerwallAuthError, PowerwallClient, PowerwallError
from .pricing.amber import AmberClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a LibrePower config entry."""
    # Shadow mode unless explicitly turned on. Safe to run beside another
    # optimiser: LibrePower reads and plans, but never writes.
    control_enabled = entry.options.get(
        CONF_CONTROL_ENABLED, DEFAULT_CONTROL_ENABLED
    )
    if not control_enabled:
        _LOGGER.info(
            "LibrePower starting in shadow mode - planning only, no battery "
            "writes. Enable control in options when ready to take over."
        )

    powerwall = PowerwallClient(
        hass,
        host=entry.data[CONF_GATEWAY_HOST],
        gateway_password=entry.data[CONF_GATEWAY_PASSWORD],
        read_only=not control_enabled,
    )

    try:
        await powerwall.async_connect()
    except PowerwallAuthError as err:
        # Gateway password is wrong — prompt reauth rather than retrying forever.
        raise ConfigEntryAuthFailed(str(err)) from err
    except PowerwallError as err:
        raise ConfigEntryNotReady(f"Cannot reach Powerwall: {err}") from err

    pricing = _build_pricing_client(hass, entry)

    optimiser_config = OptimizationConfig(
        battery_capacity_wh=entry.options.get(
            CONF_BATTERY_CAPACITY_WH,
            entry.data.get(CONF_BATTERY_CAPACITY_WH, DEFAULT_BATTERY_CAPACITY_WH),
        ),
        max_charge_w=entry.options.get(CONF_MAX_CHARGE_W, DEFAULT_MAX_CHARGE_W),
        max_discharge_w=entry.options.get(
            CONF_MAX_DISCHARGE_W, DEFAULT_MAX_DISCHARGE_W
        ),
        backup_reserve=entry.options.get(
            CONF_BACKUP_RESERVE, DEFAULT_BACKUP_RESERVE
        ),
        cycle_cost=entry.options.get(CONF_CYCLE_COST, DEFAULT_CYCLE_COST),
        interval_minutes=OPTIMISE_INTERVAL_MINUTES,
        horizon_hours=OPTIMISE_HORIZON_HOURS,
    )

    coordinator = LibrePowerCoordinator(
        hass, powerwall=powerwall, pricing=pricing, optimiser_config=optimiser_config
    )
    await coordinator.async_config_entry_first_refresh()

    # Solve once immediately so entities are populated before the first tick.
    await coordinator.async_refresh_plan()

    entry.async_on_unload(
        async_track_time_interval(
            hass, coordinator.async_refresh_plan, UPDATE_INTERVAL_OPTIMISE
        )
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


def _build_pricing_client(hass: HomeAssistant, entry: ConfigEntry):
    """Instantiate the configured retailer client.

    Deliberately a small factory rather than a plugin registry — with two
    providers, indirection would cost more than it saves.
    """
    provider = entry.data.get(CONF_PROVIDER, PROVIDER_AMBER)

    if provider == PROVIDER_AMBER:
        return AmberClient(
            async_get_clientsession(hass),
            token=entry.data[CONF_AMBER_TOKEN],
            site_id=entry.data[CONF_AMBER_SITE_ID],
        )

    # GloBird is schedule-driven and needs the tariff from options.
    from .pricing.globird import GlobirdClient, TouSchedule

    schedule = _schedule_from_options(entry)
    return GlobirdClient(schedule, dt_util_timezone(hass))


def _schedule_from_options(entry: ConfigEntry):
    """Rebuild a GloBird ToU schedule from stored options."""
    from datetime import time

    from .pricing.globird import TouSchedule, TouWindow

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
