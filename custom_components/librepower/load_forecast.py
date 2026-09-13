# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Home load forecasting.

The optimiser needs a load curve for the horizon. We build one from the site's
own recent history rather than asking the user to describe their habits.

Method: per-slot median over the trailing N days, split weekday/weekend. A
median (not a mean) because household load is spiky — one EV charge or one
pool pump run should not drag the whole forecast up.

This is deliberately a statistical baseline, not a learned model. It is
transparent, needs no training, degrades gracefully with little data, and is
accurate enough that forecast error is dominated by *solar* uncertainty
anyway. Swap in something smarter later behind this same interface.
"""
from __future__ import annotations

import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

_LOGGER = logging.getLogger(__name__)

HISTORY_DAYS = 21
MIN_SAMPLES_PER_SLOT = 3


class LoadForecaster:
    """Rolling per-slot median of observed house load."""

    def __init__(self, interval_minutes: int, fallback_w: float = 500.0) -> None:
        self._interval = interval_minutes
        self._slots_per_day = (24 * 60) // interval_minutes
        self._fallback_w = fallback_w
        # (is_weekend, slot_index) -> list of observed watt values
        self._samples: dict[tuple[bool, int], list[float]] = defaultdict(list)
        self._last_observed: datetime | None = None

    # -- ingestion ------------------------------------------------------------

    def observe(self, moment: datetime, load_w: float) -> None:
        """Record one measured load reading."""
        if load_w < 0:
            return  # sensor glitch; house load cannot be negative
        key = self._key(moment)
        bucket = self._samples[key]
        bucket.append(float(load_w))
        # Bound memory: keep only enough samples to cover HISTORY_DAYS.
        if len(bucket) > HISTORY_DAYS:
            del bucket[0]
        self._last_observed = moment

    def _key(self, moment: datetime) -> tuple[bool, int]:
        slot = (moment.hour * 60 + moment.minute) // self._interval
        return (moment.weekday() >= 5, slot % self._slots_per_day)

    # -- prediction -----------------------------------------------------------

    @property
    def has_data(self) -> bool:
        return any(
            len(v) >= MIN_SAMPLES_PER_SLOT for v in self._samples.values()
        )

    def forecast(self, start: datetime, count: int) -> list[float]:
        """Return ``count`` load predictions in Watts from ``start``."""
        step = timedelta(minutes=self._interval)
        return [
            self._predict(start + step * slot) for slot in range(count)
        ]

    def _predict(self, moment: datetime) -> float:
        samples = self._samples.get(self._key(moment), [])
        if len(samples) >= MIN_SAMPLES_PER_SLOT:
            return statistics.median(samples)

        # Not enough for this slot — try the same slot on the other day type
        # before falling back to a flat guess.
        is_weekend, slot = self._key(moment)
        alt = self._samples.get((not is_weekend, slot), [])
        if len(alt) >= MIN_SAMPLES_PER_SLOT:
            return statistics.median(alt)

        pooled = [v for values in self._samples.values() for v in values]
        if len(pooled) >= MIN_SAMPLES_PER_SLOT:
            return statistics.median(pooled)
        return self._fallback_w

    # -- persistence ----------------------------------------------------------

    def to_dict(self) -> dict:
        """Serialise for HA's Store so history survives restarts."""
        return {
            "interval_minutes": self._interval,
            "samples": {
                f"{int(weekend)}:{slot}": values
                for (weekend, slot), values in self._samples.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict, interval_minutes: int) -> "LoadForecaster":
        forecaster = cls(interval_minutes)
        if data.get("interval_minutes") != interval_minutes:
            # Interval changed; old slot indices are meaningless. Start fresh.
            _LOGGER.debug("Load history discarded: interval changed")
            return forecaster
        for key, values in (data.get("samples") or {}).items():
            try:
                weekend_str, slot_str = key.split(":")
                parsed = (bool(int(weekend_str)), int(slot_str))
            except (ValueError, AttributeError):
                continue
            if isinstance(values, list):
                forecaster._samples[parsed] = [
                    float(v) for v in values if isinstance(v, (int, float))
                ]
        return forecaster
