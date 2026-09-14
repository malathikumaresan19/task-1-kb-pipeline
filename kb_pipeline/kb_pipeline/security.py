"""
Security controls used throughout the pipeline:

- AccessControl: RBAC for pipeline operations (ingest / approve / activate /
  rollback / view_metrics / query), with an audit log so unauthorized
  attempts are traceable.
- PromptInjectionGuard: scans ingested documents (and live queries) for
  instruction-override / jailbreak patterns before they ever reach the KB
  or the model context.
- DataMasker: redacts common categories of sensitive data (emails, phone
  numbers, SSNs, credit card numbers, API keys/secrets) before content is
  indexed.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

class AccessDenied(Exception):
    pass


class Role:
    VIEWER = "viewer"
    EDITOR = "editor"
    APPROVER = "approver"
    ADMIN = "admin"


# Which roles may perform which pipeline action. ADMIN can do everything.
PERMISSIONS = {
    "ingest": {Role.EDITOR, Role.ADMIN},
    "approve": {Role.APPROVER, Role.ADMIN},
    "activate": {Role.ADMIN},
    "rollback": {Role.ADMIN},
    "view_metrics": {Role.VIEWER, Role.EDITOR, Role.APPROVER, Role.ADMIN},
    "query": {Role.VIEWER, Role.EDITOR, Role.APPROVER, Role.ADMIN},
}


@dataclass
class AuditEntry:
    user: str
    action: str
    role: str | None
    allowed: bool
    timestamp: object = None


class AccessControl:
    def __init__(self, clock=None):
        self._users: dict[str, str] = {}
        self._audit_log: list[AuditEntry] = []
        self._clock = clock

    def register_user(self, user: str, role: str) -> None:
        self._users[user] = role

    def role_of(self, user: str) -> str | None:
        return self._users.get(user)

    def check(self, user: str, action: str) -> bool:
        role = self._users.get(user)
        allowed = role in PERMISSIONS.get(action, set())
        self._audit_log.append(AuditEntry(
            user=user, action=action, role=role, allowed=allowed,
            timestamp=self._clock.now() if self._clock else None,
        ))
        if not allowed:
            raise AccessDenied(
                f"user '{user}' (role={role!r}) is not permitted to perform '{action}'"
            )
        return True

    def audit_log(self) -> list[AuditEntry]:
        return list(self._audit_log)

    def unauthorized_attempts(self) -> list[AuditEntry]:
        return [e for e in self._audit_log if not e.allowed]


# ---------------------------------------------------------------------------
# Prompt-injection protection
# ---------------------------------------------------------------------------

DEFAULT_INJECTION_PATTERNS = [
    r"ignore (all|any)? ?(previous|prior|above) instructions",
    r"disregard (all|any)? ?(previous|prior) (instructions|rules)",
    r"system prompt",
    r"you are now (a|an)?",
    r"act as (an?|the) (unrestricted|jailbroken|uncensored)",
    r"reveal (your|the) (system prompt|hidden instructions|instructions)",
    r"\bDAN\b",
    r"override (safety|guardrails|restrictions)",
    r"pretend (you|to) (have no|are not) (restrictions|rules|guidelines)",
    r"new instructions?:",
]


class PromptInjectionGuard:
    def __init__(self, patterns: list[str] | None = None):
        self.patterns = [re.compile(p, re.IGNORECASE) for p in (patterns or DEFAULT_INJECTION_PATTERNS)]

    def scan(self, text: str) -> list[str]:
        """Return the list of pattern strings that matched."""
        return [p.pattern for p in self.patterns if p.search(text or "")]

    def sanitize(self, text: str) -> tuple[str, list[str]]:
        """Return (redacted_text, matched_patterns)."""
        hits = self.scan(text)
        clean = text or ""
        for p in self.patterns:
            clean = p.sub("[REDACTED-INSTRUCTION]", clean)
        return clean, hits


# ---------------------------------------------------------------------------
# Sensitive data masking
# ---------------------------------------------------------------------------

DEFAULT_PII_PATTERNS = {
    "email": r"[\w.+-]+@[\w-]+\.[\w.-]+",
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "credit_card": r"\b(?:\d[ -]?){13,16}\b",
    "phone": r"\b(?:\+?\d{1,2}[- .]?)?\(?\d{3}\)?[- .]\d{3}[- .]\d{4}\b",
    "api_key": r"\b(?:sk|api|key|token)[-_][A-Za-z0-9]{16,}\b",
}


class DataMasker:
    def __init__(self, patterns: dict[str, str] | None = None):
        self.patterns = {name: re.compile(p) for name, p in (patterns or DEFAULT_PII_PATTERNS).items()}

    def mask(self, text: str) -> tuple[str, dict[str, int]]:
        masked = text or ""
        found: dict[str, int] = {}
        for name, pat in self.patterns.items():
            matches = pat.findall(masked)
            if matches:
                found[name] = len(matches)
                masked = pat.sub(f"[{name.upper()}_REDACTED]", masked)
        return masked, found
