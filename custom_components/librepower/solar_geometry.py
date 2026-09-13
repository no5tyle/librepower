# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Solar position and a deliberately crude clear-sky irradiance estimate.

The elevation calculation is the standard simplified NOAA solar position
algorithm (public astronomical formulas — declination, equation of time, hour
angle — the same equations appear in any solar-engineering textbook and in
countless independent implementations; nothing here is derived from anyone's
code). Good to within a fraction of a degree, which is far more precision
than a *ratio*-based forecast actually needs.

Why the clear-sky GHI model is intentionally crude
---------------------------------------------------
``clear_sky_ghi_estimate`` is just ``1000 * max(0, sin(elevation))`` — no
atmospheric attenuation, no air-mass correction, no turbidity. That is a
correctness choice, not laziness: this value is used on *both* sides of two
separate ratios elsewhere in the solar-forecasting code —

  1. ``HistoricalSolarForecaster`` divides observed production by this shape
     to learn a per-slot index (which absorbs the site's real panel
     orientation, tilt, shading and losses).
  2. The weather layer divides Open-Meteo's forecast GHI by this same shape
     to get a clearness index (how cloudy vs a clear day).

Any systematic bias in the crude model cancels between the two ratios as
long as the *same* function is used consistently on both sides — which is
exactly what this module being the single source of that estimate guarantees.
A more accurate clear-sky model would change both ratios' absolute scale
together and leave every downstream forecast unchanged.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


def solar_elevation_degrees(
    moment: datetime, latitude: float, longitude: float
) -> float:
    """Solar elevation angle in degrees. Negative when the sun is below the horizon.

    ``moment`` must be timezone-aware; converted to UTC internally so the
    result doesn't depend on what zone it happened to arrive in.
    """
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone-aware")
    moment = moment.astimezone(timezone.utc)

    day_of_year = moment.timetuple().tm_yday
    hour_utc = moment.hour + moment.minute / 60.0 + moment.second / 3600.0

    gamma = 2.0 * math.pi / 365.0 * (day_of_year - 1 + (hour_utc - 12.0) / 24.0)

    # Equation of time, in minutes - the gap between clock time and true
    # solar time caused by Earth's elliptical orbit and axial tilt.
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )

    # Solar declination, in radians.
    decl = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    # Working directly in UTC makes the timezone-offset term drop out of the
    # standard NOAA formula (offset would otherwise be -60*tz_hours).
    time_offset = eqtime + 4.0 * longitude
    true_solar_time = hour_utc * 60.0 + time_offset

    hour_angle_deg = true_solar_time / 4.0 - 180.0
    hour_angle = math.radians(hour_angle_deg)

    lat_rad = math.radians(latitude)

    cos_zenith = math.sin(lat_rad) * math.sin(decl) + math.cos(lat_rad) * math.cos(
        decl
    ) * math.cos(hour_angle)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))  # guard float drift at the poles of the range

    zenith_deg = math.degrees(math.acos(cos_zenith))
    return 90.0 - zenith_deg


def clear_sky_shape(moment: datetime, latitude: float, longitude: float) -> float:
    """Relative clear-sky output shape, 0-1. Zero when the sun is below the horizon.

    This is what ``HistoricalSolarForecaster`` divides observed production by
    to learn a per-slot index — see module docstring for why a crude model is
    fine here.
    """
    elevation = solar_elevation_degrees(moment, latitude, longitude)
    if elevation <= 0:
        return 0.0
    return math.sin(math.radians(elevation))


# Nominal clear-sky peak irradiance at solar noon, W/m^2 - the standard
# "1000 W/m^2" reference used throughout solar engineering (roughly what a
# clear temperate-latitude midday delivers at the surface). This is only
# used to express clear_sky_shape in physical units for the Open-Meteo
# clearness-index calculation; per the module docstring, the exact constant
# does not matter for the site-calibration ratio, only for the weather one,
# and Open-Meteo's own GHI is reported in the same units so this must match
# that convention (W/m^2) to produce a meaningful ratio.
CLEAR_SKY_PEAK_GHI_W_M2 = 1000.0


def clear_sky_ghi_estimate(moment: datetime, latitude: float, longitude: float) -> float:
    """Approximate clear-sky global horizontal irradiance, W/m^2."""
    return CLEAR_SKY_PEAK_GHI_W_M2 * clear_sky_shape(moment, latitude, longitude)
