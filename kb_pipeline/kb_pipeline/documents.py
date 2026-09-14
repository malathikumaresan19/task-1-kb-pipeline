"""
Document ingestion pipeline stage.

Responsibilities:
  1. Validate each incoming file (size, extension, encoding, emptiness).
     Invalid files are quarantined with a reason, never reaching the KB.
  2. Detect exact duplicates (hash match) and near-duplicates (simhash)
     against both the existing knowledge base and other files in the same
     batch.
  3. Classify every remaining file as new / modified / unchanged relative
     to the last known-good manifest, so only new-or-modified documents
     flow further down the pipeline.
  4. Scan for prompt-injection content and mask sensitive data before the
     text is allowed into the candidate snapshot.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum

from .hashing import content_hash, simhash, hamming_distance
from .security import PromptInjectionGuard, DataMasker


@dataclass
class Document:
    doc_id: str
    path: str
    raw: bytes
    text: str = ""
    hash: str = ""
    simhash_val: int = 0

    def __post_init__(self):
        if not self.text and self.raw:
            try:
                self.text = self.raw.decode("utf-8")
            except UnicodeDecodeError:
                self.text = ""


class QuarantineReason(str, Enum):
    EMPTY = "empty_document"
    TOO_LARGE = "exceeds_size_limit"
    BAD_ENCODING = "invalid_encoding"
    DISALLOWED_TYPE = "disallowed_file_type"
    INJECTION_DETECTED = "prompt_injection_detected"


@dataclass
class IngestionResult:
    new: list = field(default_factory=list)
    modified: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    quarantined: list = field(default_factory=list)  # list[(Document, reason)]
    pii_found: dict = field(default_factory=dict)     # doc_id -> {category: count}

    def changed(self):
        return self.new + self.modified


class DocumentProcessor:
    def __init__(
        self,
        max_size: int = 2_000_000,
        allowed_ext: tuple = (".txt", ".md", ".html", ".json"),
        injection_guard: PromptInjectionGuard | None = None,
        masker: DataMasker | None = None,
        near_dup_hamming_threshold: int = 3,
    ):
        self.max_size = max_size
        self.allowed_ext = allowed_ext
        self.injection_guard = injection_guard or PromptInjectionGuard()
        self.masker = masker or DataMasker()
        self.near_dup_threshold = near_dup_hamming_threshold

    def _validate(self, doc: Document) -> QuarantineReason | None:
        if not any(doc.path.lower().endswith(ext) for ext in self.allowed_ext):
            return QuarantineReason.DISALLOWED_TYPE
        if len(doc.raw) == 0 or not doc.text.strip():
            return QuarantineReason.EMPTY
        if len(doc.raw) > self.max_size:
            return QuarantineReason.TOO_LARGE
        try:
            doc.raw.decode("utf-8")
        except UnicodeDecodeError:
            return QuarantineReason.BAD_ENCODING
        return None

    def process_batch(
        self,
        files: list[Document],
        manifest: dict[str, str],
        existing_simhashes: dict[str, int],
    ) -> IngestionResult:
        """
        manifest: doc_id -> content hash of the last known-good version of
                  each document (i.e. what's currently live in the KB).
        existing_simhashes: doc_id -> simhash of the last known-good text,
                  used for near-duplicate comparison against the live KB.
        """
        result = IngestionResult()
        seen_hashes_in_batch: set[str] = set()
        seen_simhashes_in_batch: dict[str, int] = {}

        for doc in files:
            reason = self._validate(doc)
            if reason is not None:
                result.quarantined.append((doc, reason))
                continue

            doc.hash = content_hash(doc.raw)
            prior_hash = manifest.get(doc.doc_id)

            # Unchanged: identical content already live for this doc_id.
            if prior_hash is not None and prior_hash == doc.hash:
                result.unchanged.append(doc)
                continue

            # Exact duplicate: same bytes as some *other* doc_id, either
            # already live in the KB or elsewhere in this batch.
            other_live_hashes = {h for did, h in manifest.items() if did != doc.doc_id}
            if doc.hash in other_live_hashes or doc.hash in seen_hashes_in_batch:
                result.duplicates.append(doc)
                continue

            # Near-duplicate: similar-but-not-identical text vs. the live KB
            # or vs. another file already accepted in this same batch.
            doc.simhash_val = simhash(doc.text)
            other_live_simhashes = {did: sh for did, sh in existing_simhashes.items() if did != doc.doc_id}
            candidates = list(other_live_simhashes.values()) + list(seen_simhashes_in_batch.values())
            if any(hamming_distance(doc.simhash_val, sh) <= self.near_dup_threshold for sh in candidates):
                result.duplicates.append(doc)
                continue

            # Prompt-injection scan: quarantine rather than silently strip,
            # so a human can review content that was clearly trying to plant
            # instructions in the knowledge base.
            _, hits = self.injection_guard.sanitize(doc.text)
            if hits:
                result.quarantined.append((doc, QuarantineReason.INJECTION_DETECTED))
                continue

            # Sensitive-data masking before the text is allowed into the
            # candidate snapshot.
            masked_text, pii = self.masker.mask(doc.text)
            doc.text = masked_text
            if pii:
                result.pii_found[doc.doc_id] = pii

            seen_hashes_in_batch.add(doc.hash)
            seen_simhashes_in_batch[doc.doc_id] = doc.simhash_val

            if prior_hash is None:
                result.new.append(doc)
            else:
                result.modified.append(doc)

        return result
