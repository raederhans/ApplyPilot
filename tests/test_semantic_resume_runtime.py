from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from playwright import sync_api

from applypilot.apply.browser_broker import BrowserBroker
from applypilot.apply.semantic_browser_ops import (
    SEMANTIC_WRITE_POLICY,
    SEMANTIC_WRITE_POLICY_DIGEST,
    ResumeUploadPostcondition,
    ResumeUploadRequest,
    SemanticBrowserOps,
    SemanticWriteAuthorityIssuer,
    resume_postcondition_digest,
)
from applypilot.apply.semantic_resume_runtime import (
    DurableSemanticWriteLifecycle,
    PlaywrightResumeUploadDriver,
    SemanticResumeTargetError,
    application_binding_hash,
    bound_artifact,
    expected_postcondition_digest,
    material_binding_hash,
    operation_id,
    provider_for_url,
)
from applypilot.apply.semantic_resume_upload import ADAPTER_VERSION
from applypilot.storage import semantic_browser_writes as journal


class Input:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.calls = 0

    def get_attribute(self, name: str) -> str | None:
        return {
            "id": "resume",
            "accept": ".pdf",
            "required": "",
        }.get(name)

    def evaluate(self, _script: str) -> object:
        return {
            "labels": ["Resume / CV"],
            "ancestor_texts": ["Resume / CV"],
            "container_text": self.path.name if self.path else "",
            "action_text": ["Remove"] if self.path else [],
            "uploaded_filename": self.path.name if self.path else None,
            "uploaded_size": self.path.stat().st_size if self.path else None,
        }

    def set_input_files(self, value: str) -> None:
        self.calls += 1
        self.path = Path(value)


class Surface:
    def __init__(self, *inputs: Input) -> None:
        self.inputs = list(inputs)

    def query_selector_all(self, selector: str) -> list[Input]:
        assert selector == 'input[type="file"]'
        return self.inputs


class LocalSelectionOnlyInput(Input):
    def evaluate(self, _script: str) -> object:
        return {
            "labels": ["Resume / CV"],
            "ancestor_texts": ["Resume / CV"],
            "container_text": "",
            "action_text": ["Remove"],
            "status_text": [],
            "busy": False,
            "uploaded_filename": self.path.name if self.path else None,
            "uploaded_size": self.path.stat().st_size if self.path else None,
        }


def _artifact(tmp_path: Path):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF")
    return bound_artifact(
        path,
        hashlib.sha256(path.read_bytes()).hexdigest(),
        path.stat().st_size,
    )


def test_provider_resolution_is_exact_https_only() -> None:
    assert provider_for_url("https://tenant.wd5.myworkdayjobs.com/apply") == "workday"
    assert provider_for_url("https://tenant.myworkdaysite.com/apply") == "workday"
    assert provider_for_url("https://jobs.smartrecruiters.com/Example/1") == (
        "smartrecruiters"
    )
    assert provider_for_url("http://jobs.smartrecruiters.com/Example/1") is None
    assert provider_for_url("https://smartrecruiters.com.evil.test/apply") is None
    assert provider_for_url("not a url") is None


