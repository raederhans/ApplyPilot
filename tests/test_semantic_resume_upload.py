from __future__ import annotations

from pathlib import Path

import pytest

from applypilot.apply import semantic_resume_upload as upload_driver
from applypilot.apply.semantic_resume_upload import (
    LocalPdfArtifact,
    observe_resume_upload,
    upload_resume,
)


class FakeInput:
    def __init__(
        self,
        *,
        attrs: dict[str, str | None],
        labels: list[str],
        ancestors: list[str] | None = None,
        proof_after_upload: bool = True,
        preexisting_actions: list[str] | None = None,
        preexisting_container_text: str = "",
        after_upload_states: list[dict[str, object]] | None = None,
    ) -> None:
        self.attrs = attrs
        self.labels = labels
        self.ancestors = ancestors or []
        self.proof_after_upload = proof_after_upload
        self.preexisting_actions = preexisting_actions or []
        self.preexisting_container_text = preexisting_container_text
        self.after_upload_states = list(after_upload_states or [])
        self.after_upload_observations = 0
        self.uploaded: Path | None = None
        self.set_calls = 0
        self.click_calls = 0

    def get_attribute(self, name: str) -> str | None:
        return self.attrs.get(name)

    def evaluate(self, _expression: str) -> object:
        filename = self.uploaded.name if self.uploaded else None
        size = self.uploaded.stat().st_size if self.uploaded else None
        state: dict[str, object] = {
            "labels": self.labels,
            "ancestor_texts": self.ancestors,
            "container_text": (
                filename
                if filename and self.proof_after_upload
                else self.preexisting_container_text
            ),
            "action_text": (
                ["Remove"]
                if filename and self.proof_after_upload
                else self.preexisting_actions
            ),
            "status_text": [],
            "busy": False,
            "uploaded_filename": filename,
            "uploaded_size": size,
        }
        if filename and self.after_upload_states:
            index = min(
                self.after_upload_observations,
                len(self.after_upload_states) - 1,
            )
            state.update(self.after_upload_states[index])
            self.after_upload_observations += 1
        return state

    def set_input_files(self, files: str) -> None:
        self.set_calls += 1
        self.uploaded = Path(files)

    def click(self) -> None:
        self.click_calls += 1
        raise AssertionError("the upload driver must never click")


class FakeSurface:
    def __init__(self, inputs: list[FakeInput]) -> None:
        self.inputs = inputs
        self.submit_calls = 0
        self.click_calls = 0

    def query_selector_all(self, selector: str) -> list[FakeInput]:
        assert selector == 'input[type="file"]'
        return self.inputs

    def click(self, _selector: str) -> None:
        self.click_calls += 1
        raise AssertionError("the upload driver must never click")

    def submit(self) -> None:
        self.submit_calls += 1
        raise AssertionError("the upload driver must never submit")


@pytest.fixture
def pdf_artifact(tmp_path: Path) -> LocalPdfArtifact:
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF")
    return LocalPdfArtifact(path=path, filename=path.name, size_bytes=path.stat().st_size)


def _input(
    label: str,
    *,
    input_id: str = "resume-upload",
    required: bool = False,
    accept: str = ".pdf",
    disabled: bool = False,
    proof_after_upload: bool = True,
) -> FakeInput:
    attrs = {"id": input_id, "accept": accept}
    if required:
        attrs["required"] = ""
    if disabled:
        attrs["disabled"] = ""
    return FakeInput(attrs=attrs, labels=[label], proof_after_upload=proof_after_upload)


def test_workday_uploads_once_and_verifies_same_container(pdf_artifact: LocalPdfArtifact) -> None:
    resume = _input("Resume / CV")
    surface = FakeSurface([resume])

    observed = observe_resume_upload(surface, "workday")
    result = upload_resume(observed, pdf_artifact)

    assert observed.status == "ready"
    assert observed.container_key
    assert observed.file_input_identity == "id:resume-upload"
    assert result.status == "uploaded"
    assert result.set_input_files_calls == 1
    assert result.observation.uploaded_filename == "resume.pdf"
    assert result.observation.uploaded_size == pdf_artifact.size_bytes
    assert result.observation.proof is not None
    assert result.observation.proof.container_text_has_filename
    assert result.observation.proof.has_remove_action
    assert resume.set_calls == 1
    assert resume.click_calls == surface.click_calls == surface.submit_calls == 0


def test_smartrecruiters_uses_required_resume_not_optional_easy_apply(
    pdf_artifact: LocalPdfArtifact,
) -> None:
    optional = _input("Easy Apply autocomplete (optional)", input_id="easy-apply")
    required = _input("Resume", input_id="required-resume", required=True)
    surface = FakeSurface([optional, required])

    observed = observe_resume_upload(surface, "smartrecruiters")
    result = upload_resume(observed, pdf_artifact)

    assert observed.file_input_identity == "id:required-resume"
    assert result.status == "uploaded"
    assert optional.set_calls == 0
    assert required.set_calls == 1


