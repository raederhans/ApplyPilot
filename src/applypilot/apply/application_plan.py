"""Immutable application plans and an opt-in host-submit seam.

This module is deliberately not wired into the production worker.  It lets the
current Agent path be shadowed with content-addressed plans and deterministic
audit receipts while keeping every host-side effect behind an explicit feature
gate.  Browser-form submission still follows the existing safety order; direct
email remains a mailbox-owned route and is never accepted by HostSubmitExecutor.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from typing import Literal, Protocol, runtime_checkable

ApplicationRoute = Literal["browser_form", "direct_email"]
AuditDisposition = Literal["clear", "blocked"]
ObservationDisposition = Literal["confirmed", "uncertain", "blocked"]
ReceiptStatus = Literal["admitted", "uncertain", "rejected"]
AttemptSubmitState = Literal["submit_started", "receipt_only", "terminal"]

FACT_KEY_CODES = frozenset(
    {
        "availability",
        "city",
        "country",
        "email",
        "legal_name",
        "phone",
        "portfolio_url",
        "postal_code",
        "preferred_name",
        "salary_expectation",
        "sponsorship",
        "state",
        "work_authorization",
    }
)
FACT_SCOPE_CODES = frozenset({"application", "candidate_profile", "employer", "job", "jurisdiction"})
MATERIAL_PURPOSE_CODES = frozenset({"cover_letter", "portfolio", "resume", "transcript", "writing_sample"})
FIELD_SEMANTIC_CODES = FACT_KEY_CODES | frozenset({"cover_letter", "resume", "transcript", "writing_sample"})
PROVIDER_CODES = frozenset(
    {
        "ashby",
        "direct_email",
        "generic",
        "greenhouse",
        "icims",
        "lever",
        "linkedin",
        "smartrecruiters",
        "taleo",
        "workday",
    }
)
TARGET_SEMANTIC_CODES = frozenset({"application_form", "application_review", "direct_email_application"})

BROWSER_SUBMIT_STAGES = (
    "pre_submit_audit",
    "global_submit_lane",
    "reservation",
    "submit_started",
    "single_use_submit_authority",
    "submit_once",
    "independent_observer",
    "receipt_reconciliation",
)
DIRECT_EMAIL_STAGES = (
    "pre_submit_audit",
    "global_submit_lane",
    "reservation",
    "submit_started",
    "single_use_send_authority",
    "mailbox_send_once",
    "sent_copy_observer",
    "receipt_reconciliation",
)


class HostSubmitDenied(RuntimeError):
    """The host-submit boundary failed closed before a new submit effect."""


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


def _sha256(value: object, name: str) -> str:
    text = _required(value, name).casefold()
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return text


def _content_ref(value: object, name: str) -> str:
    text = _required(value, name)
    scheme, separator, digest = text.partition(":")
    if separator != ":" or scheme != "sha256":
        raise ValueError(f"{name} must be a sha256 content reference")
    return f"sha256:{_sha256(digest, name)}"


def _opaque_binding(value: object, name: str) -> str:
    raw = _required(value, name).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _code(value: object, name: str, allowed: frozenset[str]) -> str:
    text = _required(value, name)
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", text) or text not in allowed:
        raise ValueError(f"{name} is not an admitted symbolic code")
    return text


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _canonical_codes(values: Sequence[str], name: str) -> tuple[str, ...]:
    normalized = tuple(sorted({_required(value, name) for value in values}))
    if any(not value.replace("_", "").isalnum() or value != value.upper() for value in normalized):
        raise ValueError(f"{name} must contain stable uppercase symbolic codes")
    return normalized


@dataclass(frozen=True, slots=True)
class FactRef:
    """Reference to one exact-scope fact; it intentionally carries no value."""

    fact_ref: str
    key: str
    scope: str
    value_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "fact_ref", _content_ref(self.fact_ref, "fact_ref"))
        object.__setattr__(self, "key", _code(self.key, "key", FACT_KEY_CODES))
        object.__setattr__(self, "scope", _code(self.scope, "scope", FACT_SCOPE_CODES))
        object.__setattr__(self, "value_sha256", _sha256(self.value_sha256, "value_sha256"))

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": "fact",
            "ref": self.fact_ref,
            "key": self.key,
            "scope": self.scope,
            "value_sha256": self.value_sha256,
        }


@dataclass(frozen=True, slots=True)
class MaterialRef:
    """Reference to a verified application artifact; it carries no filesystem path."""

    material_ref: str
    purpose: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "material_ref",
            _content_ref(self.material_ref, "material_ref"),
        )
        object.__setattr__(
            self,
            "purpose",
            _code(self.purpose, "purpose", MATERIAL_PURPOSE_CODES),
        )
        object.__setattr__(self, "content_sha256", _sha256(self.content_sha256, "content_sha256"))

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": "material",
            "ref": self.material_ref,
            "purpose": self.purpose,
            "content_sha256": self.content_sha256,
        }


@dataclass(frozen=True, slots=True)
class ProvenanceRef:
    """Reference to host-issued field provenance; raw answers stay outside the plan."""

    provenance_ref: str
    field_semantic: str
    snapshot_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provenance_ref",
            _content_ref(self.provenance_ref, "provenance_ref"),
        )
        object.__setattr__(
            self,
            "field_semantic",
            _code(self.field_semantic, "field_semantic", FIELD_SEMANTIC_CODES),
        )
        object.__setattr__(self, "snapshot_sha256", _sha256(self.snapshot_sha256, "snapshot_sha256"))

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": "provenance",
            "ref": self.provenance_ref,
            "field_semantic": self.field_semantic,
            "snapshot_sha256": self.snapshot_sha256,
        }


PlanRef = FactRef | MaterialRef | ProvenanceRef


def _ref_identity(value: PlanRef) -> tuple[str, str]:
    if isinstance(value, FactRef):
        return "fact", value.fact_ref
    if isinstance(value, MaterialRef):
        return "material", value.material_ref
    if isinstance(value, ProvenanceRef):
        return "provenance", value.provenance_ref
    raise TypeError("application plan references must use typed reference classes")


def _freeze_refs(values: Sequence[PlanRef], expected: type[PlanRef], name: str) -> tuple[PlanRef, ...]:
    frozen = tuple(values)
    if any(type(value) is not expected for value in frozen):
        raise TypeError(f"{name} must contain only {expected.__name__}")
    return tuple(sorted(frozen, key=lambda value: _ref_identity(value)))


@dataclass(frozen=True, slots=True)
class ApplicationPlan:
    """Content-addressed plan input with no browser handle or submit authority."""

    plan_id: str
    attempt_id: str
    revision: int
    route: ApplicationRoute
    provider: str
    target_semantic_code: str
    target_binding_ref: str
    fact_refs: tuple[FactRef, ...] = ()
    material_refs: tuple[MaterialRef, ...] = ()
    provenance_refs: tuple[ProvenanceRef, ...] = ()
    parent_plan_sha256: str | None = None
    schema_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_id", _required(self.plan_id, "plan_id"))
        object.__setattr__(self, "attempt_id", _required(self.attempt_id, "attempt_id"))
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError("revision must be a positive integer")
        if self.route not in {"browser_form", "direct_email"}:
            raise ValueError("route must be browser_form or direct_email")
        object.__setattr__(self, "provider", _code(self.provider, "provider", PROVIDER_CODES))
        object.__setattr__(
            self,
            "target_semantic_code",
            _code(
                self.target_semantic_code,
                "target_semantic_code",
                TARGET_SEMANTIC_CODES,
            ),
        )
        if (self.route == "direct_email" and self.target_semantic_code != "direct_email_application") or (
            self.route == "browser_form" and self.target_semantic_code == "direct_email_application"
        ):
            raise ValueError("target_semantic_code does not match the application route")
        object.__setattr__(
            self,
            "target_binding_ref",
            _content_ref(self.target_binding_ref, "target_binding_ref"),
        )
        object.__setattr__(self, "schema_version", _required(self.schema_version, "schema_version"))
        object.__setattr__(
            self,
            "fact_refs",
            _freeze_refs(self.fact_refs, FactRef, "fact_refs"),
        )
        object.__setattr__(
            self,
            "material_refs",
            _freeze_refs(self.material_refs, MaterialRef, "material_refs"),
        )
        object.__setattr__(
            self,
            "provenance_refs",
            _freeze_refs(self.provenance_refs, ProvenanceRef, "provenance_refs"),
        )
        identities = [_ref_identity(value) for value in self.all_refs]
        if len(set(identities)) != len(identities):
            raise ValueError("application plan typed references must be unique")
        if self.parent_plan_sha256 is not None:
            object.__setattr__(
                self,
                "parent_plan_sha256",
                _sha256(self.parent_plan_sha256, "parent_plan_sha256"),
            )
        if (self.revision == 1) != (self.parent_plan_sha256 is None):
            raise ValueError("only revision 1 may omit parent_plan_sha256")

    @property
    def all_refs(self) -> tuple[PlanRef, ...]:
        return (*self.fact_refs, *self.material_refs, *self.provenance_refs)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_binding_ref": _opaque_binding(self.plan_id, "plan_id"),
            "attempt_binding_ref": _opaque_binding(self.attempt_id, "attempt_id"),
            "revision": self.revision,
            "route": self.route,
            "provider": self.provider,
            "target_semantic_code": self.target_semantic_code,
            "target_binding_ref": self.target_binding_ref,
            "parent_plan_sha256": self.parent_plan_sha256,
            "fact_refs": [value.as_dict() for value in self.fact_refs],
            "material_refs": [value.as_dict() for value in self.material_refs],
            "provenance_refs": [value.as_dict() for value in self.provenance_refs],
            "host_authority": False,
            "submit_authority": False,
        }

    @property
    def digest(self) -> str:
        return _digest(self.as_dict())


def render_application_plan_delta(
    plan: ApplicationPlan,
    *,
    previous: ApplicationPlan | None = None,
) -> str:
    """Render a deterministic ref-only continuation prompt for one plan revision."""

    if previous is None:
        if plan.revision != 1 or plan.parent_plan_sha256 is not None:
            raise ValueError("an initial delta requires revision 1 without a parent")
        previous_refs: dict[tuple[str, str], PlanRef] = {}
        base_digest: str | None = None
    else:
        if (
            plan.attempt_id != previous.attempt_id
            or plan.plan_id != previous.plan_id
            or plan.revision != previous.revision + 1
            or plan.parent_plan_sha256 != previous.digest
        ):
            raise ValueError("plan delta does not continue the exact previous plan")
        previous_refs = {_ref_identity(value): value for value in previous.all_refs}
        base_digest = previous.digest
    current_refs = {_ref_identity(value): value for value in plan.all_refs}
    added = [current_refs[key].as_dict() for key in sorted(current_refs.keys() - previous_refs.keys())]
    removed = [previous_refs[key].as_dict() for key in sorted(previous_refs.keys() - current_refs.keys())]
    changed = [
        {"before": previous_refs[key].as_dict(), "after": current_refs[key].as_dict()}
        for key in sorted(current_refs.keys() & previous_refs.keys())
        if current_refs[key] != previous_refs[key]
    ]
    payload = {
        "schema": "ApplicationPlanDelta/v1",
        "base_plan_sha256": base_digest,
        "plan_sha256": plan.digest,
        "attempt_binding_ref": _opaque_binding(plan.attempt_id, "attempt_id"),
        "revision": plan.revision,
        "route": plan.route,
        "provider": plan.provider,
        "target_semantic_code": plan.target_semantic_code,
        "target_binding_ref": plan.target_binding_ref,
        "added_refs": added,
        "changed_refs": changed,
        "removed_refs": removed,
        "authority": {"browser_write": False, "reservation": False, "submit": False},
    }
    route_rule = (
        "Execution route is mailbox-only; browser HostSubmit is forbidden."
        if plan.route == "direct_email"
        else "Host audit, reservation, SubmitAuthority, observation, and receipt stay host-owned."
    )
    return (
        "APPLICATION_PLAN_DELTA_V1\n"
        f"{_canonical_json(payload).decode('ascii')}\n"
        "Resolve only the typed references in this delta; it contains no raw fact or material values.\n"
        f"{route_rule}"
    )


@dataclass(frozen=True, slots=True)
class HostAuditReceipt:
    """Deterministic host-issued proof of an exact plan audit; never authority."""

    plan_sha256: str
    attempt_id: str
    route: ApplicationRoute
    target_binding_ref: str
    audit_report_ref: str
    disposition: AuditDisposition
    blocker_codes: tuple[str, ...]
    issuer_id: str
    signature: str
    schema_version: str = "1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _sha256(self.plan_sha256, "plan_sha256"))
        object.__setattr__(self, "attempt_id", _required(self.attempt_id, "attempt_id"))
        if self.route not in {"browser_form", "direct_email"}:
            raise ValueError("audit receipt route is invalid")
        object.__setattr__(
            self,
            "target_binding_ref",
            _content_ref(self.target_binding_ref, "target_binding_ref"),
        )
        object.__setattr__(self, "audit_report_ref", _content_ref(self.audit_report_ref, "audit_report_ref"))
        if self.disposition not in {"clear", "blocked"}:
            raise ValueError("audit receipt disposition is invalid")
        object.__setattr__(
            self,
            "blocker_codes",
            _canonical_codes(self.blocker_codes, "blocker_codes"),
        )
        if (self.disposition == "clear") == bool(self.blocker_codes):
            raise ValueError("clear audits cannot have blockers and blocked audits require blockers")
        object.__setattr__(self, "issuer_id", _required(self.issuer_id, "issuer_id"))
        object.__setattr__(self, "signature", _required(self.signature, "signature"))
        object.__setattr__(self, "schema_version", _required(self.schema_version, "schema_version"))

    def claims(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "plan_sha256": self.plan_sha256,
            "attempt_binding_ref": _opaque_binding(self.attempt_id, "attempt_id"),
            "route": self.route,
            "target_binding_ref": self.target_binding_ref,
            "audit_report_ref": self.audit_report_ref,
            "disposition": self.disposition,
            "blocker_codes": list(self.blocker_codes),
        }

    @property
    def digest(self) -> str:
        return _digest(self.claims())


class HostAuditReceiptIssuer:
    """Host-local authenticity for deterministic, content-addressed receipts."""

    def __init__(self) -> None:
        self._issuer_id = secrets.token_hex(16)
        self._secret = secrets.token_bytes(32)
        self._attempt_latch = HostSubmitAttemptLatch()

    @property
    def attempt_latch(self) -> HostSubmitAttemptLatch:
        return self._attempt_latch

    def issue(
        self,
        plan: ApplicationPlan,
        *,
        audit_report_ref: str,
        disposition: AuditDisposition,
        blocker_codes: Sequence[str] = (),
        observed_target_ref: str | None = None,
    ) -> HostAuditReceipt:
        receipt = HostAuditReceipt(
            plan_sha256=plan.digest,
            attempt_id=plan.attempt_id,
            route=plan.route,
            target_binding_ref=observed_target_ref or plan.target_binding_ref,
            audit_report_ref=audit_report_ref,
            disposition=disposition,
            blocker_codes=tuple(blocker_codes),
            issuer_id=self._issuer_id,
            signature="pending",
        )
        return replace(receipt, signature=self._sign(receipt))

    def validate(self, receipt: HostAuditReceipt, plan: ApplicationPlan) -> None:
        if not isinstance(receipt, HostAuditReceipt) or receipt.issuer_id != self._issuer_id:
            raise HostSubmitDenied("host audit receipt was not issued by this host")
        unsigned = replace(receipt, signature="pending")
        if not hmac.compare_digest(receipt.signature, self._sign(unsigned)):
            raise HostSubmitDenied("host audit receipt signature is invalid")
        if (
            receipt.plan_sha256,
            receipt.attempt_id,
            receipt.route,
            receipt.target_binding_ref,
        ) != (
            plan.digest,
            plan.attempt_id,
            plan.route,
            plan.target_binding_ref,
        ):
            raise HostSubmitDenied("host audit receipt does not bind the exact application plan")
        if receipt.disposition != "clear" or receipt.blocker_codes:
            raise HostSubmitDenied("host audit receipt is not clear")

    def _sign(self, receipt: HostAuditReceipt) -> str:
        return hmac.new(self._secret, _canonical_json(receipt.claims()), hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class HostSubmitParityTrace:
    """Read-only projection of the established production submission stages."""

    route: ApplicationRoute
    interaction_driver: Literal["browser", "mailbox"]
    submit_owner: Literal["host", "mailbox"]
    stages: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.route not in {"browser_form", "direct_email"}:
            raise ValueError("parity trace route is invalid")
        if self.interaction_driver not in {"browser", "mailbox"}:
            raise ValueError("parity trace interaction_driver is invalid")
        if self.submit_owner not in {"host", "mailbox"}:
            raise ValueError("parity trace submit_owner is invalid")
        object.__setattr__(
            self,
            "stages",
            tuple(_required(stage, "stage") for stage in self.stages),
        )


@dataclass(frozen=True, slots=True)
class HostSubmitParityReport:
    plan_sha256: str
    parity: bool
    reason_code: str
    expected_stages: tuple[str, ...]
    observed_stages: tuple[str, ...]


def evaluate_host_submit_parity(
    plan: ApplicationPlan,
    trace: HostSubmitParityTrace,
) -> HostSubmitParityReport:
    """Compare a production trace without acquiring any host capability."""

    expected = BROWSER_SUBMIT_STAGES if plan.route == "browser_form" else DIRECT_EMAIL_STAGES
    expected_driver = "browser" if plan.route == "browser_form" else "mailbox"
    expected_owner = "host" if plan.route == "browser_form" else "mailbox"
    parity = (
        trace.route == plan.route
        and trace.interaction_driver == expected_driver
        and trace.submit_owner == expected_owner
        and trace.stages == expected
    )
    return HostSubmitParityReport(
        plan_sha256=plan.digest,
        parity=parity,
        reason_code="HOST_SUBMIT_PARITY" if parity else "HOST_SUBMIT_PARITY_MISMATCH",
        expected_stages=expected,
        observed_stages=trace.stages,
    )


@dataclass(frozen=True, slots=True)
class HostReservation:
    reservation_id: str
    plan_sha256: str
    audit_receipt_sha256: str
    route: ApplicationRoute = "browser_form"
    submit_owner: Literal["host", "mailbox"] = "host"
    admitted: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "reservation_id", _required(self.reservation_id, "reservation_id"))
        object.__setattr__(self, "plan_sha256", _sha256(self.plan_sha256, "plan_sha256"))
        object.__setattr__(
            self,
            "audit_receipt_sha256",
            _sha256(self.audit_receipt_sha256, "audit_receipt_sha256"),
        )
        if self.route not in {"browser_form", "direct_email"}:
            raise ValueError("reservation route is invalid")
        if self.submit_owner not in {"host", "mailbox"}:
            raise ValueError("reservation submit_owner is invalid")
        if not isinstance(self.admitted, bool):
            raise TypeError("reservation admitted must be bool")


@dataclass(frozen=True, slots=True)
class SubmitStartClaim:
    """Host-local proof that one attempt crossed submit_started exactly once."""

    attempt_id: str
    plan_sha256: str
    nonce: str

    def __reduce__(self) -> object:
        raise TypeError("SubmitStartClaim cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("SubmitStartClaim cannot be serialized")


@dataclass(slots=True)
class _AttemptLatchRecord:
    plan_sha256: str
    state: AttemptSubmitState
    claim: SubmitStartClaim
    authority_issued: bool = False


class HostSubmitAttemptLatch:
    """Shadow-only process-host monotonic submit latch keyed by attempt_id.

    Once ``begin`` succeeds, the attempt can only advance from submit_started
    to receipt_only and then terminal.  It can never enter another submit lane
    or mint another SubmitAuthority, even for a revised plan.  This is not a
    cross-process exactly-once guarantee.  Before production wiring, callers
    must restore the monotonic state from the durable ledger and reservation
    record into this process-host boundary before any submit admission check.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._attempts: dict[str, _AttemptLatchRecord] = {}

    def require_submit_allowed(self, plan: ApplicationPlan) -> None:
        with self._lock:
            record = self._attempts.get(plan.attempt_id)
            if record is not None:
                raise HostSubmitDenied(f"attempt is {record.state}; only independent reconcile is allowed")

    def begin(self, plan: ApplicationPlan) -> SubmitStartClaim:
        with self._lock:
            self.require_submit_allowed(plan)
            claim = SubmitStartClaim(
                attempt_id=plan.attempt_id,
                plan_sha256=plan.digest,
                nonce=secrets.token_hex(16),
            )
            self._attempts[plan.attempt_id] = _AttemptLatchRecord(
                plan_sha256=plan.digest,
                state="submit_started",
                claim=claim,
            )
            return claim

    def claim_authority(self, claim: SubmitStartClaim, plan: ApplicationPlan) -> None:
        with self._lock:
            record = self._require_claim(claim, plan)
            if record.state != "submit_started":
                raise HostSubmitDenied("attempt is receipt_only or terminal; authority is forbidden")
            if record.authority_issued:
                raise HostSubmitDenied("attempt already received its single SubmitAuthority")
            record.authority_issued = True

    def mark_receipt_only(self, claim: SubmitStartClaim, plan: ApplicationPlan) -> None:
        with self._lock:
            record = self._require_claim(claim, plan)
            if record.state == "submit_started":
                record.state = "receipt_only"

    def mark_terminal(self, claim: SubmitStartClaim, plan: ApplicationPlan) -> None:
        with self._lock:
            record = self._require_claim(claim, plan)
            if record.state not in {"submit_started", "receipt_only"}:
                raise HostSubmitDenied("terminal submit attempt cannot transition again")
            record.state = "terminal"

    def require_reconcile_only(self, plan: ApplicationPlan) -> SubmitStartClaim:
        with self._lock:
            record = self._attempts.get(plan.attempt_id)
            if record is None or record.plan_sha256 != plan.digest:
                raise HostSubmitDenied("attempt has no matching receipt_only submit state")
            if record.state != "receipt_only":
                raise HostSubmitDenied("attempt is not eligible for reconcile-only execution")
            return record.claim

    def state(self, attempt_id: str) -> AttemptSubmitState | None:
        with self._lock:
            record = self._attempts.get(attempt_id)
            return record.state if record is not None else None

    def _require_claim(
        self,
        claim: SubmitStartClaim,
        plan: ApplicationPlan,
    ) -> _AttemptLatchRecord:
        if not isinstance(claim, SubmitStartClaim):
            raise HostSubmitDenied("invalid submit-start claim")
        record = self._attempts.get(plan.attempt_id)
        if (
            record is None
            or record.claim != claim
            or record.plan_sha256 != plan.digest
            or claim.attempt_id != plan.attempt_id
            or claim.plan_sha256 != plan.digest
        ):
            raise HostSubmitDenied("submit-start claim does not bind this attempt and plan")
        return record


