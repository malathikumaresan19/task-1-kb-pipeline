"""
Activation control.

Approved candidates are queued and only ever activated while the current
time falls inside the configured maintenance window. Once activated, the
manager polls a health-check function for a configurable duration
(default 5 minutes). If any health check fails during that window, the
version is automatically rolled back via VersionManager.rollback().

Like the scheduler, this is driven by tick() against an injectable clock so
the 5-minute monitoring window can be simulated deterministically.
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass

from .versioning import VersionManager


@dataclass
class MaintenanceWindow:
    start: dt.time
    end: dt.time

    def contains(self, when: dt.datetime) -> bool:
        t = when.time()
        if self.start <= self.end:
            return self.start <= t <= self.end
        return t >= self.start or t <= self.end  # window wraps past midnight


class ActivationManager:
    def __init__(
        self,
        clock,
        version_manager: VersionManager,
        window: MaintenanceWindow,
        health_check_fn,
        health_check_duration_min: int = 5,
        health_check_interval_min: int = 1,
    ):
        self.clock = clock
        self.vm = version_manager
        self.window = window
        self.health_check_fn = health_check_fn  # callable() -> bool (True = healthy)
        self.duration = dt.timedelta(minutes=health_check_duration_min)
        self.interval = dt.timedelta(minutes=health_check_interval_min)

        self._pending_activation: int | None = None
        self._monitor_until: dt.datetime | None = None
        self._last_check_at: dt.datetime | None = None
        self.events: list[dict] = []

    def queue_for_activation(self, vid: int) -> None:
        v = self.vm.versions[vid]
        if v.status != "approved":
            raise ValueError(f"version {vid} must be 'approved' to queue for activation (is '{v.status}')")
        self._pending_activation = vid
        self.events.append({"time": self.clock.now(), "event": "queued", "version": vid})

    @property
    def is_monitoring(self) -> bool:
        return self._monitor_until is not None

    @property
    def has_pending_activation(self) -> bool:
        return self._pending_activation is not None

    def tick(self) -> None:
        now = self.clock.now()

        # Step 1: activate a queued candidate iff we're inside the
        # maintenance window and not already monitoring a prior activation.
        if self._pending_activation is not None and self._monitor_until is None:
            if self.window.contains(now):
                vid = self._pending_activation
                self._pending_activation = None
                self.vm.activate(vid)
                self._monitor_until = now + self.duration
                self._last_check_at = None
                self.events.append({"time": now, "event": "activated", "version": vid})

        # Step 2: while monitoring, poll health checks at the configured
        # interval; roll back immediately on the first failure.
        if self._monitor_until is not None:
            due_for_check = self._last_check_at is None or (now - self._last_check_at) >= self.interval
            if due_for_check:
                self._last_check_at = now
                healthy = bool(self.health_check_fn())
                self.events.append({"time": now, "event": "health_check", "healthy": healthy})
                if not healthy:
                    reverted_to = self.vm.rollback()
                    self.events.append({"time": now, "event": "auto_rollback", "reverted_to": reverted_to})
                    self._monitor_until = None
                    return

            if now >= self._monitor_until:
                self.events.append({
                    "time": now, "event": "monitoring_complete", "version": self.vm.active_version_id,
                })
                self._monitor_until = None
