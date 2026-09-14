"""Exact and near-duplicate detection helpers."""
from __future__ import annotations
import hashlib
import re


def content_hash(data: bytes) -> str:
    """Exact-duplicate fingerprint of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def simhash(text: str, hashbits: int = 64) -> int:
    """
    Cheap, dependency-free simhash for near-duplicate detection
    (e.g. a document re-saved with whitespace/formatting changes,
    or with a paragraph added/removed).
    """
    tokens = re.findall(r"\w+", normalize_text(text))
    if not tokens:
        return 0
    v = [0] * hashbits
    for tok in tokens:
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        for i in range(hashbits):
            v[i] += 1 if (h >> i) & 1 else -1
    fingerprint = 0
    for i in range(hashbits):
        if v[i] > 0:
            fingerprint |= (1 << i)
    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")
