# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Read dynamic/wholesale prices from an existing Home Assistant integration.

Why this exists instead of a per-retailer REST client
-------------------------------------------------------
LibrePower used to ship its own Amber Electric API client. That duplicated
work an already-maintained official Home Assistant integration does better -
handles auth, retries, and Amber's own API changes without this project
needing to track any of it. This module reads whatever price sensors are
already on the system instead, which:

  - needs no API key/account handling of its own
  - keeps working if the upstream API changes shape, since that's the
    official integration's problem to fix, not this one's
  - works for any provider with an HA integration publishing forecast data as
    a sensor attribute, not just Amber - Octopus, AEMO spot-price
    integrations, and whatever shows up next, all for free

The honest limit of "generic": field names aren't standardised
-----------------------------------------------------------------
Every integration names its forecast attribute and fields differently. This
module can't be truly zero-configuration for an arbitrary integration it has
never seen - it ships one **validated profile** (Amber's official core HA
integration, confirmed against real community-reported attribute output, not
guessed) and a **custom profile** where the user supplies the field names for
anything else, via the options flow's "Price sensor field names" screen
(config_flow.py's ``async_step_bridge_fields`` - kept out of initial setup so
the common case, Amber, stays a two-field pick there). Building a
fully-automatic schema-sniffing bridge would be solving a much harder problem
than this project needs; a few text fields in options is a fair trade for
supporting integrations we've never
tested against.

Amber profile, confirmed shape (as of the official core integration)
------------------------------------------------------------------------
Forecast sensor's ``forecasts`` attribute is a list of dicts shaped like::

    {
        "duration": 30,
        "start_time": "2026-09-14T02:00:01+00:00",
        "end_time": "2026-09-14T02:30:01+00:00",
        "per_kwh": 0.32,
        ...
    }

Amber publishes import (``general``) and export (``feed_in``) as *separate*
sensors, each with their own ``forecasts`` attribute - this bridge therefore
takes two entity IDs, not one, and merges them by matching start times.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .models import PriceForecast, PriceInterval, PricingError

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EntityBridgeFieldMap:
    """Which attribute holds the forecast list, and what its fields are called.

    Defaults match the Amber profile - see module docstring. Override any
    field for a different integration's shape.
    """

    forecast_attribute: str = "forecasts"
    start_time_field: str = "start_time"
    price_field: str = "per_kwh"


# The one validated profile shipped out of the box. Anything else uses
# EntityBridgeFieldMap's defaults as a starting guess, adjustable in options.
AMBER_PROFILE = EntityBridgeFieldMap()


class EntityBridgeProvider:
    """Builds a PriceForecast from two existing sensor entities' attributes."""

    def __init__(
        self,
        hass: HomeAssistant,
        import_entity_id: str,
        export_entity_id: str,
        field_map: EntityBridgeFieldMap = AMBER_PROFILE,
    ) -> None:
        self._hass = hass
        self._import_entity_id = import_entity_id
        self._export_entity_id = export_entity_id
        self._map = field_map

    @property
    def provider_name(self) -> str:
        return f"Entity bridge ({self._import_entity_id})"

    async def async_get_forecast(self, horizon_hours: int = 48) -> PriceForecast:
        """Read both entities' current attribute state. No network call of our
        own - whatever integration owns these entities has already fetched
        the data; we just read what Home Assistant is currently holding.
        """
        import_by_time = self._read_forecast(self._import_entity_id)
        export_by_time = self._read_forecast(self._export_entity_id)

        if not import_by_time:
            raise PricingError(
                f"No usable forecast data on {self._import_entity_id} - "
                "is the source integration configured and up to date?"
            )

        all_times = sorted(set(import_by_time) | set(export_by_time))
        intervals = [
            PriceInterval(
                start=t,
                import_price=import_by_time.get(t, 0.0),
                export_price=export_by_time.get(t, 0.0),
            )
            for t in all_times
        ]

        return PriceForecast(provider=self.provider_name, intervals=intervals)

    def _read_forecast(self, entity_id: str) -> dict[datetime, float]:
        state = self._hass.states.get(entity_id)
        if state is None:
            raise PricingError(f"Entity {entity_id} does not exist")

        forecast_list = state.attributes.get(self._map.forecast_attribute)
        if not isinstance(forecast_list, list):
            raise PricingError(
                f"{entity_id} has no '{self._map.forecast_attribute}' "
                "attribute - check the field map matches this integration"
            )

        result: dict[datetime, float] = {}
        for entry in forecast_list:
            if not isinstance(entry, dict):
                continue
            raw_time = entry.get(self._map.start_time_field)
            raw_price = entry.get(self._map.price_field)
            if raw_time is None or raw_price is None:
                continue
            moment = self._parse_time(raw_time)
            if moment is None:
                continue
            try:
                result[moment] = float(raw_price)
            except (TypeError, ValueError):
                continue

        return result

    @staticmethod
    def _parse_time(value) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None
        return None
