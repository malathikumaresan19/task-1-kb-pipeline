# Knowledge Base Update Pipeline

A production pipeline for updating and monitoring a chatbot's knowledge base (KB).
Pure Python, no external dependencies for the core logic (pytest only needed to run tests).

## What it does

| Requirement | Where it's implemented |
|---|---|
| Process only new/modified documents | `documents.DocumentProcessor` diffs incoming files against a manifest of content hashes; unchanged files are skipped entirely |
| Detect duplicates | `hashing.py` — exact match via SHA-256, near-duplicate via simhash + Hamming distance, checked against both the live KB and the current batch |
| Quarantine invalid files | `documents.py` — bad encoding, disallowed type, empty, oversized, or prompt-injection content is quarantined with a reason, never reaches the KB |
| Maintain versions / rollback | `versioning.VersionManager` — immutable, timestamped `KBVersion` snapshots with full activation history; `rollback()` reverts to the last healthy active version |
| Quality tests before activation, reject regressions | `quality.QualityGate` — pluggable `eval_fn`; rejects a candidate if accuracy/grounding would regress past baseline, or error rate is too high |
| Configurable schedule, retry at 15/30/60 min | `scheduler.Scheduler` — daily trigger at a configurable `time`; on failure (exception or `False` return, e.g. a source outage) retries after 15, then 30, then 60 minutes, then gives up until the next scheduled run |
| Activate only during maintenance window | `activation.ActivationManager` + `MaintenanceWindow` — approved candidates queue until the current time falls inside the window |
| Auto-rollback if health checks fail within 5 min | `activation.ActivationManager` — polls `health_check_fn` for a configurable duration (default 5 min) after activation; first failure triggers `VersionManager.rollback()` |
| Access control | `security.AccessControl` — RBAC (`viewer`/`editor`/`approver`/`admin`) with an audit log of every allowed/denied action |
| Prompt-injection protection | `security.PromptInjectionGuard` — scans both ingested documents (quarantines on hit) and live queries (flags for escalation) |
| Sensitive-data masking | `security.DataMasker` — redacts emails, phone numbers, SSNs, credit-card numbers, API keys before text is indexed |
| Monitoring: latency, failures, confidence, escalations | `monitoring.MetricsRegistry` — `record_request()` / `record_escalation()` / `summary()` |

## Design choice: everything is driven by an injectable clock

Every time-dependent behavior (retry backoff, maintenance windows, the 5-minute
health-check window) reads time through a `Clock`. In production you use the
real `Clock()` and call `pipeline.tick()` once a minute (cron, background
thread, etc.). In tests — or in an automated/simulated evaluation — you use
`SimulatedClock`, call `.advance(minutes=15)` etc., and then `tick()`. This
makes "wait 15/30/60 minutes" and "monitor for 5 minutes" fully deterministic
and instant to test, with no real sleeping and no flakiness.

## Quick start

```python
import datetime as dt
from kb_pipeline import (
    KnowledgeBasePipeline, Clock, MaintenanceWindow, Role, Document,
)

def fetch_documents():
    # Pull from your real source (S3, CMS, git repo, etc.)
    # Raise on failure -- the scheduler will retry at 15/30/60 min.
    ...
    return [Document(doc_id="faq_1", path="faq_1.md", raw=b"...")]

def eval_fn(docs_snapshot):
    # Plug in your real retrieval/answer-quality eval harness here.
    from kb_pipeline import QualityMetrics
    ...
    return QualityMetrics(accuracy=0.93, grounding=0.91, avg_latency_ms=140, error_rate=0.0)

def health_check_fn():
    # Ping the live serving stack after activation.
    return True

pipeline = KnowledgeBasePipeline(
    clock=Clock(),
    fetch_documents=fetch_documents,
    eval_fn=eval_fn,
    maintenance_window=MaintenanceWindow(start=dt.time(2, 0), end=dt.time(4, 0)),
    health_check_fn=health_check_fn,
    daily_update_time=dt.time(1, 0),
)

pipeline.access.register_user("etl-bot", Role.EDITOR)
pipeline.access.register_user("system", Role.EDITOR)   # runs the scheduled job
pipeline.access.register_user("alice", Role.ADMIN)

# Call once a minute, e.g. from a cron job / background thread:
pipeline.tick()

# Manual operations:
pipeline.run_update_cycle(user="etl-bot")     # trigger an update cycle now
pipeline.rollback(user="alice")               # manual rollback
pipeline.get_metrics(user="alice")             # latency / failure / confidence / escalation summary
```

## Update lifecycle

```
fetch_documents()
      │
      ▼
validate / quarantine  (bad encoding, empty, oversized, disallowed type, prompt injection)
      │
      ▼
dedup                    (exact hash + near-duplicate simhash, vs. live KB and vs. batch)
      │
      ▼
new / modified only  ──►  mask sensitive data  ──►  candidate KBVersion created
      │
      ▼
QualityGate.evaluate()  ──► regressed? ──► REJECTED (never queued, version marked "rejected")
      │  passed
      ▼
approved & queued for activation
      │
      ▼ (ActivationManager.tick(), only when now ∈ maintenance window)
ACTIVATED  ──►  health checks every interval for 5 minutes
      │                         │
      │                         └─ any failure → automatic rollback to previous active version
      ▼
stays active after the monitoring window elapses cleanly
```

Failures at the *fetch* stage (simulated outages, network errors, etc.) are
handled by the `Scheduler`, which retries the whole cycle at 15, 30, then 60
minutes before giving up until the next scheduled run. Failures at the
*quality gate* stage are not retried — they are a deliberate reject, logged
with the specific regression reasons.

## Running the tests

```bash
pip install pytest
python -m pytest tests/ -v
```

The test suite (`tests/test_pipeline.py`) exercises, deterministically via
`SimulatedClock`:

- exact and near-duplicate document detection
- unchanged documents being skipped (not reprocessed)
- invalid files being quarantined (not treated as duplicates)
- a flaky update source recovering after 15/30 minutes of retries
- a fully-down source exhausting all retries (15 → 30 → 60) and giving up
- a quality regression being rejected instead of activated
- unauthorized users being denied ingest/rollback, with an audit trail
- prompt injection in both live queries and ingested documents
- sensitive data (email/phone) being masked before indexing
- an approved update sitting queued until the maintenance window opens
- a failing health check triggering automatic rollback inside the 5-minute window
- a healthy version remaining active once the monitoring window elapses
- manual rollback reverting to the previous version

## Extending for production

- **`fetch_documents`**: point at your real document source (git, CMS, S3 bucket, etc.). Track a `path`/`doc_id` scheme stable across runs so "modified" detection works.
- **`eval_fn`**: replace the bundled heuristic with a real eval harness — run a fixed regression test set against a retrieval index built from the candidate snapshot, and score accuracy/groundedness against your baseline.
- **`health_check_fn`**: wire to real synthetic queries against the newly-activated serving stack, or to APM/error-rate signals.
- **`MetricsRegistry`**: swap for your real metrics backend (Prometheus, Datadog, etc.) by calling `record_request`/`record_escalation` from your serving layer, or by having `summary()` read from that backend instead.
- **`AccessControl`**: back `register_user`/`role_of` with your real identity provider (SSO/OAuth claims) instead of the in-memory dict.
