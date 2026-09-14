"""
Runtime monitoring.

Tracks the operational signals called out in the spec: request latency,
failure rate, confidence scores, and escalations (e.g. queries flagged for
human review due to low confidence or a detected prompt-injection attempt).
"""
from __future__ import annotations


class MetricsRegistry:
    def __init__(self):
        self.latencies_ms: list[float] = []
        self.requests = 0
        self.failures = 0
        self.confidences: list[float] = []
        self.escalations = 0
        self.escalation_log: list[dict] = []

    def record_request(self, latency_ms: float, success: bool, confidence: float | None = None) -> None:
        self.requests += 1
        self.latencies_ms.append(latency_ms)
        if not success:
            self.failures += 1
        if confidence is not None:
            self.confidences.append(confidence)

    def record_escalation(self, reason: str = "") -> None:
        self.escalations += 1
        self.escalation_log.append({"reason": reason})

    def summary(self) -> dict:
        n = len(self.latencies_ms)
        sorted_lat = sorted(self.latencies_ms)
        p95 = sorted_lat[max(0, int(n * 0.95) - 1)] if n else 0.0
        return {
            "requests": self.requests,
            "failure_rate": (self.failures / self.requests) if self.requests else 0.0,
            "avg_latency_ms": (sum(self.latencies_ms) / n) if n else 0.0,
            "p95_latency_ms": p95,
            "avg_confidence": (sum(self.confidences) / len(self.confidences)) if self.confidences else None,
            "escalations": self.escalations,
        }
