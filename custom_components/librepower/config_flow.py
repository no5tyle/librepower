# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Core config flow: pricing setup only.

Since the repo split, core has no battery-specific step here at all - that
setup (gateway host/password, or whatever a given brand needs) lives entirely
in the relevant battery-adapter integration's own config flow (e.g.
librepower-powerwall). Core's first step is choosing a pricing source; core
doesn't know or care what battery, if any, is attached until one registers.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BACKUP_RESERVE,
    CONF_BRIDGE_EXPORT_ENTITY,
    CONF_BRIDGE_IMPORT_ENTITY,
    CONF_CONTROL_ENABLED,
    CONF_CYCLE_COST,
    CONF_PROVIDER,
    CONF_WEATHER_AWARE_SOLAR,
    DEFAULT_BACKUP_RESERVE,
    DEFAULT_CONTROL_ENABLED,
    DEFAULT_CYCLE_COST,
    DEFAULT_WEATHER_AWARE_SOLAR,
    DOMAIN,
    PROVIDER_ENTITY_BRIDGE,
    PROVIDER_FIXED_TARIFF,
)

STEP_PROVIDER = vol.Schema(
    {
        vol.Required(CONF_PROVIDER, default=PROVIDER_FIXED_TARIFF): vol.In(
            {
                PROVIDER_FIXED_TARIFF: "Fixed or time-of-use tariff (enter rates from your bill)",
                PROVIDER_ENTITY_BRIDGE: "Read prices from an existing integration (e.g. Amber)",
            }
        )
    }
)


class LibrePowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Guided setup: pricing only.

    No unique_id enforced - multiple core instances (e.g. separate sites) are
    allowed; nothing here assumes one LibrePower per Home Assistant install.
    """

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> "LibrePowerOptionsFlow":
        return LibrePowerOptionsFlow()

    # -- step 1: choose pricing source ---------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            if user_input[CONF_PROVIDER] == PROVIDER_ENTITY_BRIDGE:
                return await self.async_step_entity_bridge()
            return await self.async_step_fixed_tariff()

        return self.async_show_form(step_id="user", data_schema=STEP_PROVIDER)

    # -- step 2a: entity bridge ------------------------------------------------

    async def async_step_entity_bridge(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Point at an existing integration's price sensors.

        Validated out of the box against Amber's official core integration's
        confirmed attribute shape (see pricing/entity_bridge.py). A different
        integration's field names, if they differ, can be set afterwards in
        options — kept out of this first screen so setup for the common case
        (Amber) stays a two-field pick, not a schema-mapping exercise.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            # We don't validate connectivity here the way a REST client
            # would — there's no network call to make. The entities either
            # exist and have the right shape when the coordinator first reads
            # them, or they don't; that error surfaces there.
            import_entity = user_input[CONF_BRIDGE_IMPORT_ENTITY]
            export_entity = user_input[CONF_BRIDGE_EXPORT_ENTITY]
            if self.hass.states.get(import_entity) is None:
                errors["base"] = "entity_not_found"
            else:
                self._data[CONF_BRIDGE_IMPORT_ENTITY] = import_entity
                self._data[CONF_BRIDGE_EXPORT_ENTITY] = export_entity
                return self._create()

        return self.async_show_form(
            step_id="entity_bridge",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_BRIDGE_IMPORT_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Required(CONF_BRIDGE_EXPORT_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(domain="sensor")
                    ),
                }
            ),
            errors=errors,
        )

    # -- step 2b: fixed tariff --------------------------------------------------

    async def async_step_fixed_tariff(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """A flat or time-of-use tariff entered by hand, no API involved.

        We only collect a flat rate here to keep setup short; peak/offpeak
        windows are configured afterwards in the options flow, where a repeating
        section is a better fit than a linear config flow.
        """
        if user_input is not None:
            self._data.update(user_input)
            return self._create()

        return self.async_show_form(
            step_id="fixed_tariff",
            data_schema=vol.Schema(
                {
                    vol.Required("default_import_price", default=0.30): vol.Coerce(
                        float
                    ),
                    vol.Required("default_export_price", default=0.05): vol.Coerce(
                        float
                    ),
                }
            ),
        )

    def _create(self) -> ConfigFlowResult:
        return self.async_create_entry(title="LibrePower", data=self._data)


class LibrePowerOptionsFlow(OptionsFlow):
    """Post-setup tuning.

    No battery-hardware fields here anymore (capacity, max charge/discharge) -
    those are reported by whichever battery adapter is registered, via
    ``BatteryClient.async_get_capabilities()``. This screen is only for
    genuinely site-level policy: control handover, backup reserve, wear cost,
    and the weather-aware solar toggle.
    """

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Screen 1: control handover."""
        if user_input is not None:
            self._options.update(user_input)
            if user_input.get(CONF_CONTROL_ENABLED) and not self.config_entry.options.get(
                CONF_CONTROL_ENABLED, DEFAULT_CONTROL_ENABLED
            ):
                # Turning control on for the first time — make it deliberate.
                return await self.async_step_confirm_control()
            return await self.async_step_tuning()

        current = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_CONTROL_ENABLED,
                        default=current.get(
                            CONF_CONTROL_ENABLED, DEFAULT_CONTROL_ENABLED
                        ),
                    ): bool
                }
            ),
        )

    async def async_step_confirm_control(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explicit confirmation before LibrePower starts writing to the battery.

        Two integrations controlling one battery is the single worst failure
        mode available here, so this is a stop sign, not a checkbox. Whether
        a write can actually reach the battery still depends on the battery
        adapter reading this same setting - see its own README.
        """
        if user_input is not None:
            if not user_input.get("understood"):
                # Refused — fall back to shadow mode rather than half-enabling.
                self._options[CONF_CONTROL_ENABLED] = False
            return await self.async_step_tuning()

        return self.async_show_form(
            step_id="confirm_control",
            data_schema=vol.Schema({vol.Required("understood", default=False): bool}),
        )

    async def async_step_tuning(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Screen 2: site-level optimiser tuning. No hardware specs here."""
        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        current = self.config_entry.options
        return self.async_show_form(
            step_id="tuning",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BACKUP_RESERVE,
                        default=current.get(
                            CONF_BACKUP_RESERVE, DEFAULT_BACKUP_RESERVE
                        ),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
                    vol.Required(
                        CONF_CYCLE_COST,
                        default=current.get(CONF_CYCLE_COST, DEFAULT_CYCLE_COST),
                    ): vol.All(vol.Coerce(float), vol.Range(min=0.0, max=1.0)),
                    vol.Required(
                        CONF_WEATHER_AWARE_SOLAR,
                        default=current.get(
                            CONF_WEATHER_AWARE_SOLAR, DEFAULT_WEATHER_AWARE_SOLAR
                        ),
                    ): bool,
                }
            ),
        )
