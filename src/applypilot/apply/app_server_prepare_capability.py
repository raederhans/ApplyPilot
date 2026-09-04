"""Separate App Server capability contract for controlled prepare-only writes.

This module does not widen the existing read-only App Server shadow.  A host
may pre-bind one ``SemanticBatchRuntimeRequest`` and expose only the opaque
grant claims to a distinct prepare turn.  Patch values, browser handles,
credentials, mailbox state, navigation, Submit and receipt authority never
cross the tool boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace

from applypilot.apply.semantic_batch import SemanticBatchDenied
from applypilot.apply.semantic_batch_runtime import (
    SemanticBatchRuntimeRequest,
    SemanticBatchRuntimeResult,
)

PREPARE_WRITE_PROMPT_MARKER = "APPLYPILOT_PREPARE_SEMANTIC_WRITE_V1"
PREPARE_WRITE_TOOL_NAME = "applypilot_prepare_semantic_batch"


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


@dataclass(frozen=True, slots=True)
class PrepareOnlyExecutionContext:
    """Host facts that must all remain non-submission prepare state."""

    attempt_id: str
    actor_id: str
    phase: str
    route: str
    page_id: str
    page_lease_id: str
    page_lease_epoch: int
    page_epoch: int
    navigation_authorized: bool = False
    credential_authorized: bool = False
    mailbox_authorized: bool = False
    final_submit_authorized: bool = False
    reservation_claimed: bool = False
    receipt_authorized: bool = False

    def __post_init__(self) -> None:
        for name in ("attempt_id", "actor_id", "phase", "route", "page_id", "page_lease_id"):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if (
            isinstance(self.page_lease_epoch, bool)
            or self.page_lease_epoch < 1
            or isinstance(self.page_epoch, bool)
            or self.page_epoch < 0
        ):
            raise ValueError("prepare-only page epochs are invalid")
        for name in (
            "navigation_authorized",
            "credential_authorized",
            "mailbox_authorized",
            "final_submit_authorized",
            "reservation_claimed",
            "receipt_authorized",
        ):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class PrepareSemanticBatchGrant:
    """Opaque, one-shot JSON grant; it contains no patch value or browser handle."""

    grant_id: str
    attempt_id: str
    actor_id: str
    batch_id: str
    page_id: str
    page_lease_id: str
    page_lease_epoch: int
    page_epoch: int
    expires_at: float
    signature: str
    submit_authority: bool = False

    def public_claims(self) -> dict[str, object]:
        return {
            "grant_id": self.grant_id,
            "attempt_id": self.attempt_id,
            "actor_id": self.actor_id,
            "batch_id": self.batch_id,
            "page_id": self.page_id,
            "page_lease_id": self.page_lease_id,
            "page_lease_epoch": self.page_lease_epoch,
            "page_epoch": self.page_epoch,
            "expires_at": self.expires_at,
            "signature": self.signature,
            "submit_authority": False,
        }

    def __reduce__(self) -> object:
        raise TypeError("PrepareSemanticBatchGrant cannot be serialized as a Python capability")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("PrepareSemanticBatchGrant cannot be serialized as a Python capability")


BatchExecutor = Callable[[SemanticBatchRuntimeRequest], SemanticBatchRuntimeResult]


@dataclass(slots=True)
class _GrantRecord:
    grant: PrepareSemanticBatchGrant
    request: SemanticBatchRuntimeRequest
    consumed: bool = False


class PrepareOnlyPolicyEngine:
    """Host-local admission and one-shot dispatch around SemanticPatchBatch."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._secret = secrets.token_bytes(32)
        self._lock = threading.RLock()
        self._records: dict[str, _GrantRecord] = {}
        self._batch_grants: dict[str, str] = {}

    def issue(
        self,
        context: PrepareOnlyExecutionContext,
        request: SemanticBatchRuntimeRequest,
    ) -> PrepareSemanticBatchGrant:
        """Admit a host-built canary request without exposing its patch values."""

        self._validate_context(context, request)
        with self._lock:
            if request.batch_id in self._batch_grants:
                raise SemanticBatchDenied("semantic batch already has a prepare grant")
            grant_id = "prepare-grant:" + secrets.token_hex(16)
            unsigned = PrepareSemanticBatchGrant(
                grant_id=grant_id,
                attempt_id=request.attempt_id,
                actor_id=request.actor_id,
                batch_id=request.batch_id,
                page_id=request.page_id,
                page_lease_id=request.page_lease_id,
                page_lease_epoch=request.page_lease_epoch,
                page_epoch=request.page_binding.page_epoch,
                expires_at=self._clock() + self._ttl_seconds,
                signature="pending",
            )
            grant = replace(unsigned, signature=self._sign(unsigned))
            self._records[grant_id] = _GrantRecord(grant=grant, request=request)
            self._batch_grants[request.batch_id] = grant_id
        return grant

    def execute(
        self,
        public_claims: Mapping[str, object],
        *,
        executor: BatchExecutor,
    ) -> SemanticBatchRuntimeResult:
        """Consume exactly one host-held request and require its postcondition result."""

        if set(public_claims) != set(PrepareSemanticBatchGrant.__dataclass_fields__):
            raise SemanticBatchDenied("prepare grant schema is invalid")
        grant_id = _required(public_claims.get("grant_id"), "grant_id")
        with self._lock:
            record = self._records.get(grant_id)
            if record is None or record.consumed:
                raise SemanticBatchDenied("prepare grant is missing or already consumed")
            if public_claims != record.grant.public_claims():
                raise SemanticBatchDenied("prepare grant claims were modified")
            unsigned = replace(record.grant, signature="pending")
            if not hmac.compare_digest(record.grant.signature, self._sign(unsigned)):
                raise SemanticBatchDenied("prepare grant signature is invalid")
            if self._clock() >= record.grant.expires_at:
                raise SemanticBatchDenied("prepare grant expired")
            # Consume before dispatch.  Any exception or uncertain effect is
            # reconcile/park-only and can never authorize a second write.
            record.consumed = True
            request = record.request

        result = executor(request)
        if not isinstance(result, SemanticBatchRuntimeResult):
            raise TypeError("prepare executor must return SemanticBatchRuntimeResult")
        if (
            result.submit_authority is not False
            or result.mode != "canary"
            or result.status
            not in {"verified", "replayed", "fallback", "parked", "not_applicable"}
            or result.batch_id != request.batch_id
            or result.candidate_count != len(request.patches)
            or result.effect_count < 0
            or result.effect_count > result.candidate_count
        ):
            raise SemanticBatchDenied("prepare executor result violated the semantic batch contract")
        return result

    @staticmethod
    def _validate_context(
        context: PrepareOnlyExecutionContext,
        request: SemanticBatchRuntimeRequest,
    ) -> None:
        if request.mode != "canary":
            raise SemanticBatchDenied("prepare authority requires a canary SemanticPatchBatch")
        if context.phase != "prepare" or context.route != "browser":
            raise SemanticBatchDenied("prepare authority is limited to the browser prepare phase")
        forbidden = (
            context.navigation_authorized
            or context.credential_authorized
            or context.mailbox_authorized
            or context.final_submit_authorized
            or context.reservation_claimed
            or context.receipt_authorized
        )
        if forbidden:
            raise SemanticBatchDenied("prepare authority cannot coexist with a forbidden capability")
        if (
            context.attempt_id,
            context.actor_id,
            context.page_id,
            context.page_lease_id,
            context.page_lease_epoch,
            context.page_epoch,
        ) != (
            request.attempt_id,
            request.actor_id,
            request.page_id,
            request.page_lease_id,
            request.page_lease_epoch,
            request.page_binding.page_epoch,
        ):
            raise SemanticBatchDenied("prepare authority does not bind the current page lease")

    def _sign(self, grant: PrepareSemanticBatchGrant) -> str:
        claims = grant.public_claims()
        claims["signature"] = "pending"
        return hmac.new(self._secret, _canonical_json(claims), hashlib.sha256).hexdigest()


