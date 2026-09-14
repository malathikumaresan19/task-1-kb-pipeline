"""
Update scheduler.

Runs the update job once per day at a configurable time. If the job fails
(exception, or run_fn returns False -- e.g. the document source is down),
it is retried after 15, then 30, then 60 minutes. After the final retry
also fails, the job is marked as permanently failed for that day and the
scheduler waits for the next scheduled run.

Driven by `tick()`, which should be called periodically (e.g. once a
minute in production, or manually after clock.advance(...) in tests/
simulated evaluation) -- this keeps the scheduler fully deterministic and
testable without real sleeping.
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field


@dataclass
class ScheduledAttempt:
    run_at: dt.datetime
    attempt: int = 0  # 0 = first/original attempt, 1..N = retries


class Scheduler:
    def __init__(self, clock, run_fn, daily_time: dt.time, retry_delays_min=(15, 30, 60)):
        """
        clock: Clock or SimulatedClock
        run_fn: callable() -> bool (True = success). May raise; treated as failure.
        daily_time: datetime.time the job should fire once per calendar day
        retry_delays_min: minutes to wait before each successive retry
        """
        self.clock = clock
        self.run_fn = run_fn
        self.daily_time = daily_time
        self.retry_delays_min = list(retry_delays_min)
        self._pending_retry: ScheduledAttempt | None = None
        self._last_run_date = None
        self.history: list[dict] = []

    def _daily_due(self) -> bool:
        now = self.clock.now()
        return self._last_run_date != now.date() and now.time() >= self.daily_time

    def force_run_now(self) -> None:
        """Manually trigger a run outside of the daily schedule (e.g. for testing)."""
        self._execute(ScheduledAttempt(run_at=self.clock.now()))

    def tick(self) -> None:
        now = self.clock.now()

        if self._pending_retry is not None and now >= self._pending_retry.run_at:
            job = self._pending_retry
            self._pending_retry = None
            self._execute(job)
            return

        if self._pending_retry is None and self._daily_due():
            self._last_run_date = now.date()
            self._execute(ScheduledAttempt(run_at=now, attempt=0))

    def _execute(self, job: ScheduledAttempt) -> None:
        now = self.clock.now()
        success = False
        error = None
        try:
            success = bool(self.run_fn())
        except Exception as e:  # simulated service outage / unhandled error
            success = False
            error = str(e)

        self.history.append({
            "time": now, "attempt": job.attempt, "success": success, "error": error,
        })

        if success:
            return

        if job.attempt < len(self.retry_delays_min):
            delay = self.retry_delays_min[job.attempt]
            self._pending_retry = ScheduledAttempt(
                run_at=now + dt.timedelta(minutes=delay),
                attempt=job.attempt + 1,
            )
        else:
            self.history.append({"time": now, "final_failure": True})

    @property
    def has_pending_retry(self) -> bool:
        return self._pending_retry is not None

    @property
    def next_retry_at(self) -> dt.datetime | None:
        return self._pending_retry.run_at if self._pending_retry else None
