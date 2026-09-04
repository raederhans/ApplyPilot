from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from applypilot.apply.app_server_prepare_capability import (
    PREPARE_WRITE_PROMPT_MARKER,
    PREPARE_WRITE_TOOL_NAME,
    PrepareOnlyExecutionContext,
    PrepareOnlyPolicyEngine,
    build_prepare_write_prompt,
    prepare_write_tool_contract,
)
from applypilot.apply.semantic_batch import (
    BatchPageBinding,
    BrowserResourceIdentity,
    SemanticBatchDenied,
    SemanticPatch,
)
from applypilot.apply.semantic_batch_runtime import (
    SemanticBatchRuntimeRequest,
    SemanticBatchRuntimeResult,
)


def _request(tmp_path: Path, *, mode: str = "canary") -> SemanticBatchRuntimeRequest:
    return SemanticBatchRuntimeRequest(
        mode=mode,  # type: ignore[arg-type]
        attempt_id="attempt-prepare",
        actor_id="application:attempt-prepare",
        provider="workday",
        adapter_version="playwright-semantic-batch/v1",
        page_binding=BatchPageBinding(
            "https://example.myworkdayjobs.com/application",
            (),
            "a" * 64,
            4,
        ),
        page_id="application:attempt-prepare",
        page_lease_id="page-lease-prepare",
        page_lease_epoch=2,
        resources=BrowserResourceIdentity(
            str((tmp_path / "journal.db").resolve()),
            str((tmp_path / "profile").resolve()),
            9432,
        ),
        patches=(SemanticPatch("preferred_name", "Private Candidate Name"),),
    )


def _context() -> PrepareOnlyExecutionContext:
    return PrepareOnlyExecutionContext(
        attempt_id="attempt-prepare",
        actor_id="application:attempt-prepare",
        phase="prepare",
        route="browser",
        page_id="application:attempt-prepare",
        page_lease_id="page-lease-prepare",
        page_lease_epoch=2,
        page_epoch=4,
    )


def _verified(request: SemanticBatchRuntimeRequest) -> SemanticBatchRuntimeResult:
    return SemanticBatchRuntimeResult(
        status="verified",
        mode="canary",
        batch_id=request.batch_id,
        candidate_count=len(request.patches),
        effect_count=1,
        legacy_fallback_safe=False,
        reason_code="verified",
    )


def test_prepare_prompt_and_tool_are_distinct_from_read_only_shadow(tmp_path: Path) -> None:
    grant = PrepareOnlyPolicyEngine().issue(_context(), _request(tmp_path))

    prompt = build_prepare_write_prompt(grant)
    contract = prepare_write_tool_contract()

    assert prompt.startswith(PREPARE_WRITE_PROMPT_MARKER)
    assert "SHADOW_OBSERVATION_ONLY" not in prompt
    assert PREPARE_WRITE_TOOL_NAME in prompt
    assert contract["name"] == PREPARE_WRITE_TOOL_NAME
    assert contract["effect_class"] == "browser_write"
    assert contract["authority"] == "prepare_semantic_batch"
    assert contract["phase"] == "prepare"
    assert "Private Candidate Name" not in prompt
    assert "myworkdayjobs.com" not in prompt
    assert "journal.db" not in prompt
    assert "profile" not in prompt


def test_prepare_tool_resolves_host_held_batch_and_consumes_once(tmp_path: Path) -> None:
    engine = PrepareOnlyPolicyEngine()
    request = _request(tmp_path)
    grant = engine.issue(_context(), request)
    seen: list[SemanticBatchRuntimeRequest] = []

    result = engine.execute(
        grant.public_claims(),
        executor=lambda bound: seen.append(bound) or _verified(bound),
    )

    assert seen == [request]
    assert result.status == "verified"
    assert result.submit_authority is False
    with pytest.raises(SemanticBatchDenied, match="already consumed"):
        engine.execute(grant.public_claims(), executor=_verified)


def test_prepare_policy_never_issues_a_second_grant_for_one_batch(tmp_path: Path) -> None:
    engine = PrepareOnlyPolicyEngine()
    request = _request(tmp_path)
    engine.issue(_context(), request)

    with pytest.raises(SemanticBatchDenied, match="already has a prepare grant"):
        engine.issue(_context(), request)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("phase", "submit"),
        ("route", "direct_email"),
        ("navigation_authorized", True),
        ("credential_authorized", True),
        ("mailbox_authorized", True),
        ("final_submit_authorized", True),
        ("reservation_claimed", True),
        ("receipt_authorized", True),
    ),
)
def test_prepare_policy_rejects_every_forbidden_authority(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    context = replace(_context(), **{field: value})

    with pytest.raises(SemanticBatchDenied):
        PrepareOnlyPolicyEngine().issue(context, _request(tmp_path))


def test_prepare_policy_rejects_shadow_batch_and_stale_page_lease(tmp_path: Path) -> None:
    engine = PrepareOnlyPolicyEngine()

    with pytest.raises(SemanticBatchDenied, match="canary"):
        engine.issue(_context(), _request(tmp_path, mode="shadow"))
    with pytest.raises(SemanticBatchDenied, match="current page lease"):
        engine.issue(
            replace(_context(), page_epoch=5),
            _request(tmp_path),
        )


def test_prepare_grant_tampering_and_expiry_fail_before_dispatch(tmp_path: Path) -> None:
    clock = [10.0]
    engine = PrepareOnlyPolicyEngine(ttl_seconds=5, clock=lambda: clock[0])
    grant = engine.issue(_context(), _request(tmp_path))
    calls: list[object] = []
    tampered = {**grant.public_claims(), "page_epoch": 5}

    with pytest.raises(SemanticBatchDenied, match="modified"):
        engine.execute(tampered, executor=lambda request: calls.append(request))  # type: ignore[arg-type,return-value]
    clock[0] = 15.0
    with pytest.raises(SemanticBatchDenied, match="expired"):
        engine.execute(grant.public_claims(), executor=_verified)
    assert calls == []


def test_executor_failure_consumes_grant_and_never_retries(tmp_path: Path) -> None:
    engine = PrepareOnlyPolicyEngine()
    grant = engine.issue(_context(), _request(tmp_path))
    calls = 0

    def fail(_request: SemanticBatchRuntimeRequest) -> SemanticBatchRuntimeResult:
        nonlocal calls
        calls += 1
        raise RuntimeError("synthetic uncertain effect")

    with pytest.raises(RuntimeError, match="uncertain effect"):
        engine.execute(grant.public_claims(), executor=fail)
    with pytest.raises(SemanticBatchDenied, match="already consumed"):
        engine.execute(grant.public_claims(), executor=fail)
    assert calls == 1


def test_prepare_executor_result_must_preserve_batch_and_no_submit(tmp_path: Path) -> None:
    engine = PrepareOnlyPolicyEngine()
    request = _request(tmp_path)
    grant = engine.issue(_context(), request)

    def wrong_batch(_request: SemanticBatchRuntimeRequest) -> SemanticBatchRuntimeResult:
        return SemanticBatchRuntimeResult(
            status="verified",
            mode="canary",
            batch_id="semantic-batch:wrong",
            candidate_count=1,
            effect_count=1,
            legacy_fallback_safe=False,
            reason_code="verified",
        )

    with pytest.raises(SemanticBatchDenied, match="violated"):
        engine.execute(grant.public_claims(), executor=wrong_batch)
