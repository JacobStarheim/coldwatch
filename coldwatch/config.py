"""Settings, read from environment variables so the same build runs in a demo and for real."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _opt_float(name: str, default: float | None) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return float(raw) if raw.strip() else None


@dataclass
class Settings:
    db_path: str = "coldwatch.db"
    # Shared secret configured on the DT Data Connector. When set, every request must be signed.
    signature_secret: str | None = None

    # Rule thresholds (seconds). Defaults suit a real kitchen; shorten them for a live demo.
    excursion_grace_s: float = 15 * 60  # short spikes from door openings are normal
    door_open_max_s: float = 5 * 60
    escalate_after_s: float = 10 * 60
    offline_min_s: float = 15 * 60  # DT: offline after max(15 min, 1.5 x heartbeat)
    # Events older than this when they arrive are history (backfill, retries): store, don't page anyone.
    historical_s: float = 10 * 60

    # Limits used when a temperature sensor has no max_temp/min_temp labels.
    default_max_temp: float | None = 4.0
    default_min_temp: float | None = None

    # Predictive maintenance: compare how fast the unit cools back down after door openings,
    # recent days vs. the weeks before.
    recovery_band_c: float = 0.5  # "recovered" = back within this of the pre-opening baseline
    min_rise_c: float = 0.5  # ignore openings that barely warmed the unit (too noisy to time)
    recovery_timeout_s: float = 6 * 3600
    maint_recent_days: float = 3
    maint_history_days: float = 30
    maint_min_samples: int = 20
    maint_ratio: float = 1.5
    maint_min_delta: float = 1.0  # min per °C
    maint_clear_ratio: float = 1.25

    # Notifications (all optional; console is always on).
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    webhook_url: str | None = None

    tick_s: float = 15
    timezone: str = "Europe/Oslo"

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        return cls(
            db_path=os.environ.get("COLDWATCH_DB", d.db_path),
            signature_secret=os.environ.get("DT_SIGNATURE_SECRET") or None,
            excursion_grace_s=_float("EXCURSION_GRACE_S", d.excursion_grace_s),
            door_open_max_s=_float("DOOR_OPEN_MAX_S", d.door_open_max_s),
            escalate_after_s=_float("ESCALATE_AFTER_S", d.escalate_after_s),
            offline_min_s=_float("OFFLINE_MIN_S", d.offline_min_s),
            historical_s=_float("HISTORICAL_S", d.historical_s),
            default_max_temp=_opt_float("DEFAULT_MAX_TEMP", d.default_max_temp),
            default_min_temp=_opt_float("DEFAULT_MIN_TEMP", d.default_min_temp),
            ntfy_topic=os.environ.get("NTFY_TOPIC") or None,
            ntfy_server=os.environ.get("NTFY_SERVER", d.ntfy_server),
            webhook_url=os.environ.get("ALERT_WEBHOOK_URL") or None,
            tick_s=_float("TICK_S", d.tick_s),
            timezone=os.environ.get("COLDWATCH_TZ", d.timezone),
        )