def test_smartrecruiters_optional_easy_apply_alone_is_unsupported() -> None:
    optional = _input("Resume Easy Apply autocomplete (optional)", input_id="easy-apply")

    observed = observe_resume_upload(FakeSurface([optional]), "smartrecruiters")

    assert observed.status == "unsupported"
    assert optional.set_calls == 0


@pytest.mark.parametrize(
    "label",
    ["Resume cover letter", "Additional Resume attachment", "Resume supporting document"],
)
def test_non_resume_attachments_are_excluded(label: str) -> None:
    element = _input(label)

    observed = observe_resume_upload(FakeSurface([element]), "workday")

    assert observed.status == "unsupported"
    assert element.set_calls == 0


def test_two_eligible_resume_inputs_are_manual_and_zero_write(pdf_artifact: LocalPdfArtifact) -> None:
    first = _input("Resume", input_id="resume-one")
    second = _input("CV", input_id="resume-two")
    observed = observe_resume_upload(FakeSurface([first, second]), "workday")

    result = upload_resume(observed, pdf_artifact)

    assert observed.status == "manual"
    assert observed.reason == "ambiguous_resume_inputs"
    assert result.set_input_files_calls == 0
    assert first.set_calls == second.set_calls == 0


@pytest.mark.parametrize(
    ("disabled", "accept"),
    [(True, ".pdf"), (False, "image/png,.docx")],
)
def test_disabled_or_incompatible_input_is_zero_write(
    disabled: bool,
    accept: str,
    pdf_artifact: LocalPdfArtifact,
) -> None:
    element = _input("Resume", disabled=disabled, accept=accept)
    observed = observe_resume_upload(FakeSurface([element]), "workday")

    result = upload_resume(observed, pdf_artifact)

    assert observed.status == "unsupported"
    assert result.set_input_files_calls == 0
    assert element.set_calls == 0


def test_upload_fails_closed_without_same_container_proof(pdf_artifact: LocalPdfArtifact) -> None:
    element = _input("Resume", proof_after_upload=False)
    observed = observe_resume_upload(FakeSurface([element]), "workday")

    result = upload_resume(observed, pdf_artifact)

    assert result.status == "failed"
    assert result.reason == "provider_acceptance_unverified"
    assert result.set_input_files_calls == 1
    assert element.set_calls == 1


def test_preexisting_remove_and_local_file_selection_do_not_prove_provider_acceptance(
    pdf_artifact: LocalPdfArtifact,
) -> None:
    element = FakeInput(
        attrs={"id": "resume-upload", "accept": ".pdf"},
        labels=["Resume"],
        proof_after_upload=False,
        preexisting_actions=["Remove"],
    )
    observed = observe_resume_upload(FakeSurface([element]), "workday")

    result = upload_resume(observed, pdf_artifact)

    assert result.status == "failed"
    assert result.reason == "provider_acceptance_unverified"
    assert result.observation.uploaded_filename == pdf_artifact.filename
    assert result.observation.proof is not None
    assert not result.observation.proof.container_text_has_filename
    assert result.observation.proof.has_remove_action
    assert element.set_calls == 1


def test_existing_local_selection_without_provider_acceptance_is_never_rewritten(
    pdf_artifact: LocalPdfArtifact,
) -> None:
    element = FakeInput(
        attrs={"id": "resume-upload", "accept": ".pdf"},
        labels=["Resume"],
        proof_after_upload=False,
        preexisting_actions=["Remove"],
    )
    element.uploaded = pdf_artifact.path
    observed = observe_resume_upload(
        FakeSurface([element]),
        "workday",
        expected=pdf_artifact,
    )

    result = upload_resume(observed, pdf_artifact)

    assert result.status == "manual"
    assert result.reason == "existing_upload_state_unresolved"
    assert result.set_input_files_calls == 0
    assert element.set_calls == 0


@pytest.mark.parametrize("misleading_name", ["old-resume.pdf", "resume.pdf.bak"])
def test_preexisting_similar_filename_card_is_not_existing_acceptance(
    misleading_name: str,
    pdf_artifact: LocalPdfArtifact,
) -> None:
    accepted = {
        "container_text": f"{pdf_artifact.filename} Uploaded",
        "action_text": ["Remove"],
        "status_text": ["Upload complete"],
    }
    element = FakeInput(
        attrs={"id": "resume-upload", "accept": ".pdf"},
        labels=["Resume"],
        proof_after_upload=False,
        preexisting_actions=["Remove"],
        preexisting_container_text=misleading_name,
        after_upload_states=[accepted, accepted, accepted],
    )
    observed = observe_resume_upload(
        FakeSurface([element]),
        "workday",
        expected=pdf_artifact,
    )

    result = upload_resume(observed, pdf_artifact)

    assert observed.proof is not None
    assert not observed.proof.container_text_has_filename
    assert result.status == "uploaded"
    assert result.reason == "provider_acceptance_stable"
    assert element.set_calls == 1


