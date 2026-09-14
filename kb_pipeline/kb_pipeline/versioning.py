"""
Knowledge-base versioning.

Every processed batch of new/modified documents produces a new immutable
KBVersion (a "candidate"). Candidates move through:

    candidate -> approved -> active -> superseded
                       -> rejected              -> rolled_back

VersionManager keeps the full activation history so rollback() can always
step back to the last version that was actually active and healthy.
"""
from __future__ import annotations
import datetime as dt
from dataclasses import dataclass, field


@dataclass
class KBVersion:
    version_id: int
    created_at: dt.datetime
    manifest: dict          # doc_id -> content hash, full KB state at this version
    docs_snapshot: dict     # doc_id -> text, full KB content at this version
    status: str = "candidate"
    quality_report: dict = field(default_factory=dict)
    parent: int | None = None


class VersionManager:
    def __init__(self, clock):
        self.clock = clock
        self.versions: dict[int, KBVersion] = {}
        self.active_version_id: int | None = None
        self._next_id = 1
        self._activation_history: list[int] = []  # chronological list of activated version ids

    def create_candidate(self, manifest: dict, docs_snapshot: dict) -> KBVersion:
        vid = self._next_id
        self._next_id += 1
        v = KBVersion(
            version_id=vid,
            created_at=self.clock.now(),
            manifest=dict(manifest),
            docs_snapshot=dict(docs_snapshot),
            status="candidate",
            parent=self.active_version_id,
        )
        self.versions[vid] = v
        return v

    def approve(self, vid: int) -> None:
        self.versions[vid].status = "approved"

    def reject(self, vid: int, reason: str = "") -> None:
        v = self.versions[vid]
        v.status = "rejected"
        v.quality_report["rejection_reason"] = reason

    def activate(self, vid: int) -> None:
        v = self.versions[vid]
        if v.status not in ("approved",):
            raise ValueError(f"version {vid} must be 'approved' before activation (is '{v.status}')")
        if self.active_version_id is not None:
            prev = self.versions.get(self.active_version_id)
            if prev and prev.status == "active":
                prev.status = "superseded"
        v.status = "active"
        self._activation_history.append(vid)
        self.active_version_id = vid

    def rollback(self) -> int | None:
        """
        Roll back the currently active version. Returns the version_id now
        active, or None if there was nothing healthy to roll back to (the
        KB reverts to an empty/pre-pipeline baseline in that case).
        """
        if not self._activation_history:
            return None
        failed_vid = self._activation_history.pop()
        if failed_vid in self.versions:
            self.versions[failed_vid].status = "rolled_back"
        if self._activation_history:
            prev_vid = self._activation_history[-1]
            self.versions[prev_vid].status = "active"
            self.active_version_id = prev_vid
            return prev_vid
        self.active_version_id = None
        return None

    def active(self) -> KBVersion | None:
        if self.active_version_id is None:
            return None
        return self.versions.get(self.active_version_id)

    def history(self) -> list[int]:
        return list(self._activation_history)
