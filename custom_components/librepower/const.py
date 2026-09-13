# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Constants for LibrePower."""
from __future__ import annotations

from datetime import timedelta

DOMAIN = "librepower"

# --- Config entry keys -------------------------------------------------------

# Powerwall (local TEDAPI)
CONF_GATEWAY_HOST = "gateway_host"
CONF_GATEWAY_PASSWORD = "gateway_password"

# Pricing provider
CONF_PROVIDER = "provider"
PROVIDER_AMBER = "amber"
PROVIDER_GLOBIRD = "globird"
PROVIDERS = [PROVIDER_AMBER, PROVIDER_GLOBIRD]

CONF_AMBER_TOKEN = "amber_token"
CONF_AMBER_SITE_ID = "amber_site_id"

# Battery physical parameters
CONF_BATTERY_CAPACITY_WH = "battery_capacity_wh"
CONF_MAX_CHARGE_W = "max_charge_w"
CONF_MAX_DISCHARGE_W = "max_discharge_w"
CONF_BACKUP_RESERVE = "backup_reserve"

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

# Gateway's own WiFi AP address. Reachable when HA host has a route to it.
DEFAULT_GATEWAY_HOST = "192.168.91.1"

DEFAULT_BATTERY_CAPACITY_WH = 13500.0
DEFAULT_MAX_CHARGE_W = 5000.0
DEFAULT_MAX_DISCHARGE_W = 5000.0
DEFAULT_BACKUP_RESERVE = 0.20

# $/kWh of battery throughput charged against arbitrage. Keeps the optimiser
# from chasing spreads that cost more in cell wear than they earn.
DEFAULT_CYCLE_COST = 0.02

# --- Polling / optimisation cadence -----------------------------------------

# Powerwall telemetry. Local TEDAPI is cheap but the gateway dislikes hammering.
UPDATE_INTERVAL_TELEMETRY = timedelta(seconds=30)

# Amber publishes 5-minute intervals; GloBird is a static ToU schedule.
UPDATE_INTERVAL_PRICES = timedelta(minutes=5)

# Re-solve on a receding horizon. Cheaper than it sounds: ~1s for 48h at 30min.
UPDATE_INTERVAL_OPTIMISE = timedelta(minutes=5)

# --- Optimiser horizon -------------------------------------------------------

OPTIMISE_HORIZON_HOURS = 48
OPTIMISE_INTERVAL_MINUTES = 30

# --- Amber API ---------------------------------------------------------------

AMBER_API_BASE = "https://api.amber.com.au/v1"

# --- Battery actions ---------------------------------------------------------

ACTION_IDLE = "idle"
ACTION_CHARGE = "charge"
ACTION_DISCHARGE = "discharge"
ACTION_EXPORT = "export"
ACTION_SELF_CONSUMPTION = "self_consumption"
