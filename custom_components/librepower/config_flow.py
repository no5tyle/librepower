# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Config flow: Powerwall first, then retailer.

Order matters. We validate the Powerwall connection before asking for retailer
credentials, because a gateway that can't be reached is a hard blocker and
there's no point collecting an Amber token the user can't use yet.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_AMBER_SITE_ID,
    CONF_AMBER_TOKEN,
    CONF_BACKUP_RESERVE,
    CONF_BATTERY_CAPACITY_WH,
    CONF_CONTROL_ENABLED,
    CONF_CYCLE_COST,
    CONF_GATEWAY_HOST,
    CONF_GATEWAY_PASSWORD,
    CONF_PROVIDER,
    CONF_MAX_CHARGE_W,
    CONF_MAX_DISCHARGE_W,
    CONF_WEATHER_AWARE_SOLAR,
    DEFAULT_BACKUP_RESERVE,
    DEFAULT_BATTERY_CAPACITY_WH,
    DEFAULT_CONTROL_ENABLED,
    DEFAULT_CYCLE_COST,
    DEFAULT_GATEWAY_HOST,
    DEFAULT_MAX_CHARGE_W,
    DEFAULT_MAX_DISCHARGE_W,
    DEFAULT_WEATHER_AWARE_SOLAR,
    DOMAIN,
    PROVIDER_AMBER,
    PROVIDER_GLOBIRD,
)
from .powerwall import (
    PowerwallAuthError,
    PowerwallClient,
    PowerwallError,
    PowerwallUnreachableError,
)
from .pricing.amber import AmberClient
from .pricing.models import PricingAuthError, PricingError

_LOGGER = logging.getLogger(__name__)

STEP_POWERWALL = vol.Schema(
    {
        vol.Required(CONF_GATEWAY_HOST, default=DEFAULT_GATEWAY_HOST): str,
        vol.Required(CONF_GATEWAY_PASSWORD): str,
    }
)

STEP_PROVIDER = vol.Schema(
    {
        vol.Required(CONF_PROVIDER, default=PROVIDER_AMBER): vol.In(
            {
                PROVIDER_AMBER: "Amber Electric (wholesale)",
                PROVIDER_GLOBIRD: "GloBird Energy (time-of-use)",
            }
        )
    }
)

STEP_AMBER = vol.Schema({vol.Required(CONF_AMBER_TOKEN): str})


class LibrePowerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Guided setup."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._amber_sites: list[dict] = []
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> "LibrePowerOptionsFlow":
        return LibrePowerOptionsFlow()

    # -- step 1: Powerwall ----------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            client = PowerwallClient(
                self.hass,
                host=user_input[CONF_GATEWAY_HOST],
                gateway_password=user_input[CONF_GATEWAY_PASSWORD],
            )
            try:
                await client.async_connect()
            except PowerwallAuthError:
                errors["base"] = "invalid_gateway_password"
            except PowerwallUnreachableError:
                errors["base"] = "gateway_unreachable"
            except PowerwallError as err:
                _LOGGER.error("Powerwall setup failed: %s", err)
                errors["base"] = "unknown"
            else:
                await client.async_close()
                await self.async_set_unique_id(
                    f"{DOMAIN}_{user_input[CONF_GATEWAY_HOST]}"
                )
                self._abort_if_unique_id_configured()
                self._data.update(user_input)
                return await self.async_step_provider()

        return self.async_show_form(
            step_id="user", data_schema=STEP_POWERWALL, errors=errors
        )

    # -- reauth: gateway password ---------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Triggered when the Gateway starts rejecting our password."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        assert entry is not None

        if user_input is not None:
            client = PowerwallClient(
                self.hass,
                host=entry.data[CONF_GATEWAY_HOST],
                gateway_password=user_input[CONF_GATEWAY_PASSWORD],
            )
            try:
                await client.async_connect()
            except PowerwallAuthError:
                errors["base"] = "invalid_gateway_password"
            except PowerwallUnreachableError:
                errors["base"] = "gateway_unreachable"
            except PowerwallError:
                errors["base"] = "unknown"
            else:
                await client.async_close()
                # Only the password changes; retailer config is untouched.
                return self.async_update_reload_and_abort(
                    entry,
                    data={**entry.data, **user_input},
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_GATEWAY_PASSWORD): str}),
            errors=errors,
        )

    # -- step 2: choose retailer ---------------------------------------------

    async def async_step_provider(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            if user_input[CONF_PROVIDER] == PROVIDER_AMBER:
                return await self.async_step_amber()
            return await self.async_step_globird()

        return self.async_show_form(step_id="provider", data_schema=STEP_PROVIDER)

    # -- step 3a: Amber -------------------------------------------------------

    async def async_step_amber(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            client = AmberClient(
                async_get_clientsession(self.hass),
                token=user_input[CONF_AMBER_TOKEN],
                site_id="",
            )
            try:
                sites = await client.async_get_sites()
            except PricingAuthError:
                errors["base"] = "invalid_amber_token"
            except PricingError as err:
                _LOGGER.error("Amber lookup failed: %s", err)
                errors["base"] = "amber_unavailable"
            else:
                if not sites:
                    errors["base"] = "no_amber_sites"
                else:
                    self._data[CONF_AMBER_TOKEN] = user_input[CONF_AMBER_TOKEN]
                    self._amber_sites = sites
                    if len(sites) == 1:
                        # Don't make the user pick from a list of one.
                        self._data[CONF_AMBER_SITE_ID] = sites[0]["id"]
                        return self._create()
                    return await self.async_step_amber_site()

        return self.async_show_form(
            step_id="amber", data_schema=STEP_AMBER, errors=errors
        )

    async def async_step_amber_site(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_AMBER_SITE_ID] = user_input[CONF_AMBER_SITE_ID]
            return self._create()

        choices = {
            site["id"]: site.get("nmi") or site["id"] for site in self._amber_sites
        }
        return self.async_show_form(
            step_id="amber_site",
            data_schema=vol.Schema({vol.Required(CONF_AMBER_SITE_ID): vol.In(choices)}),
        )

    # -- step 3b: GloBird -----------------------------------------------------

    async def async_step_globird(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """GloBird has no price API, so the tariff is entered by hand.

        We only collect a flat rate here to keep setup short; peak/offpeak
        windows are configured afterwards in the options flow, where a repeating
        section is a better fit than a linear config flow.
        """
        if user_input is not None:
            self._data.update(user_input)
            return self._create()

        return self.async_show_form(
            step_id="globird",
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

    Two screens rather than one long form: the handover decision is a different
    kind of choice from battery limits, and burying "take control of my battery"
    among capacity fields is how people flip it by accident.
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
            return await self.async_step_battery()

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
        mode available here, so this is a stop sign, not a checkbox.
        """
        if user_input is not None:
            if not user_input.get("understood"):
                # Refused — fall back to shadow mode rather than half-enabling.
                self._options[CONF_CONTROL_ENABLED] = False
            return await self.async_step_battery()

        return self.async_show_form(
            step_id="confirm_control",
            data_schema=vol.Schema({vol.Required("understood", default=False): bool}),
        )

    async def async_step_battery(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Screen 2: battery limits and optimiser tuning."""
        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        current = self.config_entry.options
        return self.async_show_form(
            step_id="battery",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BATTERY_CAPACITY_WH,
                        default=current.get(
                            CONF_BATTERY_CAPACITY_WH, DEFAULT_BATTERY_CAPACITY_WH
                        ),
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_MAX_CHARGE_W,
                        default=current.get(CONF_MAX_CHARGE_W, DEFAULT_MAX_CHARGE_W),
                    ): vol.Coerce(float),
                    vol.Required(
                        CONF_MAX_DISCHARGE_W,
                        default=current.get(
                            CONF_MAX_DISCHARGE_W, DEFAULT_MAX_DISCHARGE_W
                        ),
                    ): vol.Coerce(float),
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
