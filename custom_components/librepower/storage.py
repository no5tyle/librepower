# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Persistence for learned history (load and solar forecasters).

Both learners accumulate state across weeks of observation, and losing that
on every restart would mean re-learning from scratch every time Home
Assistant updates. HA's ``Store`` helper already handles the actual file I/O,
debouncing, and atomic writes correctly — this module just gives each
learner a consistent load-once / save-on-a-timer pattern rather than each
reimplementing it slightly differently.

A failure to load or save is never fatal: a learner that can't restore its
history just starts fresh (the same as day one), and a failed save is retried
on the next scheduled flush. Learned history is valuable, not critical.
"""
from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)


class LearningStore:
    """One learner's persisted state, keyed uniquely per config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str, name: str) -> None:
        # entry_id in the key so multiple LibrePower instances (multiple
        # Powerwalls) never collide on the same storage file.
        self._store: Store = Store(
            hass, STORAGE_VERSION, f"librepower_{name}_{entry_id}"
        )

    async def async_load(self) -> dict | None:
        try:
            return await self._store.async_load()
        except Exception as err:  # noqa: BLE001 - a bad file must not block startup
            _LOGGER.warning(
                "Could not load learned history (%s) - starting fresh: %s",
                self._store.key,
                err,
            )
            return None

    async def async_save(self, data: dict) -> None:
        try:
            await self._store.async_save(data)
        except Exception as err:  # noqa: BLE001 - retried on the next scheduled flush
            _LOGGER.warning(
                "Could not save learned history (%s), will retry next flush: %s",
                self._store.key,
                err,
            )
