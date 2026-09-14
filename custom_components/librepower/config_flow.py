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

from datetime import time as dtime
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BACKUP_RESERVE,
    CONF_BRIDGE_EXPORT_ENTITY,
    CONF_BRIDGE_FORECAST_ATTRIBUTE,
    CONF_BRIDGE_IMPORT_ENTITY,
    CONF_BRIDGE_PRICE_FIELD,
    CONF_BRIDGE_START_TIME_FIELD,
    CONF_CONTROL_ENABLED,
    CONF_CYCLE_COST,
    CONF_NO_IMPORT_WINDOWS,
    CONF_PROVIDER,
    CONF_TOU_WINDOWS,
    CONF_WEATHER_AWARE_SOLAR,
    DEFAULT_BACKUP_RESERVE,
    DEFAULT_CONTROL_ENABLED,
    DEFAULT_CYCLE_COST,
    DEFAULT_WEATHER_AWARE_SOLAR,
    DOMAIN,
    PROVIDER_ENTITY_BRIDGE,
    PROVIDER_FIXED_TARIFF,
)
from .time_windows import RecurringWindow

# 0 = Monday, matching RecurringWindow/datetime.weekday()'s own convention.
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_WEEKDAY_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[
            selector.SelectOptionDict(value=str(i), label=name)
            for i, name in enumerate(_WEEKDAY_NAMES)
        ],
        multiple=True,
    )
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
            return await self.async_step_no_import_windows()

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

    async def async_step_bridge_fields(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Screen 3, entity-bridge setups only: field names for a source
        integration that isn't Amber-shaped.

        Only reached when the provider is entity_bridge (async_step_tuning
        branches here instead of finishing) - a fixed-tariff entry has no
        bridge to configure and never sees this screen. Defaults match
        AMBER_PROFILE; see entity_bridge.py's module docstring for the "one
        validated profile + a custom one" design this fills in the second
        half of - before this, there was no UI path to ever set these, only
        dead constants in const.py.
        """
        if user_input is not None:
            self._options.update(user_input)
            return self.async_create_entry(title="", data=self._options)

        from .pricing.entity_bridge import AMBER_PROFILE

        current = self.config_entry.options
        return self.async_show_form(
            step_id="bridge_fields",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_BRIDGE_FORECAST_ATTRIBUTE,
                        default=current.get(
                            CONF_BRIDGE_FORECAST_ATTRIBUTE,
                            AMBER_PROFILE.forecast_attribute,
                        ),
                    ): str,
                    vol.Required(
                        CONF_BRIDGE_START_TIME_FIELD,
                        default=current.get(
                            CONF_BRIDGE_START_TIME_FIELD,
                            AMBER_PROFILE.start_time_field,
                        ),
                    ): str,
                    vol.Required(
                        CONF_BRIDGE_PRICE_FIELD,
                        default=current.get(
                            CONF_BRIDGE_PRICE_FIELD, AMBER_PROFILE.price_field
                        ),
                    ): str,
                }
            ),
        )

    # -- no-import windows: general site policy, shown for every provider -----

    async def async_step_no_import_windows(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Screen 3: recurring windows where grid import is hard-capped near
        zero - e.g. GloBird ZeroHero's evening-peak credit. Shown regardless
        of pricing provider (dynamic or static) - this is a site policy, not
        a fixed-tariff-specific one. See OptimizationConfig.no_import_windows
        in optimiser/engine.py for how it's enforced.

        A menu rather than a single form because the underlying data is a
        list of arbitrary length - HA's config flow has no native "repeat
        this group of fields N times" widget, so this loops (add/remove/done)
        the same way async_step_tou_windows below does for rate windows.
        """
        if not hasattr(self, "_no_import_windows"):
            self._no_import_windows: list[RecurringWindow] = [
                RecurringWindow.from_dict(raw)
                for raw in self.config_entry.options.get(CONF_NO_IMPORT_WINDOWS, [])
            ]

        if user_input is not None:
            action = user_input["action"]
            if action == "add":
                return await self.async_step_add_no_import_window()
            if action == "remove":
                return await self.async_step_remove_no_import_window()
            # done
            self._options[CONF_NO_IMPORT_WINDOWS] = [
                w.to_dict() for w in self._no_import_windows
            ]
            provider = self.config_entry.data.get(CONF_PROVIDER)
            if provider == PROVIDER_ENTITY_BRIDGE:
                return await self.async_step_bridge_fields()
            if provider == PROVIDER_FIXED_TARIFF:
                return await self.async_step_tou_windows()
            return self.async_create_entry(title="", data=self._options)  # pragma: no cover - only 2 providers exist

        actions = {"add": "Add a no-import window", "done": "Done - continue"}
        if self._no_import_windows:
            actions["remove"] = "Remove a no-import window"
        return self.async_show_form(
            step_id="no_import_windows",
            data_schema=vol.Schema(
                {vol.Required("action", default="done"): vol.In(actions)}
            ),
            description_placeholders={
                "windows": _window_list_text(self._no_import_windows)
            },
        )

    async def async_step_add_no_import_window(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            window, error = _parse_window_input(user_input)
            if error:
                return self._add_no_import_window_form(errors={"base": error})
            self._no_import_windows.append(window)
            return await self.async_step_no_import_windows()

        return self._add_no_import_window_form()

    def _add_no_import_window_form(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="add_no_import_window",
            data_schema=vol.Schema(
                {
                    vol.Required("start"): selector.TimeSelector(),
                    vol.Required("end"): selector.TimeSelector(),
                    vol.Optional("weekdays", default=[]): _WEEKDAY_SELECTOR,
                }
            ),
            errors=errors or {},
        )

    async def async_step_remove_no_import_window(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            del self._no_import_windows[int(user_input["window"])]
            return await self.async_step_no_import_windows()

        return self.async_show_form(
            step_id="remove_no_import_window",
            data_schema=vol.Schema(
                {
                    vol.Required("window"): vol.In(
                        {
                            str(i): w.label
                            for i, w in enumerate(self._no_import_windows)
                        }
                    )
                }
            ),
        )

    # -- ToU rate windows: fixed-tariff setups only ----------------------------

    async def async_step_tou_windows(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Final screen, fixed-tariff setups only: peak/off-peak/shoulder
        rate windows on top of the flat rate collected at initial setup
        (which remains the fallback outside every window - see
        pricing/fixed_tariff.py's TouSchedule.rates_at). Only reached when
        the provider is fixed_tariff; entity_bridge has no tariff schedule
        of its own to configure and never sees this screen.

        Same add/remove/done menu loop as async_step_no_import_windows,
        with two extra price fields per window.
        """
        if not hasattr(self, "_tou_windows"):
            from .pricing.fixed_tariff import TouWindow

            self._tou_windows: list[TouWindow] = []
            for raw in self.config_entry.options.get(CONF_TOU_WINDOWS, []):
                try:
                    self._tou_windows.append(
                        TouWindow(
                            window=RecurringWindow.from_dict(raw),
                            import_price=float(raw.get("import_price", 0.0)),
                            export_price=float(raw.get("export_price", 0.0)),
                        )
                    )
                except (KeyError, ValueError):
                    continue  # a malformed stored window - __init__.py logs this case; just skip it here

        if user_input is not None:
            action = user_input["action"]
            if action == "add":
                return await self.async_step_add_tou_window()
            if action == "remove":
                return await self.async_step_remove_tou_window()
            # done
            self._options[CONF_TOU_WINDOWS] = [
                {**w.window.to_dict(), "import_price": w.import_price, "export_price": w.export_price}
                for w in self._tou_windows
            ]
            return self.async_create_entry(title="", data=self._options)

        actions = {"add": "Add a rate window", "done": "Done - save"}
        if self._tou_windows:
            actions["remove"] = "Remove a rate window"
        return self.async_show_form(
            step_id="tou_windows",
            data_schema=vol.Schema(
                {vol.Required("action", default="done"): vol.In(actions)}
            ),
            description_placeholders={
                "windows": _window_list_text(
                    self._tou_windows, with_prices=True
                )
            },
        )

    async def async_step_add_tou_window(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            window, error = _parse_window_input(user_input)
            if error:
                return self._add_tou_window_form(errors={"base": error})
            from .pricing.fixed_tariff import TouWindow

            self._tou_windows.append(
                TouWindow(
                    window=window,
                    import_price=float(user_input["import_price"]),
                    export_price=float(user_input["export_price"]),
                )
            )
            return await self.async_step_tou_windows()

        return self._add_tou_window_form()

    def _add_tou_window_form(
        self, errors: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="add_tou_window",
            data_schema=vol.Schema(
                {
                    vol.Required("start"): selector.TimeSelector(),
                    vol.Required("end"): selector.TimeSelector(),
                    vol.Optional("weekdays", default=[]): _WEEKDAY_SELECTOR,
                    vol.Required("import_price"): vol.Coerce(float),
                    vol.Required("export_price"): vol.Coerce(float),
                }
            ),
            errors=errors or {},
        )

    async def async_step_remove_tou_window(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            del self._tou_windows[int(user_input["window"])]
            return await self.async_step_tou_windows()

        return self.async_show_form(
            step_id="remove_tou_window",
            data_schema=vol.Schema(
                {
                    vol.Required("window"): vol.In(
                        {
                            str(i): f"{w.window.label}: import ${w.import_price:.3f}, export ${w.export_price:.3f}"
                            for i, w in enumerate(self._tou_windows)
                        }
                    )
                }
            ),
        )


def _parse_window_input(
    user_input: dict[str, Any]
) -> tuple[RecurringWindow | None, str | None]:
    """Shared by both add-window steps. Returns (window, None) on success or
    (None, error_key) on failure - HA's TimeSelector already constrains
    input to valid HH:MM:SS strings, so the only real failure mode is a
    window with equal start/end (RecurringWindow would treat that as either
    "never" or "always", depending on the midnight-wrap check - neither is
    ever what the user meant to type)."""
    start = dtime.fromisoformat(user_input["start"])
    end = dtime.fromisoformat(user_input["end"])
    if start == end:
        return None, "window_zero_length"
    weekdays = frozenset(int(d) for d in user_input.get("weekdays", []))
    return RecurringWindow(start=start, end=end, weekdays=weekdays), None


def _window_list_text(windows: list[Any], with_prices: bool = False) -> str:
    if not windows:
        return "(none configured)"
    if with_prices:
        return "\n".join(
            f"- {w.window.label}: import ${w.import_price:.3f}/kWh, export ${w.export_price:.3f}/kWh"
            for w in windows
        )
    return "\n".join(f"- {w.label}" for w in windows)