@dataclass(frozen=True, slots=True)
class SubmitAuthority:
    """One-shot, host-local capability for one exact reserved submit effect."""

    attempt_id: str
    plan_sha256: str
    audit_receipt_sha256: str
    reservation_id: str
    expires_at: float
    nonce: str
    signature: str

    def __reduce__(self) -> object:
        raise TypeError("SubmitAuthority cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("SubmitAuthority cannot be serialized")


class SubmitAuthorityIssuer:
    def __init__(
        self,
        *,
        audit_issuer: HostAuditReceiptIssuer,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._audit_issuer = audit_issuer
        self._attempt_latch = audit_issuer.attempt_latch
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._secret = secrets.token_bytes(32)
        self._issued: dict[str, float] = {}

    def issue(
        self,
        plan: ApplicationPlan,
        audit: HostAuditReceipt,
        reservation: HostReservation,
        *,
        start_claim: SubmitStartClaim,
    ) -> SubmitAuthority:
        self._audit_issuer.validate(audit, plan)
        self._validate_binding(plan, audit, reservation)
        self._attempt_latch.claim_authority(start_claim, plan)
        authority = SubmitAuthority(
            attempt_id=plan.attempt_id,
            plan_sha256=plan.digest,
            audit_receipt_sha256=audit.digest,
            reservation_id=reservation.reservation_id,
            expires_at=self._clock() + self._ttl_seconds,
            nonce=secrets.token_hex(16),
            signature="",
        )
        authority = replace(authority, signature=self._sign(authority))
        self._issued[authority.nonce] = authority.expires_at
        return authority

    @property
    def attempt_latch(self) -> HostSubmitAttemptLatch:
        return self._attempt_latch

    def consume(
        self,
        authority: SubmitAuthority,
        plan: ApplicationPlan,
        audit: HostAuditReceipt,
        reservation: HostReservation,
    ) -> None:
        if not isinstance(authority, SubmitAuthority):
            raise HostSubmitDenied("invalid SubmitAuthority type")
        if not hmac.compare_digest(authority.signature, self._sign(replace(authority, signature=""))):
            raise HostSubmitDenied("SubmitAuthority signature is invalid")
        if self._clock() >= authority.expires_at:
            raise HostSubmitDenied("SubmitAuthority expired")
        self._validate_binding(plan, audit, reservation)
        if (
            authority.attempt_id,
            authority.plan_sha256,
            authority.audit_receipt_sha256,
            authority.reservation_id,
        ) != (
            plan.attempt_id,
            plan.digest,
            audit.digest,
            reservation.reservation_id,
        ):
            raise HostSubmitDenied("SubmitAuthority binding mismatch")
        if self._issued.pop(authority.nonce, None) != authority.expires_at:
            raise HostSubmitDenied("SubmitAuthority was already consumed or was not issued here")

    @staticmethod
    def _validate_binding(
        plan: ApplicationPlan,
        audit: HostAuditReceipt,
        reservation: HostReservation,
    ) -> None:
        if plan.route != "browser_form":
            raise HostSubmitDenied("direct email requires the mailbox route")
        if (
            reservation.admitted is not True
            or reservation.route != "browser_form"
            or reservation.submit_owner != "host"
            or reservation.plan_sha256 != plan.digest
            or reservation.audit_receipt_sha256 != audit.digest
        ):
            raise HostSubmitDenied("reservation does not bind an admitted host browser submit")

    def _sign(self, authority: SubmitAuthority) -> str:
        return hmac.new(
            self._secret,
            _canonical_json(
                {
                    "attempt_id": authority.attempt_id,
                    "plan_sha256": authority.plan_sha256,
                    "audit_receipt_sha256": authority.audit_receipt_sha256,
                    "reservation_id": authority.reservation_id,
                    "expires_at": authority.expires_at,
                    "nonce": authority.nonce,
                }
            ),
            hashlib.sha256,
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class HostObservation:
    plan_sha256: str
    disposition: ObservationDisposition
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _sha256(self.plan_sha256, "plan_sha256"))
        if self.disposition not in {"confirmed", "uncertain", "blocked"}:
            raise ValueError("observation disposition is invalid")
        object.__setattr__(self, "evidence_ref", _content_ref(self.evidence_ref, "evidence_ref"))


@dataclass(frozen=True, slots=True)
class HostReconciledReceipt:
    plan_sha256: str
    observation_ref: str
    status: ReceiptStatus
    receipt_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "plan_sha256", _sha256(self.plan_sha256, "plan_sha256"))
        object.__setattr__(self, "observation_ref", _content_ref(self.observation_ref, "observation_ref"))
        if self.status not in {"admitted", "uncertain", "rejected"}:
            raise ValueError("reconciled receipt status is invalid")
        object.__setattr__(self, "receipt_ref", _content_ref(self.receipt_ref, "receipt_ref"))


@runtime_checkable
class HostSubmitHooks(Protocol):
    """Narrow host-owned effects; no production implementation is installed here."""

    def global_submit_lane(self, plan: ApplicationPlan) -> AbstractContextManager[None]: ...

    def reserve(self, plan: ApplicationPlan, audit: HostAuditReceipt) -> HostReservation: ...

    def mark_submit_started(self, reservation: HostReservation) -> None: ...

    def submit_once(self, plan: ApplicationPlan, authority: SubmitAuthority) -> None: ...

    def observe(self, plan: ApplicationPlan) -> HostObservation: ...

    def reconcile(
        self,
        plan: ApplicationPlan,
        observation: HostObservation,
    ) -> HostReconciledReceipt: ...


@dataclass(frozen=True, slots=True)
class HostSubmitResult:
    disposition: Literal["shadow", "confirmed", "uncertain"]
    reason_code: str
    plan_sha256: str
    stages: tuple[str, ...]
    submit_effect_count: int
    observation_ref: str | None = None
    receipt_ref: str | None = None


class HostSubmitExecutor:
    """Feature-gated, one-shot host submit orchestration for browser forms only."""

    def __init__(
        self,
        *,
        feature_enabled: bool = False,
        audit_issuer: HostAuditReceiptIssuer,
        authority_issuer: SubmitAuthorityIssuer | None = None,
    ) -> None:
        if not isinstance(feature_enabled, bool):
            raise TypeError("feature_enabled must be bool")
        self._feature_enabled = feature_enabled
        self._audit_issuer = audit_issuer
        if authority_issuer is not None:
            self._authority_issuer = authority_issuer
            self._attempt_latch = authority_issuer.attempt_latch
        else:
            self._attempt_latch = audit_issuer.attempt_latch
            self._authority_issuer = SubmitAuthorityIssuer(
                audit_issuer=audit_issuer,
            )

    def execute(
        self,
        *,
        plan: ApplicationPlan,
        audit: HostAuditReceipt,
        hooks: HostSubmitHooks,
    ) -> HostSubmitResult:
        if not self._feature_enabled:
            return HostSubmitResult(
                disposition="shadow",
                reason_code="HOST_SUBMIT_FEATURE_DISABLED",
                plan_sha256=plan.digest,
                stages=(),
                submit_effect_count=0,
            )
        if plan.route != "browser_form":
            raise HostSubmitDenied("direct email requires the mailbox route")
        self._attempt_latch.require_submit_allowed(plan)
        self._audit_issuer.validate(audit, plan)
        stages = ["pre_submit_audit"]
        reservation: HostReservation | None = None
        start_claim: SubmitStartClaim | None = None
        submit_started = False
        submit_effect_count = 0
        submit_path_error = False
        try:
            with hooks.global_submit_lane(plan):
                self._attempt_latch.require_submit_allowed(plan)
                stages.append("global_submit_lane")
                reservation = hooks.reserve(plan, audit)
                SubmitAuthorityIssuer._validate_binding(plan, audit, reservation)
                stages.append("reservation")
                start_claim = self._attempt_latch.begin(plan)
                submit_started = True
                hooks.mark_submit_started(reservation)
                stages.append("submit_started")
                authority = self._authority_issuer.issue(
                    plan,
                    audit,
                    reservation,
                    start_claim=start_claim,
                )
                self._authority_issuer.consume(authority, plan, audit, reservation)
                stages.append("single_use_submit_authority")
                submit_effect_count = 1
                stages.append("submit_once")
                hooks.submit_once(plan, authority)
        except Exception as exc:
            if not submit_started:
                raise HostSubmitDenied("host submit stopped before submit_started") from exc
            submit_path_error = True

        if start_claim is None:
            raise HostSubmitDenied("submit_started has no host attempt claim")
        self._attempt_latch.mark_receipt_only(start_claim, plan)

        try:
            observation = hooks.observe(plan)
            if observation.plan_sha256 != plan.digest:
                raise HostSubmitDenied("observer evidence does not bind the application plan")
            stages.append("independent_observer")
            receipt = hooks.reconcile(plan, observation)
            if receipt.plan_sha256 != plan.digest or receipt.observation_ref != observation.evidence_ref:
                raise HostSubmitDenied("reconciled receipt does not bind the observer evidence")
            stages.append("receipt_reconciliation")
        # Observer implementations are external host boundaries.  Any provider
        # exception after submit_started is deliberately collapsed to uncertain
        # so no caller can interpret it as permission to retry Submit.
        except Exception:  # noqa: BLE001
            return HostSubmitResult(
                disposition="uncertain",
                reason_code="HOST_SUBMIT_OBSERVATION_OR_RECEIPT_UNCERTAIN",
                plan_sha256=plan.digest,
                stages=tuple(stages),
                submit_effect_count=submit_effect_count,
            )

        confirmed = not submit_path_error and observation.disposition == "confirmed" and receipt.status == "admitted"
        if confirmed:
            self._attempt_latch.mark_terminal(start_claim, plan)
        return HostSubmitResult(
            disposition="confirmed" if confirmed else "uncertain",
            reason_code=("HOST_SUBMIT_RECEIPT_ADMITTED" if confirmed else "HOST_SUBMIT_EFFECT_OR_RECEIPT_UNCERTAIN"),
            plan_sha256=plan.digest,
            stages=tuple(stages),
            submit_effect_count=submit_effect_count,
            observation_ref=observation.evidence_ref,
            receipt_ref=receipt.receipt_ref,
        )

    def reconcile_only(
        self,
        *,
        plan: ApplicationPlan,
        hooks: HostSubmitHooks,
    ) -> HostSubmitResult:
        """Observe and reconcile a receipt_only attempt without submit capability."""

        if not self._feature_enabled:
            return HostSubmitResult(
                disposition="shadow",
                reason_code="HOST_SUBMIT_FEATURE_DISABLED",
                plan_sha256=plan.digest,
                stages=(),
                submit_effect_count=0,
            )
        claim = self._attempt_latch.require_reconcile_only(plan)
        stages: list[str] = []
        try:
            observation = hooks.observe(plan)
            if observation.plan_sha256 != plan.digest:
                raise HostSubmitDenied("observer evidence does not bind the application plan")
            stages.append("independent_observer")
            receipt = hooks.reconcile(plan, observation)
            if receipt.plan_sha256 != plan.digest or receipt.observation_ref != observation.evidence_ref:
                raise HostSubmitDenied("reconciled receipt does not bind the observer evidence")
            stages.append("receipt_reconciliation")
        except Exception:  # noqa: BLE001
            return HostSubmitResult(
                disposition="uncertain",
                reason_code="HOST_SUBMIT_OBSERVATION_OR_RECEIPT_UNCERTAIN",
                plan_sha256=plan.digest,
                stages=tuple(stages),
                submit_effect_count=0,
            )
        confirmed = observation.disposition == "confirmed" and receipt.status == "admitted"
        if confirmed:
            self._attempt_latch.mark_terminal(claim, plan)
        return HostSubmitResult(
            disposition="confirmed" if confirmed else "uncertain",
            reason_code=("HOST_SUBMIT_RECEIPT_ADMITTED" if confirmed else "HOST_SUBMIT_EFFECT_OR_RECEIPT_UNCERTAIN"),
            plan_sha256=plan.digest,
            stages=tuple(stages),
            submit_effect_count=0,
            observation_ref=observation.evidence_ref,
            receipt_ref=receipt.receipt_ref,
        )
