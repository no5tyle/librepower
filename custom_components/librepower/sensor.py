# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Sensors.

A deliberately short list. Every sensor here answers a question a user actually
asks: how full is the battery, what's it doing, what am I paying, is the plan
saving me anything. Resist adding one per internal variable — that's how you
end up with hundreds of entities nobody reads.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, OPTIMISE_INTERVAL_MINUTES
from .coordinator import LibrePowerCoordinator, LibrePowerData


@dataclass(frozen=True, kw_only=True)
class LibrePowerSensorDescription(SensorEntityDescription):
    """Adds a value extractor to the standard description."""

    value_fn: Callable[[LibrePowerData], Any]
    attrs_fn: Callable[[LibrePowerData], dict[str, Any]] | None = None


def _price_now(data: LibrePowerData, export: bool = False) -> float | None:
    """The price applying to the current interval."""
    if data.prices is None or not data.prices.intervals:
        return None
    now = datetime.now(timezone.utc)
    current = data.prices.intervals[0]
    for interval in data.prices.intervals:
        if interval.start > now:
            break
        current = interval
    return round(current.export_price if export else current.import_price, 4)


def _plan_attrs(data: LibrePowerData) -> dict[str, Any]:
    """Expose the upcoming schedule for dashboard charting."""
    if data.plan is None or not data.plan.success:
        return {"status": data.last_error or "no plan"}

    plan = data.plan
    upcoming = []
    for index in range(min(len(plan.charge_schedule_w), 48)):
        upcoming.append(
            {
                "slot": index,
                "charge_w": round(plan.charge_schedule_w[index]),
                "discharge_w": round(plan.discharge_schedule_w[index]),
                "soc": round(plan.soc_trajectory[index], 3)
                if index < len(plan.soc_trajectory)
                else None,
            }
        )

    return {
        "interval_minutes": OPTIMISE_INTERVAL_MINUTES,
        "solver": plan.solver_name,
        "solve_time_ms": round(plan.solve_time_ms, 1),
        "total_cost": round(plan.total_cost, 2),
        "baseline_cost": round(plan.baseline_cost, 2),
        "plan_created": data.plan_created.isoformat() if data.plan_created else None,
        "schedule": upcoming,
    }


SENSORS: tuple[LibrePowerSensorDescription, ...] = (
    LibrePowerSensorDescription(
        key="battery_soc",
        name="Battery level",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda d: round(d.snapshot.soc_pct, 1) if d.snapshot else None,
    ),
    LibrePowerSensorDescription(
        key="battery_power",
        name="Battery power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda d: round(d.snapshot.battery_w) if d.snapshot else None,
    ),
    LibrePowerSensorDescription(
        key="solar_power",
        name="Solar power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda d: round(d.snapshot.solar_w) if d.snapshot else None,
    ),
    LibrePowerSensorDescription(
        key="grid_power",
        name="Grid power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda d: round(d.snapshot.grid_w) if d.snapshot else None,
    ),
    LibrePowerSensorDescription(
        key="home_load",
        name="Home load",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        value_fn=lambda d: round(d.snapshot.load_w) if d.snapshot else None,
    ),
    LibrePowerSensorDescription(
        key="import_price",
        name="Import price",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD/kWh",
        value_fn=lambda d: _price_now(d, export=False),
    ),
    LibrePowerSensorDescription(
        key="export_price",
        name="Export price",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="AUD/kWh",
        value_fn=lambda d: _price_now(d, export=True),
    ),
    LibrePowerSensorDescription(
        key="control_mode",
        name="Control mode",
        device_class=SensorDeviceClass.ENUM,
        options=["shadow", "active"],
        value_fn=lambda d: d.control_mode,
    ),
    LibrePowerSensorDescription(
        key="planned_action",
        name="Planned action",
        device_class=SensorDeviceClass.ENUM,
        options=["idle", "charge", "discharge", "export", "self_consumption"],
        value_fn=lambda d: d.current_action,
        attrs_fn=_plan_attrs,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: LibrePowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        LibrePowerSensor(coordinator, entry, description) for description in SENSORS
    )


class LibrePowerSensor(CoordinatorEntity[LibrePowerCoordinator], SensorEntity):
    """A single LibrePower sensor."""

    _attr_has_entity_name = True
    entity_description: LibrePowerSensorDescription

    def __init__(
        self,
        coordinator: LibrePowerCoordinator,
        entry: ConfigEntry,
        description: LibrePowerSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="LibrePower",
            manufacturer="LibrePower",
            model="Powerwall (local TEDAPI)",
        )

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.coordinator.data is None or self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data)
