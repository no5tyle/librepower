# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Provider-neutral pricing types.

The optimiser should never know which retailer it is talking to. Every provider
client reduces to a ``PriceForecast``, and only that crosses the boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable


class PricingError(Exception):
    """Provider could not supply prices."""


class PricingAuthError(PricingError):
    """Provider rejected our credentials."""


@dataclass(frozen=True, slots=True)
class PriceInterval:
    """One settlement interval. Prices in $/kWh.

    ``export_price`` is positive when you are *paid* to export, negative when
    you are charged to export (negative feed-in).
    """

    start: datetime
    import_price: float
    export_price: float


@dataclass(slots=True)
class PriceForecast:
    """An ordered forward curve from a single provider."""

    provider: str
    intervals: list[PriceInterval]

    def __len__(self) -> int:
        return len(self.intervals)

    @property
    def start(self) -> datetime | None:
        return self.intervals[0].start if self.intervals else None

    def resample(self, minutes: int, count: int) -> tuple[list[float], list[float]]:
        """Project onto a fixed grid of ``count`` slots of ``minutes`` each.

        The optimiser needs two dense, equal-length arrays. Providers give us
        irregular or differently-spaced data (Amber 5 or 30 min, GloBird a ToU
        schedule), so we sample the curve rather than assume alignment.

        Forward-fills the last known price past the end of the curve, which is
        the conservative choice: it never invents a cheaper slot than we've
        actually been quoted.
        """
        if not self.intervals:
            raise PricingError("Cannot resample an empty forecast")

        step = timedelta(minutes=minutes)
        origin = self.intervals[0].start
        imports: list[float] = []
        exports: list[float] = []

        cursor = 0
        for slot in range(count):
            slot_time = origin + step * slot
            # Advance while the *next* interval has already started.
            while (
                cursor + 1 < len(self.intervals)
                and self.intervals[cursor + 1].start <= slot_time
            ):
                cursor += 1
            imports.append(self.intervals[cursor].import_price)
            exports.append(self.intervals[cursor].export_price)

        return imports, exports


@runtime_checkable
class PricingProvider(Protocol):
    """What the coordinator requires of any retailer client."""

    @property
    def provider_name(self) -> str: ...

    async def async_get_forecast(self, horizon_hours: int = 48) -> PriceForecast: ...
