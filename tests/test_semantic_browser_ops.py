from __future__ import annotations

import hashlib
import json
import pickle
from dataclasses import fields, replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import applypilot.apply.semantic_browser_ops as semantic_ops
from applypilot.apply.browser_broker import (
    BrowserAuthorityDenied,
    BrowserBroker,
    BrowserLeaseBundle,
)
from applypilot.apply.semantic_browser_ops import (
    SEMANTIC_WRITE_POLICY_DIGEST,
    BoundResumeArtifact,
    ResumeUploadObservation,
    ResumeUploadPostcondition,
    ResumeUploadRequest,
    SemanticBrowserOps,
    SemanticWriteAuthority,
    SemanticWriteAuthorityIssuer,
    SemanticWriteDenied,
    SemanticWriteUncertain,
    resume_postcondition_digest,
)


class FakeDriver:
    def __init__(
        self,
        observation: ResumeUploadObservation,
        *,
        after_upload: ResumeUploadObservation | None = None,
        upload_hook: object | None = None,
    ) -> None:
        self.observation = observation
        self.after_upload = after_upload
        self.upload_hook = upload_hook
        self.observe_calls = 0
        self.upload_calls = 0

    def observe_resume(self, request: ResumeUploadRequest) -> ResumeUploadObservation:
        self.observe_calls += 1
        return self.observation

    def upload_resume(self, request: ResumeUploadRequest) -> None:
        self.upload_calls += 1
        if callable(self.upload_hook):
            self.upload_hook()
        if self.after_upload is not None:
            self.observation = self.after_upload


class RaisingUploadDriver(FakeDriver):
    def upload_resume(self, request: ResumeUploadRequest) -> None:
        self.upload_calls += 1
        raise RuntimeError("driver lost acknowledgement")


class FakeLifecycle:
    def __init__(self, events: list[object] | None = None) -> None:
        self.events = events if events is not None else []

    def require_claimed(self, operation_digest: str) -> None:
        self.events.append("claimed")

    def mark_effect_observed(self, operation_digest: str) -> None:
        self.events.append("effect")

    def mark_verified(self, operation_digest: str, result_epoch: int) -> None:
        self.events.append(("verified", result_epoch))

    def park_unknown(self, operation_digest: str, reason: str) -> None:
        self.events.append(("unknown", reason))

    def park_stale_after_effect(
        self, operation_digest: str, expected_epoch: int
    ) -> None:
        self.events.append(("stale_after_effect", expected_epoch))


def _ops(
    broker: BrowserBroker,
    issuer: SemanticWriteAuthorityIssuer,
    driver: FakeDriver,
    lifecycle: FakeLifecycle | None = None,
) -> SemanticBrowserOps:
    return SemanticBrowserOps(
        broker,
        authority_issuer=issuer,
        resume_driver=driver,
        lifecycle=lifecycle or FakeLifecycle(),
    )


@pytest.fixture
def setup(tmp_path: Path):
    now = [100.0]
    artifact_path = tmp_path / "resume.pdf"
    artifact_path.write_bytes(b"bounded-resume")
    artifact = BoundResumeArtifact(
        path=artifact_path.resolve(),
        sha256=hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
        size_bytes=artifact_path.stat().st_size,
        filename=artifact_path.name,
    )
    broker = BrowserBroker(clock=lambda: now[0])
    bundle = broker.acquire_bundle(
        profile_id="profile-1",
        page_id="page-1",
        owner_id="actor-1",
        scope_id="scope-1",
        attempt_id="attempt-1",
        runtime_id="runtime-1",
    )
    request = ResumeUploadRequest(
        actor_id="actor-1",
        attempt_id="attempt-1",
        provider="workday",
        container_key="workday:resume",
        artifact=artifact,
        application_binding_hash="a" * 64,
        material_binding_hash="b" * 64,
        policy_digest=SEMANTIC_WRITE_POLICY_DIGEST,
        adapter_version="workday/v1",
        expected_postcondition=ResumeUploadPostcondition(
            filename=artifact.filename,
            size_bytes=artifact.size_bytes,
        ),
    )
    issuer = SemanticWriteAuthorityIssuer(clock=lambda: now[0], ttl_seconds=10)
    authority = issuer.issue(
        bundle=bundle, request=request, submit_started=False
    )
    return now, broker, bundle, request, issuer, authority


