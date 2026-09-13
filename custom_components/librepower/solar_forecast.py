# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""History-based solar production forecasting.

Same statistical philosophy as load_forecast.py: a transparent rolling
statistic, not a trained model, learned per time-of-day slot from the site's
own observations. What makes this different from load forecasting is that
solar has a strong, precisely-known deterministic component (where the sun
is) mixed with a genuinely unpredictable one (today's clouds) - so naively
averaging "2pm in spring" with "2pm in autumn" would smear the sunrise/sunset
boundary as it shifts through the year.

The fix: divide observed production by ``solar_geometry.clear_sky_shape`` at
the exact observation instant before bucketing. That normalizes out the
elevation-driven seasonal shift, leaving a per-slot "index" that mostly
reflects the site's own panel orientation, tilt, shading, and losses -
which are fixed properties of the installation and don't move with the
calendar. See solar_geometry.py's docstring for why a crude clear-sky model
is fine for this.

This still produces a *climatological* forecast - "what a typical Tuesday in
this season looks like here" - not a weather forecast. It has no way to know
a cold front is coming tomorrow. That's what the optional Open-Meteo
clearness-index multiplier (composed in by the coordinator, not this module)
is for: this class never touches the network and knows nothing about
Open-Meteo. It just answers "given this timestamp, what does history say?"
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from .solar_geometry import clear_sky_shape

_LOGGER = logging.getLogger(__name__)

HISTORY_DAYS = 30
MIN_SAMPLES_PER_SLOT = 3

# Below this shape value, "index = observed / shape" blows up on sensor noise
# near sunrise/sunset (dividing a small real number by an even smaller shape
# amplifies noise into nonsense). Skip ingestion below this threshold rather
# than learn from garbage; the forecast is correctly ~0 at these times anyway
# since it's shape * index and shape itself is tiny.
MIN_SHAPE_FOR_LEARNING = 0.05

# Clamp bounds on the clearness index (Open-Meteo forecast GHI / clear-sky
# GHI estimate). Real days rarely exceed ~1.2 (cloud-edge reflection can
# briefly enhance irradiance above the modelled clear-sky value) and a
# forecast reporting 0 is "totally overcast", never negative.
MIN_CLEARNESS_INDEX = 0.0
MAX_CLEARNESS_INDEX = 1.2


class HistoricalSolarForecaster:
    """Rolling per-slot index of observed solar production vs clear-sky shape."""

    def __init__(self, latitude: float, longitude: float, interval_minutes: int) -> None:
        self._lat = latitude
        self._lon = longitude
        self._interval = interval_minutes
        self._slots_per_day = (24 * 60) // interval_minutes
        # slot index (0..slots_per_day-1) -> list of observed index samples.
        # Deliberately not split weekday/weekend like LoadForecaster - solar
        # doesn't care what day of the week it is, only where the sun is.
        self._samples: dict[int, list[float]] = defaultdict(list)

    # -- ingestion --------------------------------------------------------

    def observe(self, moment: datetime, solar_w: float) -> None:
        """Record one measured solar production reading."""
        if solar_w < 0:
            return  # sensor glitch; production cannot be negative
        shape = clear_sky_shape(moment, self._lat, self._lon)
        if shape < MIN_SHAPE_FOR_LEARNING:
            return  # near/below horizon - skip, see MIN_SHAPE_FOR_LEARNING
        index = solar_w / shape
        slot = self._slot_for(moment)
        bucket = self._samples[slot]
        bucket.append(index)
        if len(bucket) > HISTORY_DAYS:
            del bucket[0]

    def _slot_for(self, moment: datetime) -> int:
        return ((moment.hour * 60 + moment.minute) // self._interval) % self._slots_per_day

    # -- prediction ---------------------------------------------------------

    @property
    def has_data(self) -> bool:
        return any(len(v) >= MIN_SAMPLES_PER_SLOT for v in self._samples.values())

    def forecast(
        self,
        start: datetime,
        count: int,
        clearness_index: list[float] | None = None,
    ) -> list[float]:
        """Return ``count`` solar power predictions in Watts from ``start``.

        ``clearness_index``, if given, is a per-slot weather adjustment
        (typically from Open-Meteo forecast GHI / clear-sky GHI, computed by
        the coordinator - this class has no opinion on where it came from).
        Must be the same length as ``count`` if provided. Defaults to all-1.0
        (pure climatology) when omitted, which is exactly what "no weather
        data available" should produce - the history-only forecast, unchanged.
        """
        if clearness_index is not None and len(clearness_index) != count:
            raise ValueError("clearness_index length must match count")

        step = timedelta(minutes=self._interval)
        result = []
        for i in range(count):
            moment = start + step * i
            shape = clear_sky_shape(moment, self._lat, self._lon)
            if shape <= 0.0:
                result.append(0.0)  # sun is down - no amount of index changes that
                continue
            index = self._predict_index(moment)
            kt = 1.0
            if clearness_index is not None:
                kt = max(MIN_CLEARNESS_INDEX, min(MAX_CLEARNESS_INDEX, clearness_index[i]))
            result.append(shape * index * kt)
        return result

    def _predict_index(self, moment: datetime) -> float:
        slot = self._slot_for(moment)
        samples = self._samples.get(slot, [])
        if len(samples) >= MIN_SAMPLES_PER_SLOT:
            return statistics.median(samples)

        # Not enough history for this exact slot yet - pool across all slots
        # with data rather than guess. A slightly-wrong-shaped forecast from
        # real data beats an invented constant.
        pooled = [v for values in self._samples.values() for v in values]
        if len(pooled) >= MIN_SAMPLES_PER_SLOT:
            return statistics.median(pooled)
        return 0.0  # no history at all yet - stay at the safe default of zero

    # -- persistence ----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "interval_minutes": self._interval,
            "samples": {str(slot): values for slot, values in self._samples.items()},
        }

    @classmethod
    def from_dict(
        cls, data: dict, latitude: float, longitude: float, interval_minutes: int
    ) -> "HistoricalSolarForecaster":
        forecaster = cls(latitude, longitude, interval_minutes)
        if data.get("interval_minutes") != interval_minutes:
            _LOGGER.debug("Solar history discarded: interval changed")
            return forecaster
        for slot_str, values in (data.get("samples") or {}).items():
            try:
                slot = int(slot_str)
            except ValueError:
                continue
            if isinstance(values, list):
                forecaster._samples[slot] = [
                    float(v) for v in values if isinstance(v, (int, float))
                ]
        return forecaster
