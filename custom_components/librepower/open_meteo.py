# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Open-Meteo global horizontal irradiance forecast.

No API key, no account, and a genuinely generous free tier for non-commercial
use (10,000 requests/day) — a home hobbyist integration polling every few
hours doesn't come close. Chosen over Solcast deliberately: Solcast now caps
new accounts at 10 API calls/day (down from 50) and requires an account plus
a hand-tuned polling automation to stay under that limit, which runs directly
against this project's own rule that cloud dependencies should be opt-in and
low-friction, not accounts to manage.

Timestamp semantics — read before changing the resampling logic
------------------------------------------------------------------
Open-Meteo documents ``shortwave_radiation`` as a **preceding-hour mean**,
not an instantaneous reading: the value at timestamp T is the average over
roughly (T-1h, T]. This code treats each value as if it applied at T exactly,
which introduces up to ~30 minutes of timing imprecision relative to the
clear-sky estimate it gets divided by. Accepted deliberately: at the hourly
resolution Open-Meteo provides (versus this project's 30-minute optimisation
slots), sub-hour precision was never available anyway, and the clearness
index this feeds is a coarse weather-adjustment multiplier, not the thing
doing the site's actual power calibration (the history-based learner does
that). Worth revisiting only if slot resolution ever drops below an hour.

This client fetches raw irradiance only — the clearness-index math that turns
it into a power forecast lives in the coordinator, using solar_geometry.py's
clear-sky estimate, not here. This file's only job is "get GHI numbers from
the API."
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiohttp import ClientError, ClientSession

from .const import OPEN_METEO_FORECAST_URL

_LOGGER = logging.getLogger(__name__)


class OpenMeteoError(Exception):
    """Open-Meteo could not supply a forecast."""


class OpenMeteoClient:
    """Fetches forecast global horizontal irradiance (GHI), W/m^2."""

    def __init__(self, session: ClientSession, latitude: float, longitude: float) -> None:
        self._session = session
        self._latitude = latitude
        self._longitude = longitude

    async def async_get_ghi_forecast(
        self, horizon_hours: int
    ) -> list[tuple[datetime, float]]:
        """Hourly (timestamp_utc, ghi_w_m2) pairs covering the horizon.

        Open-Meteo's shortwave_radiation is already GHI at the surface in
        W/m^2 - no conversion needed before it meets the clearness-index math.
        """
        forecast_days = max(1, -(-horizon_hours // 24))  # ceil division
        params = {
            "latitude": self._latitude,
            "longitude": self._longitude,
            "hourly": "shortwave_radiation",
            "forecast_days": min(forecast_days, 16),  # API's own maximum
            "timezone": "UTC",
        }

        try:
            async with self._session.get(
                OPEN_METEO_FORECAST_URL, params=params, timeout=20
            ) as resp:
                if resp.status != 200:
                    raise OpenMeteoError(f"Open-Meteo returned HTTP {resp.status}")
                payload = await resp.json()
        except ClientError as err:
            raise OpenMeteoError(f"Could not reach Open-Meteo: {err}") from err

        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        ghi_values = hourly.get("shortwave_radiation") or []

        if not times or len(times) != len(ghi_values):
            raise OpenMeteoError("Open-Meteo response missing expected fields")

        result: list[tuple[datetime, float]] = []
        for time_str, ghi in zip(times, ghi_values):
            if ghi is None:
                continue
            try:
                moment = datetime.fromisoformat(time_str).replace(tzinfo=timezone.utc)
            except ValueError:
                _LOGGER.debug("Unparseable Open-Meteo timestamp: %s", time_str)
                continue
            result.append((moment, float(ghi)))

        if not result:
            raise OpenMeteoError("Open-Meteo returned no usable GHI values")

        return result
