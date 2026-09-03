"""Parent-owned, narrowly bounded semantic browser operations.

The broker remains observation-only.  A private in-process capability permits
only an exact, pre-submit resume upload on the currently bound page.
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
from pathlib import Path
from typing import Literal, Protocol

from applypilot.apply.browser_broker import BrowserAuthorityDenied, BrowserLeaseBundle
from applypilot.apply.page_binding import PageBinding

_ALLOWED_PROVIDERS = frozenset({"workday", "smartrecruiters"})
SEMANTIC_WRITE_POLICY = "semantic-browser-write/v1"
SEMANTIC_WRITE_POLICY_DIGEST = hashlib.sha256(
    SEMANTIC_WRITE_POLICY.encode("ascii")
).hexdigest()


class SemanticWriteDenied(BrowserAuthorityDenied):
    """The exact semantic write authority is absent, invalid, or stale."""


class SemanticWriteUncertain(RuntimeError):
    """A write/postcondition occurred but its epoch CAS is not proven."""


class PageValidationBroker(Protocol):
    """Structural broker surface; it grants no write capability."""

    def validate_page(self, binding: PageBinding) -> PageBinding: ...

    def advance_page(
        self, bundle: BrowserLeaseBundle, *, expected_page_epoch: int
    ) -> BrowserLeaseBundle: ...

    def require_operation(self, binding: PageBinding, operation: str) -> PageBinding: ...


class SemanticWriteLifecycle(Protocol):
    """Narrow journal callbacks; the implementation lives outside this module."""

    def require_claimed(self, operation_digest: str) -> None: ...

    def mark_effect_observed(self, operation_digest: str) -> None: ...

    def mark_verified(self, operation_digest: str, result_epoch: int) -> None: ...

    def park_unknown(self, operation_digest: str, reason: str) -> None: ...

    def park_stale_after_effect(
        self, operation_digest: str, expected_epoch: int
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class BoundResumeArtifact:
    """A parent-resolved local resume and independently computed binding."""

    path: Path
    sha256: str
    size_bytes: int
    filename: str

    def __post_init__(self) -> None:
        path = Path(self.path)
        object.__setattr__(self, "path", path)
        if not path.is_absolute():
            raise ValueError("resume artifact path must be absolute")
        if not _is_sha256(self.sha256):
            raise ValueError("resume artifact sha256 must be 64 lowercase hex characters")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("resume artifact size_bytes must be non-negative")
        if not self.filename or Path(self.filename).name != self.filename:
            raise ValueError("resume artifact filename must be a basename")
        if path.name != self.filename:
            raise ValueError("resume artifact filename must match its resolved path")


@dataclass(frozen=True, slots=True)
class ResumeUploadPostcondition:
    """Exact browser-visible proof required after upload."""

    filename: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not self.filename or Path(self.filename).name != self.filename:
            raise ValueError("postcondition filename must be a basename")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("postcondition size_bytes must be non-negative")


@dataclass(frozen=True, slots=True)
class ResumeUploadRequest:
    """Exact request passed only to a parent-owned provider driver."""

    actor_id: str
    attempt_id: str
    provider: Literal["workday", "smartrecruiters"]
    container_key: str
    artifact: BoundResumeArtifact
    application_binding_hash: str
    material_binding_hash: str
    policy_digest: str
    adapter_version: str
    expected_postcondition: ResumeUploadPostcondition

    def __post_init__(self) -> None:
        for value, name in (
            (self.actor_id, "actor_id"),
            (self.attempt_id, "attempt_id"),
            (self.container_key, "container_key"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        if self.provider not in _ALLOWED_PROVIDERS:
            raise ValueError("provider is not eligible for semantic resume upload")
        for value, name in (
            (self.application_binding_hash, "application_binding_hash"),
            (self.material_binding_hash, "material_binding_hash"),
            (self.policy_digest, "policy_digest"),
        ):
            if not _is_sha256(value):
                raise ValueError(f"{name} must be 64 lowercase hex characters")
        if self.policy_digest != SEMANTIC_WRITE_POLICY_DIGEST:
            raise ValueError("policy_digest does not identify the required policy")
        if (
            not isinstance(self.adapter_version, str)
            or not self.adapter_version.strip()
            or len(self.adapter_version) > 128
            or any(character.isspace() for character in self.adapter_version)
        ):
            raise ValueError("adapter_version must be a bounded non-whitespace value")
        expected = self.expected_postcondition
        if (
            expected.filename != self.artifact.filename
            or expected.size_bytes != self.artifact.size_bytes
        ):
            raise ValueError("expected_postcondition must match the bound artifact")


@dataclass(frozen=True, slots=True)
class ResumeUploadObservation:
    container_key: str
    filename: str | None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ResumeUploadResult:
    bundle: BrowserLeaseBundle
    provider: str
    container_key: str
    filename: str
    size_bytes: int
    uploaded: bool
    replayed: bool


class ResumeUploadDriver(Protocol):
    def observe_resume(self, request: ResumeUploadRequest) -> ResumeUploadObservation: ...

    def upload_resume(self, request: ResumeUploadRequest) -> None: ...


@dataclass(frozen=True, slots=True)
class SemanticWriteAuthority:
    """Opaque, non-serializable proof for one exact upload operation."""

    actor_id: str
    attempt_id: str
    page_binding: PageBinding
    provider: str
    operation: str
    artifact_sha256: str
    artifact_size_bytes: int
    artifact_filename: str
    container_key: str
    application_binding_hash: str
    material_binding_hash: str
    policy_digest: str
    adapter_version: str
    expected_postcondition: ResumeUploadPostcondition
    policy_version: str
    operation_digest: str
    expires_at: float
    nonce: str
    submit_authority: bool
    signature: str

    def __reduce__(self) -> object:
        raise TypeError("semantic write authority cannot be serialized")

    def __reduce_ex__(self, _protocol: int) -> object:
        raise TypeError("semantic write authority cannot be serialized")


class SemanticWriteAuthorityIssuer:
    """Private HMAC issuer/verifier that must remain in the parent process."""

    def __init__(
        self,
        *,
        policy_version: str = SEMANTIC_WRITE_POLICY,
        ttl_seconds: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if policy_version != SEMANTIC_WRITE_POLICY:
            raise ValueError("policy_version must use the required policy contract")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._policy_version = policy_version
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._secret = secrets.token_bytes(32)
        self._nonce_lock = threading.Lock()
        self._issued_nonces: dict[str, float] = {}

    @property
    def policy_version(self) -> str:
        return self._policy_version

    def issue(
        self,
        *,
        bundle: BrowserLeaseBundle,
        request: ResumeUploadRequest,
        submit_started: bool,
    ) -> SemanticWriteAuthority:
        if submit_started:
            raise SemanticWriteDenied("resume upload authority is pre-submit only")
        _validate_bundle_request(bundle, request)
        _validate_artifact_file(request.artifact)
        digest = _operation_digest(bundle, request, self._policy_version)
        nonce = secrets.token_hex(16)
        unsigned = SemanticWriteAuthority(
            actor_id=request.actor_id,
            attempt_id=request.attempt_id,
            page_binding=bundle.page_binding,
            provider=request.provider,
            operation="upload_bound_artifact",
            artifact_sha256=request.artifact.sha256,
            artifact_size_bytes=request.artifact.size_bytes,
            artifact_filename=request.artifact.filename,
            container_key=request.container_key,
            application_binding_hash=request.application_binding_hash,
            material_binding_hash=request.material_binding_hash,
            policy_digest=request.policy_digest,
            adapter_version=request.adapter_version,
            expected_postcondition=request.expected_postcondition,
            policy_version=self._policy_version,
            operation_digest=digest,
            expires_at=self._clock() + self._ttl_seconds,
            nonce=nonce,
            submit_authority=False,
            signature="",
        )
        authority = replace(unsigned, signature=self._sign(unsigned))
        with self._nonce_lock:
            now = self._clock()
            self._issued_nonces = {
                key: expiry
                for key, expiry in self._issued_nonces.items()
                if expiry > now
            }
            self._issued_nonces[nonce] = authority.expires_at
        return authority

    def _verify_and_consume(
        self,
        authority: SemanticWriteAuthority,
        *,
        bundle: BrowserLeaseBundle,
        request: ResumeUploadRequest,
    ) -> None:
        if not isinstance(authority, SemanticWriteAuthority):
            raise SemanticWriteDenied("semantic write authority has the wrong type")
        expected_signature = self._sign(replace(authority, signature=""))
        if not hmac.compare_digest(authority.signature, expected_signature):
            raise SemanticWriteDenied("semantic write authority signature is invalid")
        if self._clock() >= authority.expires_at:
            raise SemanticWriteDenied("semantic write authority expired")
        _validate_bundle_request(bundle, request)
        _validate_artifact_file(request.artifact)
        expected = (
            request.actor_id,
            request.attempt_id,
            bundle.page_binding,
            request.provider,
            "upload_bound_artifact",
            request.artifact.sha256,
            request.artifact.size_bytes,
            request.artifact.filename,
            request.container_key,
            request.application_binding_hash,
            request.material_binding_hash,
            request.policy_digest,
            request.adapter_version,
            request.expected_postcondition,
            self._policy_version,
            _operation_digest(bundle, request, self._policy_version),
            False,
        )
        actual = (
            authority.actor_id,
            authority.attempt_id,
            authority.page_binding,
            authority.provider,
            authority.operation,
            authority.artifact_sha256,
            authority.artifact_size_bytes,
            authority.artifact_filename,
            authority.container_key,
            authority.application_binding_hash,
            authority.material_binding_hash,
            authority.policy_digest,
            authority.adapter_version,
            authority.expected_postcondition,
            authority.policy_version,
            authority.operation_digest,
            authority.submit_authority,
        )
        if actual != expected or not _is_sha256(authority.operation_digest):
            raise SemanticWriteDenied("semantic write authority binding mismatch")
        with self._nonce_lock:
            expiry = self._issued_nonces.pop(authority.nonce, None)
        if expiry is None or expiry != authority.expires_at:
            raise SemanticWriteDenied(
                "semantic write authority was already consumed or was not issued here"
            )

    def _sign(self, authority: SemanticWriteAuthority) -> str:
        return hmac.new(
            self._secret, _authority_payload(authority), hashlib.sha256
        ).hexdigest()


class SemanticBrowserOps:
    """Parent-owned, exact pre-submit resume and routine-control operations."""

    def __init__(
        self,
        broker: PageValidationBroker,
        observe_form: Callable[[], Mapping[str, object]] | None = None,
        *,
        authority_issuer: SemanticWriteAuthorityIssuer | None = None,
        resume_driver: ResumeUploadDriver | None = None,
        lifecycle: SemanticWriteLifecycle | None = None,
    ) -> None:
        self._broker = broker
        self._observe_form = observe_form
        self._authority_issuer = authority_issuer
        self._resume_driver = resume_driver
        self._lifecycle = lifecycle

    def observe_form(self, bundle: BrowserLeaseBundle) -> dict[str, object]:
        if self._observe_form is None:
            raise BrowserAuthorityDenied("observe_form callback is not configured")
        self._broker.require_operation(bundle.page_binding, "observe_form")
        observation = dict(self._observe_form())
        self._broker.require_operation(bundle.page_binding, "observe_form")
        return observation

    def upload_bound_resume(
        self,
        bundle: BrowserLeaseBundle,
        authority: SemanticWriteAuthority,
        request: ResumeUploadRequest,
    ) -> ResumeUploadResult:
        issuer = self._authority_issuer
        driver = self._resume_driver
        lifecycle = self._lifecycle
        if issuer is None or driver is None or lifecycle is None:
            raise SemanticWriteDenied("semantic resume upload is not configured")
        issuer._verify_and_consume(authority, bundle=bundle, request=request)
        self._broker.validate_page(bundle.page_binding)
        try:
            lifecycle.require_claimed(authority.operation_digest)
        except Exception as exc:
            raise SemanticWriteDenied(
                "semantic write journal operation is not externally claimed"
            ) from exc
        before = driver.observe_resume(request)
        if before.container_key != request.container_key:
            raise SemanticWriteDenied(
                "resume driver did not resolve the exact labelled upload container"
            )
        replayed = _postcondition_matches(before, request.artifact)
        if not replayed:
            try:
                # Once invoked, even an adapter exception may follow a partial write.
                driver.upload_resume(request)
                after = driver.observe_resume(request)
                _validate_observation_container(after, request)
                if not _postcondition_matches(after, request.artifact):
                    raise RuntimeError("postcondition_mismatch")
            except Exception as exc:
                _park_unknown(
                    lifecycle,
                    authority.operation_digest,
                    type(exc).__name__,
                )
                raise SemanticWriteUncertain(
                    "resume upload occurred but its postcondition is uncertain"
                ) from exc
        try:
            lifecycle.mark_effect_observed(authority.operation_digest)
        except Exception as exc:
            raise SemanticWriteUncertain(
                "resume effect is proven but journal effect recording failed"
            ) from exc
        try:
            updated_bundle = self._broker.advance_page(
                bundle, expected_page_epoch=bundle.page_binding.page_epoch
            )
        except Exception as exc:
            try:
                lifecycle.park_stale_after_effect(
                    authority.operation_digest, bundle.page_binding.page_epoch
                )
            except Exception as lifecycle_exc:
                raise SemanticWriteUncertain(
                    "page epoch CAS and stale-effect journal parking both failed"
                ) from lifecycle_exc
            raise SemanticWriteUncertain(
                "resume postcondition is proven but page epoch CAS failed"
            ) from exc
        try:
            lifecycle.mark_verified(
                authority.operation_digest, updated_bundle.page_binding.page_epoch
            )
        except Exception as exc:
            raise SemanticWriteUncertain(
                "page epoch advanced but verified journal recording failed"
            ) from exc
        return ResumeUploadResult(
            bundle=updated_bundle,
            provider=request.provider,
            container_key=request.container_key,
            filename=request.artifact.filename,
            size_bytes=request.artifact.size_bytes,
            uploaded=not replayed,
            replayed=replayed,
        )


    def apply_form_patch(self, *_args: object, **_kwargs: object) -> None:
        raise BrowserAuthorityDenied(
            "semantic browser ops do not hold arbitrary page_write authority"
        )

    def upload_artifact(self, *_args: object, **_kwargs: object) -> None:
        raise BrowserAuthorityDenied(
            "generic page_write artifact upload is forbidden; "
            "use an exact bound resume capability"
        )


def _validate_bundle_request(
    bundle: BrowserLeaseBundle, request: ResumeUploadRequest
) -> None:
    if (
        bundle.profile.owner_id,
        bundle.page.owner_id,
        bundle.page_binding.owner_id,
    ) != (request.actor_id,) * 3:
        raise SemanticWriteDenied("actor does not own the exact browser lease bundle")
    if (
        bundle.profile.attempt_id,
        bundle.page.attempt_id,
        bundle.page_binding.attempt_id,
    ) != (request.attempt_id,) * 3:
        raise SemanticWriteDenied("attempt does not own the exact browser lease bundle")
    if request.provider not in _ALLOWED_PROVIDERS:
        raise SemanticWriteDenied("provider is not eligible for semantic resume upload")


def _validate_artifact_file(artifact: BoundResumeArtifact) -> None:
    try:
        stat = artifact.path.stat()
    except OSError as exc:
        raise SemanticWriteDenied("bound resume artifact is unavailable") from exc
    if not artifact.path.is_file() or stat.st_size != artifact.size_bytes:
        raise SemanticWriteDenied("bound resume artifact size changed")
    digest = hashlib.sha256(artifact.path.read_bytes()).hexdigest()
    if not hmac.compare_digest(digest, artifact.sha256):
        raise SemanticWriteDenied("bound resume artifact content changed")


def _operation_digest(
    bundle: BrowserLeaseBundle,
    request: ResumeUploadRequest,
    policy_version: str,
) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "actor_id": request.actor_id,
                "adapter_version": request.adapter_version,
                "application_binding_hash": request.application_binding_hash,
                "artifact_sha256": request.artifact.sha256,
                "artifact_size_bytes": request.artifact.size_bytes,
                "container_binding_hash": hashlib.sha256(
                    request.container_key.encode("utf-8")
                ).hexdigest(),
                "expected_postcondition_digest": resume_postcondition_digest(
                    request.expected_postcondition
                ),
                "material_binding_hash": request.material_binding_hash,
                "operation": "upload_bound_artifact",
                "attempt_id": request.attempt_id,
                "page_binding": _binding_payload(bundle.page_binding),
                "policy_contract": policy_version,
                "policy_digest": request.policy_digest,
                "provider": request.provider,
                "submit_authority": False,
            }
        )
    ).hexdigest()


def _authority_payload(authority: SemanticWriteAuthority) -> bytes:
    return _canonical_json(
        {
            "actor_id": authority.actor_id,
            "artifact_filename": authority.artifact_filename,
            "artifact_sha256": authority.artifact_sha256,
            "artifact_size_bytes": authority.artifact_size_bytes,
            "attempt_id": authority.attempt_id,
            "adapter_version": authority.adapter_version,
            "application_binding_hash": authority.application_binding_hash,
            "container_key": authority.container_key,
            "expires_at": authority.expires_at,
            "nonce": authority.nonce,
            "expected_postcondition": {
                "filename": authority.expected_postcondition.filename,
                "size_bytes": authority.expected_postcondition.size_bytes,
            },
            "material_binding_hash": authority.material_binding_hash,
            "operation": authority.operation,
            "operation_digest": authority.operation_digest,
            "page_binding": _binding_payload(authority.page_binding),
            "policy_version": authority.policy_version,
            "policy_digest": authority.policy_digest,
            "provider": authority.provider,
            "submit_authority": authority.submit_authority,
        }
    )


def _binding_payload(binding: PageBinding) -> dict[str, object]:
    return {
        "attempt_id": binding.attempt_id,
        "owner_id": binding.owner_id,
        "page_epoch": binding.page_epoch,
        "page_id": binding.page_id,
        "page_lease_epoch": binding.page_lease_epoch,
        "page_lease_id": binding.page_lease_id,
        "profile_lease_id": binding.profile_lease_id,
        "runtime_id": binding.runtime_id,
        "schema_version": binding.schema_version,
    }


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def resume_postcondition_digest(postcondition: ResumeUploadPostcondition) -> str:
    """Return the canonical, PII-free digest used by journal claims."""

    return hashlib.sha256(
        _canonical_json(
            {
                "filename_hash": hashlib.sha256(
                    postcondition.filename.encode("utf-8")
                ).hexdigest(),
                "size_bytes": postcondition.size_bytes,
            }
        )
    ).hexdigest()


def _park_unknown(
    lifecycle: SemanticWriteLifecycle, operation_digest: str, reason: str
) -> None:
    try:
        lifecycle.park_unknown(operation_digest, reason)
    except Exception as exc:
        raise SemanticWriteUncertain(
            "write outcome and unknown-effect journal parking both failed"
        ) from exc


def _is_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_observation_container(
    observation: ResumeUploadObservation, request: ResumeUploadRequest
) -> None:
    if observation.container_key != request.container_key:
        raise SemanticWriteUncertain(
            "resume driver observed a different labelled upload container"
        )


def _postcondition_matches(
    observation: ResumeUploadObservation, artifact: BoundResumeArtifact
) -> bool:
    if observation.filename != artifact.filename:
        return False
    return observation.size_bytes is None or observation.size_bytes == artifact.size_bytes
