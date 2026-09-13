# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""GloBird Energy pricing.

STATUS: schedule-driven, not API-driven. Read this before extending.
---------------------------------------------------------------------
Unlike Amber, GloBird does not publish a documented public price API. Its
plans (e.g. the ZeroHero-style products) are *time-of-use schedules*: fixed
c/kWh rates that apply within named windows, with seasonal and distribution-
zone variation.

That is good news for scope. A ToU plan needs no network calls at all — the
user's rates come from their bill, and we generate the forward curve locally.
This client is therefore deliberately offline.

If you later find a real GloBird API, add it as a *second* provider class
rather than bolting polling onto this one. Do not copy another project's
client: derive it from published docs or your own account's traffic.

Rates must be entered by the user as $/kWh, GST-inclusive, matching their
bill. We do not guess defaults — a wrong tariff produces confidently wrong
battery decisions, which is worse than refusing to run.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone

from .models import PriceForecast, PriceInterval, PricingError


@dataclass(frozen=True, slots=True)
class TouWindow:
    """A recurring daily rate window in the site's local time.

    ``start`` is inclusive, ``end`` exclusive. A window where ``end <= start``
    is treated as wrapping past midnight (e.g. 22:00 -> 07:00).
    """

    start: time
    end: time
    import_price: float
    export_price: float
    # Empty means "every day". 0 = Monday, matching datetime.weekday().
    weekdays: frozenset[int] = field(default_factory=frozenset)

    def covers(self, moment: datetime) -> bool:
        if self.weekdays and moment.weekday() not in self.weekdays:
            return False
        clock = moment.time()
        if self.end <= self.start:  # wraps midnight
            return clock >= self.start or clock < self.end
        return self.start <= clock < self.end


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


class GlobirdClient:
    """Generates a forward price curve from a local ToU schedule."""

    def __init__(self, schedule: TouSchedule, tzinfo) -> None:
        if not schedule.windows and schedule.default_import_price <= 0:
            raise PricingError("A GloBird tariff must be configured first")
        self._schedule = schedule
        self._tz = tzinfo

    @property
    def provider_name(self) -> str:
        return "GloBird Energy"

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
