"""
Pre-activation quality gate.

Runs an evaluation suite against a candidate KB snapshot and compares it to
the metrics recorded for the currently-active (baseline) version. If
accuracy or grounding would regress beyond the configured tolerance -- or
the error rate is too high -- the candidate is rejected and never queued
for activation.

`eval_fn` is pluggable: pass in your real retrieval/answer-quality eval
harness. The bundled default is a cheap heuristic so the pipeline runs
standalone / in tests without a real model in the loop.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class QualityMetrics:
    accuracy: float
    grounding: float
    avg_latency_ms: float
    error_rate: float


def default_eval_fn(docs_snapshot: dict) -> QualityMetrics:
    """
    Heuristic placeholder eval: rewards non-trivial, non-empty documents.
    Replace with a real eval harness (e.g. run a fixed QA test set against
    a retrieval index built from docs_snapshot and score groundedness).
    """
    if not docs_snapshot:
        return QualityMetrics(accuracy=0.0, grounding=0.0, avg_latency_ms=0.0, error_rate=1.0)
    lengths = [len(t) for t in docs_snapshot.values()]
    avg_len = sum(lengths) / len(lengths)
    accuracy = min(1.0, 0.5 + avg_len / 2000)
    grounding = min(1.0, 0.6 + avg_len / 3000)
    return QualityMetrics(accuracy=accuracy, grounding=grounding, avg_latency_ms=120.0, error_rate=0.0)


class QualityGate:
    def __init__(
        self,
        eval_fn=None,
        accuracy_tolerance: float = 0.0,
        grounding_tolerance: float = 0.0,
        max_error_rate: float = 0.05,
    ):
        self.eval_fn = eval_fn or default_eval_fn
        self.accuracy_tolerance = accuracy_tolerance
        self.grounding_tolerance = grounding_tolerance
        self.max_error_rate = max_error_rate

    def evaluate(self, candidate_docs: dict, baseline_metrics: QualityMetrics | None):
        metrics = self.eval_fn(candidate_docs)
        reasons: list[str] = []

        if baseline_metrics is not None:
            if metrics.accuracy < baseline_metrics.accuracy - self.accuracy_tolerance:
                reasons.append(
                    f"accuracy regressed: {metrics.accuracy:.3f} < baseline {baseline_metrics.accuracy:.3f}"
                )
            if metrics.grounding < baseline_metrics.grounding - self.grounding_tolerance:
                reasons.append(
                    f"grounding regressed: {metrics.grounding:.3f} < baseline {baseline_metrics.grounding:.3f}"
                )
        if metrics.error_rate > self.max_error_rate:
            reasons.append(f"error_rate too high: {metrics.error_rate:.3f} > {self.max_error_rate:.3f}")

        passed = len(reasons) == 0
        return passed, metrics, reasons