def test_bridge_uploads_once_and_maps_the_same_container(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    element = Input()
    driver = PlaywrightResumeUploadDriver(Surface(element), "workday")
    discovered = driver.discover()
    request = ResumeUploadRequest(
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        provider="workday",
        container_key=discovered.container_key or "",
        artifact=artifact,
        application_binding_hash="a" * 64,
        material_binding_hash="b" * 64,
        policy_digest=SEMANTIC_WRITE_POLICY_DIGEST,
        adapter_version=ADAPTER_VERSION,
        expected_postcondition=ResumeUploadPostcondition(
            filename=artifact.filename,
            size_bytes=artifact.size_bytes,
        ),
    )

    driver.upload_resume(request)
    observed = driver.observe_resume(request)

    assert element.calls == 1
    assert observed.container_key == request.container_key
    assert observed.filename == artifact.filename
    assert observed.size_bytes == artifact.size_bytes


def test_bridge_never_maps_local_selection_as_provider_acceptance(
    tmp_path: Path,
) -> None:
    artifact = _artifact(tmp_path)
    element = LocalSelectionOnlyInput(artifact.path)
    driver = PlaywrightResumeUploadDriver(Surface(element), "workday")
    discovered = driver.discover()
    request = ResumeUploadRequest(
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        provider="workday",
        container_key=discovered.container_key or "",
        artifact=artifact,
        application_binding_hash="a" * 64,
        material_binding_hash="b" * 64,
        policy_digest=SEMANTIC_WRITE_POLICY_DIGEST,
        adapter_version=ADAPTER_VERSION,
        expected_postcondition=ResumeUploadPostcondition(
            filename=artifact.filename,
            size_bytes=artifact.size_bytes,
        ),
    )

    observed = driver.observe_resume(request)

    assert observed.filename is None
    assert observed.size_bytes is None
    with pytest.raises(SemanticResumeTargetError, match="existing_upload_state_unresolved"):
        driver.upload_resume(request)
    assert element.calls == 0


def test_bridge_rejects_ambiguous_target_without_write(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    first = Input()
    second = Input()
    driver = PlaywrightResumeUploadDriver(Surface(first, second), "workday")
    with pytest.raises(SemanticResumeTargetError, match="manual"):
        driver.observe_resume(  # request contents are not consulted before target binding
            ResumeUploadRequest(
                actor_id="application:attempt-1",
                attempt_id="attempt-1",
                provider="workday",
                container_key="unresolved",
                artifact=artifact,
                application_binding_hash="a" * 64,
                material_binding_hash="b" * 64,
                policy_digest=SEMANTIC_WRITE_POLICY_DIGEST,
                adapter_version=ADAPTER_VERSION,
                expected_postcondition=ResumeUploadPostcondition(
                    filename=artifact.filename,
                    size_bytes=artifact.size_bytes,
                ),
            )
        )
    assert first.calls == second.calls == 0


def test_binding_hashes_exclude_paths_and_commit_only_bounded_identity() -> None:
    job = {
        "_attempt_id": "attempt-1",
        "url": "https://jobs.example.test/1",
        "application_url": "https://tenant.myworkdayjobs.com/apply",
        "tailor_status": "validated",
    }
    entry = {
        "url": job["url"],
        "application_url": job["application_url"],
        "target_host": "tenant.myworkdayjobs.com",
        "job_fingerprint": "c" * 64,
        "resume_sha256": "d" * 64,
        "resume_size": 42,
        "resume_path": "C:/private/one.pdf",
    }

    app_hash = application_binding_hash(
        job,
        entry,
        page_url="https://tenant.myworkdayjobs.com/apply/step/1?token=secret",
    )
    material_hash = material_binding_hash(job, entry)
    changed_path = {**entry, "resume_path": "D:/other/private.pdf"}

    assert len(app_hash) == len(material_hash) == 64
    assert material_binding_hash(job, changed_path) == material_hash
    assert application_binding_hash(
        {**job, "_attempt_id": "attempt-2"},
        entry,
        page_url="https://tenant.myworkdayjobs.com/apply/step/1",
    ) != app_hash


def test_durable_lifecycle_enforces_claim_effect_then_verified() -> None:
    connection = sqlite3.connect(":memory:")
    digest = "a" * 64
    operation = operation_id(digest)
    claims = journal.SemanticWriteClaims(
        operation_id=operation,
        operation_digest=digest,
        actor_id="application:attempt-1",
        attempt_id="attempt-1",
        provider="workday",
        operation_kind=journal.OPERATION_KIND,
        adapter_version=ADAPTER_VERSION,
        application_binding_hash="b" * 64,
        page_id="application:attempt-1",
        page_lease_id="lease-1",
        page_lease_epoch=1,
        expected_page_epoch=0,
        artifact_sha256="c" * 64,
        artifact_size=42,
        material_binding_hash="d" * 64,
        policy_contract_version=SEMANTIC_WRITE_POLICY,
        policy_digest=SEMANTIC_WRITE_POLICY_DIGEST,
        expected_postcondition_digest="e" * 64,
    )
    journal.begin_operation(connection, claims)
    lifecycle = DurableSemanticWriteLifecycle(
        connection,
        operation_id=operation,
        operation_digest=digest,
    )

    with pytest.raises(journal.SemanticWriteTransitionError, match="claimed"):
        lifecycle.require_claimed(digest)
    journal.claim_dispatch(connection, operation, expected_dispatch_count=0)
    lifecycle.require_claimed(digest)
    lifecycle.mark_effect_observed(digest)
    lifecycle.mark_verified(digest, 1)

    record = journal.get_operation(connection, operation)
    assert record is not None
    assert record.state == "verified"
    with pytest.raises(journal.SemanticWriteCollision):
        lifecycle.require_claimed("f" * 64)


def test_postcondition_and_operation_ids_are_path_free(tmp_path: Path) -> None:
    artifact = _artifact(tmp_path)
    digest = expected_postcondition_digest(artifact)

    assert len(digest) == 64
    assert str(tmp_path) not in digest
    assert operation_id(digest) == f"semantic-resume:{digest}"
    with pytest.raises(ValueError, match="operation_digest"):
        operation_id("not-a-digest")


@pytest.mark.parametrize(
    ("provider", "html", "optional_selector"),
    [
        (
            "workday",
            """
            <section id="resume-container">
              <label for="resume">Resume / CV</label>
              <input id="resume" type="file" accept="application/pdf">
              <button type="button">Remove</button>
            </section>
            """,
            None,
        ),
        (
            "smartrecruiters",
            """
            <section id="easy-container">
              <label for="easy">Resume Easy Apply autocomplete (optional)</label>
              <input id="easy" type="file" accept="application/pdf">
            </section>
            <section id="resume-container">
              <label for="resume">Resume</label>
              <input id="resume" type="file" required accept="application/pdf">
              <button type="button">Remove</button>
            </section>
            """,
            "#easy",
        ),
    ],
)
def test_real_browser_full_semantic_stack_is_submit_free_and_journaled(
    tmp_path: Path,
    provider: str,
    html: str,
    optional_selector: str | None,
) -> None:
    artifact = _artifact(tmp_path)
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error as exc:
            pytest.skip(f"local Playwright browser unavailable: {exc}")
        page = browser.new_page()
        page.set_content(
            html
            + """
            <script>
              document.querySelector('#resume').addEventListener('change', (event) => {
                document.querySelector('#resume-container').append(
                  document.createTextNode(event.target.files[0].name)
                );
              });
            </script>
            """
        )
        driver = PlaywrightResumeUploadDriver(page, provider)
        discovered = driver.discover()
        broker = BrowserBroker()
        bundle = broker.acquire_bundle(
            profile_id="profile-1",
            page_id="page-1",
            owner_id="application:attempt-1",
            scope_id="scope-1",
            attempt_id="attempt-1",
            runtime_id="runtime-1",
        )
        postcondition = ResumeUploadPostcondition(
            filename=artifact.filename,
            size_bytes=artifact.size_bytes,
        )
        request = ResumeUploadRequest(
            actor_id="application:attempt-1",
            attempt_id="attempt-1",
            provider=provider,  # type: ignore[arg-type]
            container_key=discovered.container_key or "",
            artifact=artifact,
            application_binding_hash="a" * 64,
            material_binding_hash="b" * 64,
            policy_digest=SEMANTIC_WRITE_POLICY_DIGEST,
            adapter_version=ADAPTER_VERSION,
            expected_postcondition=postcondition,
        )
        issuer = SemanticWriteAuthorityIssuer()
        authority = issuer.issue(
            bundle=bundle,
            request=request,
            submit_started=False,
        )
        connection = sqlite3.connect(":memory:")
        op_id = operation_id(authority.operation_digest)
        journal.begin_operation(
            connection,
            journal.SemanticWriteClaims(
                operation_id=op_id,
                operation_digest=authority.operation_digest,
                actor_id=request.actor_id,
                attempt_id=request.attempt_id,
                provider=provider,
                operation_kind=journal.OPERATION_KIND,
                adapter_version=ADAPTER_VERSION,
                application_binding_hash=request.application_binding_hash,
                page_id=bundle.page_binding.page_id,
                page_lease_id=bundle.page_binding.page_lease_id,
                page_lease_epoch=bundle.page_binding.page_lease_epoch,
                expected_page_epoch=bundle.page_binding.page_epoch,
                artifact_sha256=artifact.sha256,
                artifact_size=artifact.size_bytes,
                material_binding_hash=request.material_binding_hash,
                policy_contract_version=SEMANTIC_WRITE_POLICY,
                policy_digest=SEMANTIC_WRITE_POLICY_DIGEST,
                expected_postcondition_digest=resume_postcondition_digest(
                    postcondition
                ),
            ),
        )
        journal.claim_dispatch(connection, op_id, expected_dispatch_count=0)
        lifecycle = DurableSemanticWriteLifecycle(
            connection,
            operation_id=op_id,
            operation_digest=authority.operation_digest,
        )

        result = SemanticBrowserOps(
            broker,
            authority_issuer=issuer,
            resume_driver=driver,
            lifecycle=lifecycle,
        ).upload_bound_resume(bundle, authority, request)

        assert result.uploaded is True
        assert result.bundle.page_binding.page_epoch == 1
        assert page.locator("#resume").evaluate("el => el.files[0].name") == (
            artifact.filename
        )
        if optional_selector is not None:
            assert page.locator(optional_selector).evaluate("el => el.files.length") == 0
        persisted = journal.get_operation(connection, op_id)
        assert persisted is not None
        assert persisted.state == "verified"
        assert persisted.resulting_page_epoch == 1
        assert not hasattr(SemanticBrowserOps, "submit")
        browser.close()