def test_valid_write_verifies_postcondition_and_advances_epoch(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    events: list[object] = []
    original_advance = broker.advance_page

    def recording_advance(bundle, *, expected_page_epoch):
        events.append("cas")
        return original_advance(bundle, expected_page_epoch=expected_page_epoch)

    broker.advance_page = recording_advance  # type: ignore[method-assign]
    lifecycle = FakeLifecycle(events)
    driver = FakeDriver(
        ResumeUploadObservation(request.container_key, None),
        after_upload=ResumeUploadObservation(
            request.container_key,
            request.artifact.filename,
            request.artifact.size_bytes,
        ),
    )

    result = _ops(broker, issuer, driver, lifecycle).upload_bound_resume(
        bundle, authority, request
    )

    assert (driver.observe_calls, driver.upload_calls) == (2, 1)
    assert result.uploaded is True
    assert result.replayed is False
    assert result.bundle.page_binding.page_epoch == bundle.page_binding.page_epoch + 1
    assert events == ["claimed", "effect", "cas", ("verified", 1)]


def test_idempotent_replay_is_zero_write_and_still_advances_epoch(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    driver = FakeDriver(
        ResumeUploadObservation(
            request.container_key,
            request.artifact.filename,
            None,
        )
    )

    result = _ops(broker, issuer, driver).upload_bound_resume(
        bundle, authority, request
    )

    assert (driver.observe_calls, driver.upload_calls) == (1, 0)
    assert result.replayed is True
    assert result.bundle.page_binding.page_epoch == 1


def test_authority_is_single_use(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    exact = ResumeUploadObservation(
        request.container_key, request.artifact.filename, request.artifact.size_bytes
    )
    driver = FakeDriver(exact)
    _ops(broker, issuer, driver).upload_bound_resume(
        bundle, authority, request
    )

    with pytest.raises(SemanticWriteDenied, match="consumed"):
        _ops(broker, issuer, driver).upload_bound_resume(bundle, authority, request)


def test_new_token_can_recover_same_operation_digest_after_unknown(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    driver = FakeDriver(
        ResumeUploadObservation(request.container_key, None),
        after_upload=ResumeUploadObservation(request.container_key, "wrong.pdf"),
    )
    with pytest.raises(SemanticWriteUncertain):
        _ops(broker, issuer, driver).upload_bound_resume(bundle, authority, request)

    recovery = issuer.issue(bundle=bundle, request=request, submit_started=False)
    assert recovery.operation_digest == authority.operation_digest
    driver.observation = ResumeUploadObservation(
        request.container_key, request.artifact.filename, request.artifact.size_bytes
    )
    recovered = _ops(broker, issuer, driver).upload_bound_resume(
        bundle, recovery, request
    )
    assert recovered.replayed is True
    assert driver.upload_calls == 1


def test_operation_digest_commits_actor_attempt_and_every_page_binding_field(
    setup,
) -> None:
    _, _, bundle, request, issuer, authority = setup
    changed_requests = [
        replace(request, actor_id="actor-x"),
        replace(request, attempt_id="attempt-x"),
    ]
    for changed in changed_requests:
        assert (
            semantic_ops._operation_digest(
                bundle, changed, issuer.policy_version
            )
            != authority.operation_digest
        )
    for field_name, value in (
        ("page_id", "page-x"),
        ("page_lease_id", "page-lease-x"),
        ("page_lease_epoch", 999),
        ("page_epoch", 999),
        ("profile_lease_id", "profile-lease-x"),
        ("owner_id", "actor-x"),
        ("attempt_id", "attempt-x"),
        ("runtime_id", "runtime-x"),
        ("schema_version", "x"),
    ):
        changed_bundle = cast(
            BrowserLeaseBundle,
            SimpleNamespace(
                page_binding=replace(
                    bundle.page_binding, **{field_name: value}
                )
            ),
        )
        assert (
            semantic_ops._operation_digest(
                changed_bundle, request, issuer.policy_version
            )
            != authority.operation_digest
        )


def test_public_postcondition_digest_is_stable_and_strict(setup) -> None:
    *_, request, _, _ = setup
    digest = resume_postcondition_digest(request.expected_postcondition)
    assert len(digest) == 64
    assert digest == resume_postcondition_digest(request.expected_postcondition)
    assert digest != resume_postcondition_digest(
        replace(request.expected_postcondition, filename="other.pdf")
    )


@pytest.mark.parametrize(
    ("field_name", "tampered"),
    [
        ("actor_id", "actor-x"),
        ("attempt_id", "attempt-x"),
        ("provider", "smartrecruiters"),
        ("operation", "submit"),
        ("artifact_sha256", "0" * 64),
        ("artifact_size_bytes", 999),
        ("artifact_filename", "other.pdf"),
        ("container_key", "other:resume"),
        ("application_binding_hash", "1" * 64),
        ("material_binding_hash", "2" * 64),
        ("policy_digest", "3" * 64),
        ("adapter_version", "workday/v2"),
        (
            "expected_postcondition",
            ResumeUploadPostcondition("other.pdf", 999),
        ),
        ("policy_version", "resume-upload/v2"),
        ("operation_digest", "f" * 64),
        ("expires_at", 999.0),
        ("nonce", "tampered"),
        ("submit_authority", True),
        ("signature", "0" * 64),
    ],
)
def test_tampered_authority_field_is_rejected_before_driver(
    setup, field_name: str, tampered: object
) -> None:
    _, broker, bundle, request, issuer, authority = setup
    driver = FakeDriver(ResumeUploadObservation(request.container_key, None))
    changed = replace(authority, **{field_name: tampered})

    with pytest.raises(SemanticWriteDenied):
        _ops(broker, issuer, driver).upload_bound_resume(bundle, changed, request)
    assert (driver.observe_calls, driver.upload_calls) == (0, 0)


@pytest.mark.parametrize(
    ("field_name", "tampered"),
    [
        ("page_id", "page-x"),
        ("page_lease_id", "page-lease-x"),
        ("page_lease_epoch", 999),
        ("page_epoch", 999),
        ("profile_lease_id", "profile-lease-x"),
        ("owner_id", "actor-x"),
        ("attempt_id", "attempt-x"),
        ("runtime_id", "runtime-x"),
        ("schema_version", "x"),
    ],
)
def test_every_page_binding_identity_is_signed(
    setup, field_name: str, tampered: object
) -> None:
    _, broker, bundle, request, issuer, authority = setup
    changed = replace(
        authority,
        page_binding=replace(authority.page_binding, **{field_name: tampered}),
    )
    driver = FakeDriver(ResumeUploadObservation(request.container_key, None))

    with pytest.raises(SemanticWriteDenied):
        _ops(broker, issuer, driver).upload_bound_resume(bundle, changed, request)
    assert driver.observe_calls == 0


def test_request_artifact_and_binding_tampering_is_rejected(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    driver = FakeDriver(ResumeUploadObservation(request.container_key, None))
    variants = [
        replace(request, container_key="different"),
        replace(request, actor_id="actor-x"),
        replace(request, attempt_id="attempt-x"),
        replace(request, application_binding_hash="1" * 64),
        replace(request, material_binding_hash="2" * 64),
        replace(request, adapter_version="workday/v2"),
        replace(request, artifact=replace(request.artifact, sha256="0" * 64)),
    ]
    for changed in variants:
        with pytest.raises(SemanticWriteDenied):
            _ops(broker, issuer, driver).upload_bound_resume(
                bundle, authority, changed
            )
    assert driver.observe_calls == 0


def test_expired_authority_is_rejected_before_driver(setup) -> None:
    now, broker, bundle, request, issuer, authority = setup
    now[0] = authority.expires_at
    driver = FakeDriver(ResumeUploadObservation(request.container_key, None))
    with pytest.raises(SemanticWriteDenied, match="expired"):
        _ops(broker, issuer, driver).upload_bound_resume(bundle, authority, request)
    assert driver.observe_calls == 0


def test_issue_rejects_submit_started_and_unsupported_provider(setup) -> None:
    _, _, bundle, request, issuer, _ = setup
    with pytest.raises(SemanticWriteDenied, match="pre-submit"):
        issuer.issue(bundle=bundle, request=request, submit_started=True)
    with pytest.raises(ValueError, match="provider"):
        replace(request, provider=cast(object, "greenhouse"))


@pytest.mark.parametrize(
    "change",
    [
        {"application_binding_hash": "A" * 64},
        {"material_binding_hash": "short"},
        {"policy_digest": "0" * 64},
        {"adapter_version": "contains whitespace"},
    ],
)
def test_request_strictly_validates_new_claims(setup, change: dict[str, object]) -> None:
    *_, request, _, _ = setup
    with pytest.raises(ValueError):
        replace(request, **change)


def test_pre_stale_binding_has_zero_driver_calls(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    broker.advance_page(bundle, expected_page_epoch=0)
    driver = FakeDriver(ResumeUploadObservation(request.container_key, None))
    lifecycle = FakeLifecycle()
    with pytest.raises(Exception, match="stale"):
        _ops(broker, issuer, driver, lifecycle).upload_bound_resume(
            bundle, authority, request
        )
    assert (driver.observe_calls, driver.upload_calls) == (0, 0)
    assert lifecycle.events == []


def test_post_write_stale_cas_is_uncertain(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    driver = FakeDriver(
        ResumeUploadObservation(request.container_key, None),
        after_upload=ResumeUploadObservation(
            request.container_key, request.artifact.filename
        ),
        upload_hook=lambda: broker.advance_page(bundle, expected_page_epoch=0),
    )
    lifecycle = FakeLifecycle()
    with pytest.raises(SemanticWriteUncertain, match="CAS"):
        _ops(broker, issuer, driver, lifecycle).upload_bound_resume(
            bundle, authority, request
        )
    assert driver.upload_calls == 1
    assert lifecycle.events[-1] == ("stale_after_effect", 0)


def test_postcondition_mismatch_is_uncertain_and_never_retries(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    driver = FakeDriver(
        ResumeUploadObservation(request.container_key, None),
        after_upload=ResumeUploadObservation(request.container_key, "wrong.pdf"),
    )
    lifecycle = FakeLifecycle()
    with pytest.raises(SemanticWriteUncertain, match="postcondition"):
        _ops(broker, issuer, driver, lifecycle).upload_bound_resume(
            bundle, authority, request
        )
    assert driver.upload_calls == 1
    assert lifecycle.events[-1][0] == "unknown"


def test_upload_driver_exception_is_uncertain_and_never_retries(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    driver = RaisingUploadDriver(
        ResumeUploadObservation(request.container_key, None)
    )
    lifecycle = FakeLifecycle()
    with pytest.raises(SemanticWriteUncertain, match="postcondition"):
        _ops(broker, issuer, driver, lifecycle).upload_bound_resume(
            bundle, authority, request
        )
    assert driver.upload_calls == 1
    assert lifecycle.events[-1][0] == "unknown"


def test_effect_callback_failure_stops_before_cas(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    driver = FakeDriver(
        ResumeUploadObservation(
            request.container_key, request.artifact.filename, request.artifact.size_bytes
        )
    )

    class FailingEffect(FakeLifecycle):
        def mark_effect_observed(self, operation_digest: str) -> None:
            self.events.append("effect_failed")
            raise RuntimeError("journal unavailable")

    lifecycle = FailingEffect()
    with pytest.raises(SemanticWriteUncertain, match="effect recording"):
        _ops(broker, issuer, driver, lifecycle).upload_bound_resume(
            bundle, authority, request
        )
    assert broker.validate_page(bundle.page_binding) == bundle.page_binding
    assert lifecycle.events == ["claimed", "effect_failed"]


def test_claim_callback_failure_stops_before_driver_and_cas(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    driver = FakeDriver(ResumeUploadObservation(request.container_key, None))

    class Unclaimed(FakeLifecycle):
        def require_claimed(self, operation_digest: str) -> None:
            raise RuntimeError("not claimed")

    with pytest.raises(SemanticWriteDenied, match="not externally claimed"):
        _ops(broker, issuer, driver, Unclaimed()).upload_bound_resume(
            bundle, authority, request
        )
    assert (driver.observe_calls, driver.upload_calls) == (0, 0)
    assert broker.validate_page(bundle.page_binding) == bundle.page_binding


def test_verified_callback_failure_is_uncertain_after_cas(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    driver = FakeDriver(
        ResumeUploadObservation(
            request.container_key, request.artifact.filename, request.artifact.size_bytes
        )
    )

    class FailingVerified(FakeLifecycle):
        def mark_verified(self, operation_digest: str, result_epoch: int) -> None:
            raise RuntimeError("journal unavailable")

    with pytest.raises(SemanticWriteUncertain, match="verified journal"):
        _ops(broker, issuer, driver, FailingVerified()).upload_bound_resume(
            bundle, authority, request
        )
    with pytest.raises(Exception, match="stale"):
        broker.validate_page(bundle.page_binding)


def test_unknown_park_callback_failure_remains_fail_closed(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup
    driver = RaisingUploadDriver(
        ResumeUploadObservation(request.container_key, None)
    )

    class FailingPark(FakeLifecycle):
        def park_unknown(self, operation_digest: str, reason: str) -> None:
            raise RuntimeError("journal unavailable")

    with pytest.raises(SemanticWriteUncertain, match="parking both failed"):
        _ops(broker, issuer, driver, FailingPark()).upload_bound_resume(
            bundle, authority, request
        )
    assert broker.validate_page(bundle.page_binding) == bundle.page_binding


def test_stale_effect_park_callback_failure_remains_fail_closed(setup) -> None:
    _, broker, bundle, request, issuer, authority = setup

    class FailingStalePark(FakeLifecycle):
        def park_stale_after_effect(
            self, operation_digest: str, expected_epoch: int
        ) -> None:
            raise RuntimeError("journal unavailable")

    driver = FakeDriver(
        ResumeUploadObservation(request.container_key, None),
        after_upload=ResumeUploadObservation(
            request.container_key, request.artifact.filename
        ),
        upload_hook=lambda: broker.advance_page(bundle, expected_page_epoch=0),
    )
    with pytest.raises(SemanticWriteUncertain, match="parking both failed"):
        _ops(broker, issuer, driver, FailingStalePark()).upload_bound_resume(
            bundle, authority, request
        )


def test_authority_cannot_be_serialized_and_has_no_public_mapping_api(setup) -> None:
    *_, authority = setup
    with pytest.raises(TypeError):
        pickle.dumps(authority)
    with pytest.raises(TypeError):
        json.dumps(authority)
    assert not hasattr(authority, "as_dict")
    assert not hasattr(authority, "to_dict")


def test_different_issuer_cannot_verify_authority(setup) -> None:
    _, broker, bundle, request, _, authority = setup
    other = SemanticWriteAuthorityIssuer()
    driver = FakeDriver(ResumeUploadObservation(request.container_key, None))
    with pytest.raises(SemanticWriteDenied, match="signature"):
        _ops(broker, other, driver).upload_bound_resume(bundle, authority, request)
    assert driver.observe_calls == 0


def test_ops_exposes_no_submit_or_arbitrary_write_surface(setup) -> None:
    _, broker, *_ = setup
    ops = SemanticBrowserOps(broker)
    assert not hasattr(ops, "submit")
    assert not hasattr(ops, "click")
    assert not hasattr(ops, "next")
    with pytest.raises(BrowserAuthorityDenied):
        ops.apply_form_patch({})
    with pytest.raises(BrowserAuthorityDenied):
        ops.upload_artifact("anything")


def test_authority_schema_includes_all_bounded_fields() -> None:
    assert {item.name for item in fields(SemanticWriteAuthority)} >= {
        "actor_id",
        "attempt_id",
        "page_binding",
        "provider",
        "operation",
        "artifact_sha256",
        "artifact_size_bytes",
        "artifact_filename",
        "application_binding_hash",
        "material_binding_hash",
        "policy_digest",
        "adapter_version",
        "expected_postcondition",
        "policy_version",
        "operation_digest",
        "expires_at",
        "nonce",
        "submit_authority",
        "signature",
    }