def build_prepare_write_prompt(grant: PrepareSemanticBatchGrant) -> str:
    """Build a distinct authoritative prepare prompt; never reuse shadow text."""

    payload = {
        "schema": "ApplyPilotPrepareSemanticBatch/v1",
        "tool": PREPARE_WRITE_TOOL_NAME,
        "grant": grant.public_claims(),
        "allowed_effect": "one_host_bound_semantic_patch_batch",
        "forbidden": [
            "navigation",
            "credentials",
            "mailbox",
            "final_submit",
            "reservation",
            "receipt",
        ],
    }
    return "\n".join(
        (
            PREPARE_WRITE_PROMPT_MARKER,
            "This is a separate prepare-only capability, not the read-only shadow turn.",
            "Call the named tool at most once with the exact grant object. Do not add fields.",
            "Patch values and browser handles remain host-owned. The tool result is authoritative only when its SemanticPatchBatch postconditions are verified.",
            "No navigation, credential, mailbox, final Submit, reservation, receipt, or outcome authority exists.",
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        )
    )


def prepare_write_tool_contract() -> dict[str, object]:
    """Return the future App Server descriptor for a separate write surface."""

    properties = {
        "grant_id": {"type": "string"},
        "attempt_id": {"type": "string"},
        "actor_id": {"type": "string"},
        "batch_id": {"type": "string"},
        "page_id": {"type": "string"},
        "page_lease_id": {"type": "string"},
        "page_lease_epoch": {"type": "integer", "minimum": 1},
        "page_epoch": {"type": "integer", "minimum": 0},
        "expires_at": {"type": "number"},
        "signature": {"type": "string"},
        "submit_authority": {"const": False},
    }
    return {
        "name": PREPARE_WRITE_TOOL_NAME,
        "description": "Execute one pre-bound routine SemanticPatchBatch during prepare only.",
        "effect_class": "browser_write",
        "authority": "prepare_semantic_batch",
        "phase": "prepare",
        "route": "browser",
        "concurrency_mode": "serial_per_page",
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties),
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "required": [
                "status",
                "mode",
                "batch_id",
                "candidate_count",
                "effect_count",
                "legacy_fallback_safe",
                "reason_code",
                "submit_authority",
            ],
            "properties": {
                "status": {"type": "string"},
                "mode": {"const": "canary"},
                "batch_id": {"type": ["string", "null"]},
                "candidate_count": {"type": "integer", "minimum": 0},
                "effect_count": {"type": "integer", "minimum": 0},
                "legacy_fallback_safe": {"type": "boolean"},
                "reason_code": {"type": "string"},
                "submit_authority": {"const": False},
            },
            "additionalProperties": False,
        },
    }


__all__ = [
    "PREPARE_WRITE_PROMPT_MARKER",
    "PREPARE_WRITE_TOOL_NAME",
    "PrepareOnlyExecutionContext",
    "PrepareOnlyPolicyEngine",
    "PrepareSemanticBatchGrant",
    "build_prepare_write_prompt",
    "prepare_write_tool_contract",
]
