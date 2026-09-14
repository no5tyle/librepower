# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Generic fixed / time-of-use tariff support.

This started life as a GloBird-specific client. It never actually contained
any GloBird-specific logic — GloBird just doesn't publish a price API, so its
plans are entered as a time-of-use schedule from the user's bill, which is
exactly what most flat-rate and ToU retailers look like (most fixed plans,
many regional/regulated tariffs, and plenty of others besides GloBird). Renamed
and generalised rather than duplicated: this is now core's built-in answer to
"I don't have a dynamic-price integration, I just have a bill with peak/
off-peak rates on it" - no separate provider repo needed for that case, ever.

For dynamic/wholesale pricing (Amber, Octopus, AEMO spot, etc.), see
entity_bridge.py instead - that reads an *existing* Home Assistant
integration's price sensors rather than this module's static schedule.

Rates must be entered by the user as $/kWh, GST-inclusive, matching their
bill. This module does not guess defaults - a wrong tariff produces
confidently wrong battery decisions, which is worse than refusing to run.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..time_windows import RecurringWindow
from .models import PriceForecast, PriceInterval, PricingError


@dataclass(frozen=True, slots=True)
class TouWindow:
    """A recurring daily rate window in the site's local time - a
    ``RecurringWindow`` (the "when") plus the rates that apply inside it
    (the "how much"). See time_windows.py for the recurrence/midnight-wrap
    rules themselves; this just carries prices on top.
    """

    window: RecurringWindow
    import_price: float
    export_price: float

    def covers(self, moment: datetime) -> bool:
        return self.window.covers(moment)


@dataclass(slots=True)
class TouSchedule:
    """An ordered set of windows plus a mandatory fallback rate.

    Windows are evaluated in order and the first match wins, so put specific
    windows (peak) before broad ones (shoulder).
    """

    windows: list[TouWindow]
    default_import_price: float
    default_export_price: float

    def rates_at(self, moment: datetime) -> tuple[float, float]:
        for window in self.windows:
            if window.covers(moment):
                return window.import_price, window.export_price
        return self.default_import_price, self.default_export_price


class FixedTariffProvider:
    """Generates a forward price curve from a local ToU schedule.

    Works for any flat-rate or time-of-use retailer, regardless of brand -
    the schedule is just numbers the user typed in from their bill. Nothing
    here is specific to any one provider.
    """

    def __init__(self, schedule: TouSchedule, tzinfo) -> None:
        if not schedule.windows and schedule.default_import_price <= 0:
            raise PricingError("A fixed tariff must be configured first")
        self._schedule = schedule
        self._tz = tzinfo

    @property
    def provider_name(self) -> str:
        return "Fixed tariff"

    async def async_get_forecast(self, horizon_hours: int = 48) -> PriceForecast:
        """Build the curve locally. Never touches the network.

        Emits 30-minute intervals, which matches NEM settlement and is fine
        for a tariff whose rates only change on window boundaries.
        """
        step = timedelta(minutes=30)
        # Align to the current half-hour so slot boundaries are meaningful.
        now = datetime.now(timezone.utc).astimezone(self._tz)
        anchor = now.replace(minute=0 if now.minute < 30 else 30,
                             second=0, microsecond=0)

        intervals: list[PriceInterval] = []
        for slot in range(int(horizon_hours * 2)):
            local = anchor + step * slot
            import_price, export_price = self._schedule.rates_at(local)
            intervals.append(
                PriceInterval(
                    start=local.astimezone(timezone.utc),
                    import_price=import_price,
                    export_price=export_price,
                )
            )

        return PriceForecast(provider=self.provider_name, intervals=intervals)
