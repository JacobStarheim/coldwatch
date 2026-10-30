"""Predictive maintenance from door-opening recoveries.

Every door opening is a small, free stress test: warm air gets in and the compressor has to
pull the temperature back down. We time each recovery and divide by how much the unit warmed
up, giving minutes per °C: the unit's pull-down speed, independent of how long the door was
open or where the thermostat was in its cycle. A healthy unit pulls down at roughly the same
speed every day. When the compressor, condenser fan or door seal starts to fail, pull-down gets
slower long before the unit fails outright. We compare the median of the last few days with the
median of the weeks before, and warn when it is clearly worse.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass

from .config import Settings


@dataclass
class Assessment:
    baseline: float  # median minutes per °C before the recent window
    recent: float  # median minutes per °C in the recent window
    n_baseline: int
    n_recent: int
    degraded: bool
    cleared: bool

    @property
    def ratio(self) -> float:
        return self.recent / self.baseline if self.baseline > 0 else float("inf")

    @property
    def message(self) -> str:
        pct = (self.ratio - 1) * 100
        return (
            f"Slower cool-down after door openings: {self.recent:.1f} min per °C recently vs "
            f"{self.baseline:.1f} before (+{pct:.0f}%). The compressor, condenser or door seal "
            "may need service"
        )

    def as_details(self) -> dict:
        return {
            "baseline_min_per_c": round(self.baseline, 2),
            "recent_min_per_c": round(self.recent, 2),
            "ratio": round(self.ratio, 2),
            "n_baseline": self.n_baseline,
            "n_recent": self.n_recent,
        }


def assess(baseline: list[float], recent: list[float], settings: Settings) -> Assessment | None:
    """None when there is not enough data to say anything."""
    if len(baseline) < settings.maint_min_samples or len(recent) < settings.maint_min_samples:
        return None
    # Medians, not means: one opening during a delivery (door wedged open, then a long
    # recovery) should not trigger a service call.
    base = statistics.median(baseline)
    now = statistics.median(recent)
    ratio = now / base if base > 0 else float("inf")
    degraded = ratio >= settings.maint_ratio and now - base >= settings.maint_min_delta
    cleared = ratio < settings.maint_clear_ratio  # hysteresis so the warning doesn't flap
    return Assessment(base, now, len(baseline), len(recent), degraded, cleared)
