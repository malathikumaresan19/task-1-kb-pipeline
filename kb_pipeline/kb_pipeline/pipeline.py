"""
KnowledgeBasePipeline: the orchestrator that wires together ingestion,
versioning, quality gating, scheduling, activation, security, and
monitoring into a single production pipeline.

Typical wiring:

    pipeline = KnowledgeBasePipeline(
        clock=clock,
        fetch_documents=my_source.fetch,       # -> list[Document]; raises on outage
        eval_fn=my_eval_harness.run,            # -> QualityMetrics
        maintenance_window=MaintenanceWindow(start=time(2, 0), end=time(4, 0)),
        health_check_fn=my_health_checker.check, # -> bool
        daily_update_time=time(1, 0),
    )
    pipeline.access.register_user("alice", Role.ADMIN)
    pipeline.access.register_user("etl-bot", Role.EDITOR)

    # in production: call pipeline.tick() once a minute (e.g. from a cron
    # job / background thread). In tests: call clock.advance(...) then
    # pipeline.tick() to deterministically simulate time passing.
"""
from __future__ import annotations
from enum import Enum

from .documents import DocumentProcessor
from .versioning import VersionManager
from .quality import QualityGate
from .scheduler import Scheduler
from .activation import ActivationManager, MaintenanceWindow
from .security import AccessControl, PromptInjectionGuard, DataMasker
from .monitoring import MetricsRegistry


class UpdateOutcome(str, Enum):
    NO_CHANGES = "no_changes"
    QUALITY_REJECTED = "quality_rejected"
    APPROVED_QUEUED = "approved_queued"
    ERROR = "error"


class KnowledgeBasePipeline:
    def __init__(
        self,
        clock,
        fetch_documents,          # callable() -> list[Document]; raise on outage
        eval_fn,                  # callable(docs_snapshot) -> QualityMetrics
        maintenance_window: MaintenanceWindow,
        health_check_fn,          # callable() -> bool
        daily_update_time,        # datetime.time
        access_control: AccessControl | None = None,
        accuracy_tolerance: float = 0.0,
        grounding_tolerance: float = 0.0,
        retry_delays_min=(15, 30, 60),
        max_doc_size: int = 2_000_000,
        allowed_ext=(".txt", ".md", ".html", ".json"),
        near_dup_hamming_threshold: int = 3,
    ):
        self.clock = clock
        self.fetch_documents = fetch_documents
        self.access = access_control or AccessControl(clock=clock)

        self.injection_guard = PromptInjectionGuard()
        self.masker = DataMasker()
        self.doc_processor = DocumentProcessor(
            max_size=max_doc_size,
            allowed_ext=allowed_ext,
            injection_guard=self.injection_guard,
            masker=self.masker,
            near_dup_hamming_threshold=near_dup_hamming_threshold,
        )

        self.vm = VersionManager(clock)
        self.quality_gate = QualityGate(eval_fn, accuracy_tolerance, grounding_tolerance)
        self.metrics = MetricsRegistry()

        # Manifest / simhash index of what's currently live in the KB.
        self.manifest: dict[str, str] = {}
        self.simhash_index: dict[str, int] = {}
        self.baseline_metrics = None

        self.activation = ActivationManager(clock, self.vm, maintenance_window, health_check_fn)
        self.scheduler = Scheduler(clock, self._scheduled_run, daily_update_time, retry_delays_min)

        self.reports: list[dict] = []

    # -- scheduled job -----------------------------------------------------

    def _scheduled_run(self) -> bool:
        outcome = self.run_update_cycle(user="system")
        return outcome != UpdateOutcome.ERROR

    def run_update_cycle(self, user: str = "system") -> UpdateOutcome:
        """
        Fetch -> validate/dedup/quarantine -> version -> quality gate ->
        queue for maintenance-window activation. Raises on fetch failure
        (e.g. simulated service outage) so the Scheduler's retry logic
        (15/30/60 min) can catch it.
        """
        self.access.check(user, "ingest")

        files = self.fetch_documents()  # may raise -> propagates to Scheduler as a failure

        result = self.doc_processor.process_batch(files, self.manifest, self.simhash_index)
        changed_docs = result.changed()

        report = {
            "time": self.clock.now(),
            "new": len(result.new),
            "modified": len(result.modified),
            "unchanged": len(result.unchanged),
            "duplicates": len(result.duplicates),
            "quarantined": len(result.quarantined),
            "quarantine_reasons": [r.value for _, r in result.quarantined],
            "pii_found": result.pii_found,
        }

        if not changed_docs:
            report["outcome"] = UpdateOutcome.NO_CHANGES
            self.reports.append(report)
            return UpdateOutcome.NO_CHANGES

        new_manifest = dict(self.manifest)
        new_snapshot = dict(self._current_snapshot())
        for doc in changed_docs:
            new_manifest[doc.doc_id] = doc.hash
            new_snapshot[doc.doc_id] = doc.text

        candidate = self.vm.create_candidate(new_manifest, new_snapshot)
        passed, metrics, reasons = self.quality_gate.evaluate(new_snapshot, self.baseline_metrics)
        candidate.quality_report = {"metrics": vars(metrics), "passed": passed, "reasons": reasons}

        if not passed:
            self.vm.reject(candidate.version_id, "; ".join(reasons))
            report["outcome"] = UpdateOutcome.QUALITY_REJECTED
            report["version_id"] = candidate.version_id
            report["reasons"] = reasons
            self.reports.append(report)
            return UpdateOutcome.QUALITY_REJECTED

        self.vm.approve(candidate.version_id)
        self.activation.queue_for_activation(candidate.version_id)

        # Commit accepted changes into the live manifest/simhash index so
        # they won't be reprocessed as "new" next cycle. NOTE: this makes
        # them count as "live" for de-dup purposes immediately; the actual
        # KB *content* only goes live once ActivationManager activates the
        # version during the maintenance window.
        for doc in changed_docs:
            self.manifest[doc.doc_id] = doc.hash
            self.simhash_index[doc.doc_id] = doc.simhash_val
        self.baseline_metrics = metrics

        report["outcome"] = UpdateOutcome.APPROVED_QUEUED
        report["version_id"] = candidate.version_id
        self.reports.append(report)
        return UpdateOutcome.APPROVED_QUEUED

    def _current_snapshot(self) -> dict:
        active = self.vm.active()
        return active.docs_snapshot if active else {}

    # -- driving the pipeline -----------------------------------------------

    def tick(self) -> None:
        """Call periodically (e.g. every minute) to drive scheduling & activation."""
        self.scheduler.tick()
        self.activation.tick()

    # -- admin operations -----------------------------------------------

    def rollback(self, user: str):
        self.access.check(user, "rollback")
        return self.vm.rollback()

    def get_metrics(self, user: str) -> dict:
        self.access.check(user, "view_metrics")
        return self.metrics.summary()

    # -- simulated query path (demonstrates injection guard + monitoring) --

    def query(
        self,
        user: str,
        prompt: str,
        latency_ms: float = 50.0,
        confidence: float = 0.9,
        success: bool = True,
    ) -> dict:
        self.access.check(user, "query")
        hits = self.injection_guard.scan(prompt)
        escalate = bool(hits) or confidence < 0.5
        if escalate:
            self.metrics.record_escalation(reason="prompt_injection" if hits else "low_confidence")
        self.metrics.record_request(latency_ms, success, confidence)
        return {"escalated": escalate, "injection_flagged": hits}
