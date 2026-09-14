"""
Injectable clock.

Every time-dependent piece of the pipeline (retry backoff, maintenance
windows, health-check timers) reads time through a Clock instance instead of
calling datetime.now() directly. In production, use Clock(); in tests /
simulated evaluation, use SimulatedClock and call .advance(...) to fast
forward through 15/30/60 minute retry delays or a 5 minute health-check
window without actually sleeping.
"""
from __future__ import annotations
import datetime as dt


class Clock:
    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.timezone.utc)


class SimulatedClock(Clock):
    def __init__(self, start: dt.datetime | None = None):
        self._now = start or dt.datetime.now(dt.timezone.utc)

    def now(self) -> dt.datetime:
        return self._now

    def advance(self, **kwargs) -> dt.datetime:
        """advance(minutes=15) / advance(seconds=30) / advance(days=1) ..."""
        self._now += dt.timedelta(**kwargs)
        return self._now

    def set(self, when: dt.datetime) -> None:
        self._now = when
