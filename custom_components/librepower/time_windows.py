# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 LibrePower contributors
# Full license: /LICENSE. Third-party exception (this file is NOT it): /NOTICE.

"""Recurring daily time windows, in site-local time.

One shared primitive for two different uses that both boil down to "does
this moment fall inside a recurring daily window":

  - pricing/fixed_tariff.py's ``TouSchedule``: windows carry rates (peak/
    off-peak/shoulder), evaluated once per forecast slot when building the
    price curve.
  - the optimiser's ``no_import_windows`` (``OptimizationConfig``): windows
    carry no data of their own - grid import is hard-capped near zero for
    intervals inside one, evaluated per LP interval during solve. This is a
    general site-policy concept, independent of which pricing provider is
    active - GloBird's ZeroHero evening-peak credit is the motivating case
    (see optimiser/MODIFICATIONS.md), but it covers any contractual/VPP
    import restriction the same way, dynamic or static pricing alike.

Both need the exact same "start/end/which weekdays" recurrence and the same
midnight-wrap handling, so it lives here once rather than twice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Any


@dataclass(frozen=True, slots=True)
class RecurringWindow:
    """A recurring daily window, evaluated in whatever timezone the
    ``moment`` passed to ``covers`` is already in - this class has no
    timezone concept of its own, it only compares clock time and weekday.

    ``start`` is inclusive, ``end`` exclusive. A window where ``end <= start``
    is treated as wrapping past midnight (e.g. 22:00 -> 07:00).
    """

    start: time
    end: time
    # Empty means "every day". 0 = Monday, matching datetime.weekday().
    weekdays: frozenset[int] = field(default_factory=frozenset)

    def covers(self, moment: datetime) -> bool:
        if self.weekdays and moment.weekday() not in self.weekdays:
            return False
        clock = moment.time()
        if self.end <= self.start:  # wraps midnight
            return clock >= self.start or clock < self.end
        return self.start <= clock < self.end

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation for storing in a config entry's options
        (HA options must round-trip through JSON - ``time``/``frozenset``
        don't on their own)."""
        return {
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M"),
            "weekdays": sorted(self.weekdays),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RecurringWindow":
        return cls(
            start=time.fromisoformat(data["start"]),
            end=time.fromisoformat(data["end"]),
            weekdays=frozenset(data.get("weekdays") or ()),
        )

    @property
    def label(self) -> str:
        """Human-readable summary for options-flow list screens, e.g.
        '18:00-21:00 (Mon, Tue, Wed, Thu, Fri)' or '11:00-14:00 (every day)'.
        """
        day_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
        days = (
            ", ".join(day_names[d] for d in sorted(self.weekdays))
            if self.weekdays
            else "every day"
        )
        return f"{self.start.strftime('%H:%M')}-{self.end.strftime('%H:%M')} ({days})"
