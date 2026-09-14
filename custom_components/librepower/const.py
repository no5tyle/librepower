# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Constants for LibrePower."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "librepower"

# --- Config entry keys -------------------------------------------------------

# Pricing provider
#
# Two provider types, both generic - neither is tied to any retailer brand:
#   - fixed tariff:  a local time-of-use schedule the user types in from
#                     their bill (formerly "GloBird support"; it was always
#                     generic, just misleadingly named)
#   - entity bridge:  reads price forecast data from an *existing* HA
#                     integration's sensors (works with Amber's official
#                     integration, or any other that publishes a forecast
#                     attribute) rather than this project maintaining its own
#                     retailer-specific API client
CONF_PROVIDER = "provider"
PROVIDER_FIXED_TARIFF = "fixed_tariff"
PROVIDER_ENTITY_BRIDGE = "entity_bridge"
PROVIDERS = [PROVIDER_FIXED_TARIFF, PROVIDER_ENTITY_BRIDGE]

# Entity bridge configuration
CONF_BRIDGE_IMPORT_ENTITY = "bridge_import_entity"
CONF_BRIDGE_EXPORT_ENTITY = "bridge_export_entity"
# Advanced/override fields - default to the validated Amber profile in
# pricing/entity_bridge.py if not overridden.
CONF_BRIDGE_FORECAST_ATTRIBUTE = "bridge_forecast_attribute"
CONF_BRIDGE_START_TIME_FIELD = "bridge_start_time_field"
CONF_BRIDGE_PRICE_FIELD = "bridge_price_field"

# Fixed tariff peak/off-peak/shoulder rate windows - fixed_tariff.py's
# TouSchedule.windows. Stored as a list of dicts: each a
# time_windows.RecurringWindow.to_dict() plus "import_price"/"export_price"
# keys (JSON-safe for config entry options). See config_flow.py's
# async_step_tou_windows for where these are actually set - the flat
# default_import_price/default_export_price collected at initial setup
# (fixed_tariff step) remain the fallback rate outside any window.
CONF_TOU_WINDOWS = "tou_windows"

# Site policy (not hardware - battery physical specs like capacity and max
# charge/discharge power are reported by whichever battery adapter is
# registered, via BatteryClient.async_get_capabilities(). See battery.py.)
CONF_BACKUP_RESERVE = "backup_reserve"

# Recurring "no import" windows - a general site policy independent of
# pricing provider (dynamic or static pricing alike), not e.g. a
# fixed-tariff-only setting. GloBird's ZeroHero evening-peak credit is the
# motivating case (see optimiser/MODIFICATIONS.md and OptimizationConfig's
# own docstring), but this covers any contractual/VPP import restriction the
# same way. Stored as a list of time_windows.RecurringWindow.to_dict() dicts
# (JSON-safe for config entry options) - see config_flow.py's
# async_step_no_import_windows for where these are actually set.
CONF_NO_IMPORT_WINDOWS = "no_import_windows"

# Optimiser tuning
CONF_CYCLE_COST = "cycle_cost"

# Control authority.
#
# OFF by default, and that default is load-bearing. Two integrations writing to
# the same battery will fight: each sees the other's change as an unexpected
# state and corrects it, and the battery oscillates. While PowerSync (or any
# other optimiser) is in charge, LibrePower must observe only.
#
# Shadow mode still computes the full schedule and publishes it to sensors, so
# you can compare LibrePower's decisions against the incumbent's for as long as
# you like before handing over. Turn this on only after the other integration
# is disabled or removed.
CONF_CONTROL_ENABLED = "control_enabled"
DEFAULT_CONTROL_ENABLED = False

# --- Defaults ----------------------------------------------------------------

DEFAULT_BACKUP_RESERVE = 0.20

# $/kWh of battery throughput charged against arbitrage. Keeps the optimiser
# from chasing spreads that cost more in cell wear than they earn.
DEFAULT_CYCLE_COST = 0.02

# --- Polling / optimisation cadence -----------------------------------------

# Powerwall telemetry. Local TEDAPI is cheap but the gateway dislikes hammering.
UPDATE_INTERVAL_TELEMETRY = timedelta(seconds=30)

# Dynamic-price integrations (via the entity bridge) typically publish on a
# 5-30 minute cadence; a fixed tariff schedule needs no polling at all, but
# re-checking on this interval is cheap and keeps both providers on one path.
UPDATE_INTERVAL_PRICES = timedelta(minutes=5)

# Re-solve on a receding horizon. Cheaper than it sounds: ~1s for 48h at 30min.
UPDATE_INTERVAL_OPTIMISE = timedelta(minutes=5)

# --- Optimiser horizon -------------------------------------------------------

OPTIMISE_HORIZON_HOURS = 48
OPTIMISE_INTERVAL_MINUTES = 30

# --- Battery actions ---------------------------------------------------------

ACTION_IDLE = "idle"
ACTION_CHARGE = "charge"
ACTION_DISCHARGE = "discharge"
ACTION_EXPORT = "export"
ACTION_SELF_CONSUMPTION = "self_consumption"

# --- Solar forecasting --------------------------------------------------

# The history-based clear-sky-index model is always on - zero config, zero
# network. Open-Meteo is a genuinely optional weather-aware layer on top:
# default OFF per this project's own rule that cloud dependencies are
# opt-in, never required, even when (as here) the service needs no API key.
CONF_WEATHER_AWARE_SOLAR = "weather_aware_solar"
DEFAULT_WEATHER_AWARE_SOLAR = False

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

SOLAR_FORECAST_SOURCE_CLIMATOLOGY = "climatology"
SOLAR_FORECAST_SOURCE_OPEN_METEO = "open-meteo"

# --- Learned-history persistence ----------------------------------------

STORAGE_VERSION = 1
STORAGE_KEY_LOAD_HISTORY = "load_history"
STORAGE_KEY_SOLAR_HISTORY = "solar_history"
# How often learned history is flushed to disk. Frequent enough that a crash
# doesn't lose much; infrequent enough not to be a write-amplification concern.
STORAGE_SAVE_INTERVAL_MINUTES = 60
