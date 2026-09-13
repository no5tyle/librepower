# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Amber Electric pricing client.

Amber exposes a documented REST API, so this is a plain aiohttp client — no
reverse engineering, no vendored SDK. We fetch the forward price forecast and
reduce it to the two flat arrays the optimiser wants.

Amber quotes prices in c/kWh; the optimiser works in $/kWh. Conversion happens
here, once, at the boundary.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from aiohttp import ClientError, ClientResponseError, ClientSession

from ..const import AMBER_API_BASE
from .models import PriceForecast, PriceInterval, PricingError, PricingAuthError

_LOGGER = logging.getLogger(__name__)

# Amber channel identifiers.
_CHANNEL_GENERAL = "general"      # import
_CHANNEL_FEED_IN = "feedIn"       # export


class AmberClient:
    """Fetches forward prices from Amber Electric."""

    def __init__(self, session: ClientSession, token: str, site_id: str) -> None:
        self._session = session
        self._token = token
        self._site_id = site_id

    @property
    def provider_name(self) -> str:
        return "Amber Electric"

    async def async_get_sites(self) -> list[dict]:
        """List sites on the account. Used by the config flow."""
        return await self._get("/sites")

    async def async_get_forecast(self, horizon_hours: int = 48) -> PriceForecast:
        """Fetch current + forecast prices for the horizon."""
        # Amber returns 5-minute or 30-minute resolution depending on plan;
        # ask for enough days to cover the horizon and trim afterwards.
        days = max(1, (horizon_hours + 23) // 24)
        payload = await self._get(
            f"/sites/{self._site_id}/prices/current",
            params={"next": str(days * 48), "previous": "0"},
        )

        intervals: dict[datetime, dict[str, float]] = {}
        for row in payload:
            start = self._parse_time(row.get("startTime"))
            if start is None:
                continue
            channel = row.get("channelType")
            price = row.get("perKwh")
            if channel is None or price is None:
                continue
            slot = intervals.setdefault(start, {})
            # Amber gives c/kWh. Feed-in is quoted as a credit; a positive
            # perKwh on feedIn means you are paid, which matches the
            # optimiser's convention, so no sign flip here.
            slot[channel] = float(price) / 100.0

        cutoff = datetime.now(timezone.utc) + timedelta(hours=horizon_hours)
        ordered = [
            PriceInterval(
                start=start,
                import_price=values.get(_CHANNEL_GENERAL, 0.0),
                export_price=values.get(_CHANNEL_FEED_IN, 0.0),
            )
            for start, values in sorted(intervals.items())
            if start <= cutoff
        ]

        if not ordered:
            raise PricingError("Amber returned no usable price intervals")

        return PriceForecast(provider=self.provider_name, intervals=ordered)

    # -- transport ------------------------------------------------------------

    async def _get(self, path: str, params: dict | None = None):
        url = f"{AMBER_API_BASE}{path}"
        try:
            async with self._session.get(
                url,
                headers={"Authorization": f"Bearer {self._token}"},
                params=params,
                timeout=30,
            ) as resp:
                if resp.status in (401, 403):
                    raise PricingAuthError("Amber rejected the API token")
                resp.raise_for_status()
                return await resp.json()
        except ClientResponseError as err:
            raise PricingError(f"Amber API error {err.status}") from err
        except ClientError as err:
            raise PricingError(f"Could not reach Amber: {err}") from err

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            _LOGGER.debug("Unparseable Amber timestamp: %s", value)
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