@pytest.mark.parametrize("misleading_name", ["old-resume.pdf", "resume.pdf.bak"])
def test_similar_filename_after_write_never_proves_acceptance(
    monkeypatch,
    misleading_name: str,
    pdf_artifact: LocalPdfArtifact,
) -> None:
    monkeypatch.setattr(upload_driver.time, "sleep", lambda _seconds: None)
    element = FakeInput(
        attrs={"id": "resume-upload", "accept": ".pdf"},
        labels=["Resume"],
        after_upload_states=[
            {
                "container_text": misleading_name,
                "action_text": ["Remove"],
                "status_text": ["Upload complete"],
            }
        ],
    )
    observed = observe_resume_upload(FakeSurface([element]), "workday")

    result = upload_resume(observed, pdf_artifact)

    assert result.status == "failed"
    assert result.reason == "provider_acceptance_unverified"
    assert result.observation.proof is not None
    assert not result.observation.proof.container_text_has_filename
    assert element.set_calls == 1


def test_async_provider_error_prevents_acceptance(
    pdf_artifact: LocalPdfArtifact,
) -> None:
    accepted = {
        "container_text": f"{pdf_artifact.filename} Uploaded",
        "action_text": ["Remove"],
    }
    element = FakeInput(
        attrs={"id": "resume-upload", "accept": ".pdf"},
        labels=["Resume"],
        after_upload_states=[
            accepted,
            accepted,
            {
                "container_text": pdf_artifact.filename,
                "action_text": ["Remove"],
                "status_text": ["Upload failed"],
            },
        ],
    )
    observed = observe_resume_upload(FakeSurface([element]), "workday")

    result = upload_resume(observed, pdf_artifact)

    assert result.status == "failed"
    assert result.reason == "provider_upload_error_visible"
    assert result.observation.proof is not None
    assert result.observation.proof.has_upload_error


def test_delayed_provider_card_must_stabilize_before_acceptance(
    pdf_artifact: LocalPdfArtifact,
) -> None:
    accepted = {
        "container_text": f"{pdf_artifact.filename} Uploaded",
        "action_text": ["Remove"],
        "status_text": ["Upload complete"],
        "busy": False,
    }
    element = FakeInput(
        attrs={"id": "resume-upload", "accept": ".pdf"},
        labels=["Resume"],
        after_upload_states=[
            {
                "container_text": "Uploading",
                "status_text": ["Uploading"],
                "busy": True,
            },
            accepted,
            accepted,
            accepted,
        ],
    )
    observed = observe_resume_upload(FakeSurface([element]), "workday")

    result = upload_resume(observed, pdf_artifact)

    assert result.status == "uploaded"
    assert result.reason == "provider_acceptance_stable"
    assert element.after_upload_observations >= 4


def test_smartrecruiters_parser_in_progress_never_verifies(
    monkeypatch,
    pdf_artifact: LocalPdfArtifact,
) -> None:
    monkeypatch.setattr(upload_driver.time, "sleep", lambda _seconds: None)
    element = FakeInput(
        attrs={"id": "resume-upload", "accept": ".pdf", "required": ""},
        labels=["Resume"],
        after_upload_states=[
            {
                "container_text": pdf_artifact.filename,
                "action_text": ["Remove"],
                "status_text": ["Parsing resume"],
                "busy": False,
            }
        ],
    )
    observed = observe_resume_upload(FakeSurface([element]), "smartrecruiters")

    result = upload_resume(observed, pdf_artifact)

    assert result.status == "failed"
    assert result.reason == "provider_acceptance_unverified"
    assert result.observation.proof is not None
    assert result.observation.proof.upload_in_progress


def test_artifact_metadata_mismatch_is_zero_write(
    pdf_artifact: LocalPdfArtifact,
) -> None:
    element = _input("Resume")
    observed = observe_resume_upload(FakeSurface([element]), "workday")
    wrong = LocalPdfArtifact(
        path=pdf_artifact.path,
        filename=pdf_artifact.filename,
        size_bytes=pdf_artifact.size_bytes + 1,
    )

    result = upload_resume(observed, wrong)

    assert result.status == "failed"
    assert result.reason == "artifact_metadata_mismatch"
    assert result.set_input_files_calls == 0
    assert element.set_calls == 0


@pytest.mark.browser
def test_real_playwright_page_set_content_upload(tmp_path: Path) -> None:
    sync_api = pytest.importorskip("playwright.sync_api")
    path = tmp_path / "browser-resume.pdf"
    path.write_bytes(b"%PDF-1.4\n%%EOF")
    artifact = LocalPdfArtifact(path=path, filename=path.name, size_bytes=path.stat().st_size)

    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except sync_api.Error as exc:
            pytest.skip(f"local Playwright browser unavailable: {exc}")
        page = browser.new_page()
        page.set_content(
            """
            <section id="resume-container">
              <label for="resume">Resume / CV</label>
              <input id="resume" type="file" accept="application/pdf">
              <button type="button">Remove</button>
            </section>
            <script>
              document.querySelector('#resume').addEventListener('change', (event) => {
                const name = event.target.files[0].name;
                document.querySelector('#resume-container').append(document.createTextNode(name));
              });
            </script>
            """
        )

        observed = observe_resume_upload(page, "workday")
        result = upload_resume(observed, artifact)

        assert result.status == "uploaded"
        assert result.observation.proof is not None
        assert result.observation.proof.container_text_has_filename
        browser.close()
