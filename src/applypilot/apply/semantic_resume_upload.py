"""Deterministic, submit-free resume upload driver for supported ATS pages."""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from playwright.sync_api import Error as PlaywrightError

from applypilot.apply.provider_registry import provider_supports

Provider = Literal["workday", "smartrecruiters"]
ObservationStatus = Literal["ready", "unsupported", "manual"]
UploadStatus = Literal["uploaded", "unsupported", "manual", "failed"]
ADAPTER_VERSION = "resume-upload-driver/v1"

_POSITIVE_PATTERN = re.compile(r"\b(?:resume|résumé|cv|curriculum\s+vitae)\b", re.IGNORECASE)
_NEGATIVE_PATTERN = re.compile(
    r"\b(?:cover\s+letter|additional(?:\s+\w+){0,3}\s+(?:attachment|document)|"
    r"supporting\s+(?:attachment|document)|easy\s+apply|autocomplete|optional)\b",
    re.IGNORECASE,
)
_STRUCTURE_SCRIPT = """
(el) => {
  const labels = Array.from(el.labels || []).map((node) => node.innerText || node.textContent || "");
  const ancestorTexts = [];
  let node = el.parentElement;
  for (let depth = 0; node && depth < 3; depth += 1, node = node.parentElement) {
    if (["BODY", "HTML", "FORM"].includes(node.tagName)) break;
    ancestorTexts.push(node.innerText || node.textContent || "");
  }
  const container = el.closest("[data-automation-id*='file'], [data-testid*='file'], fieldset, section, div")
    || el.parentElement;
  const containerText = container ? (container.innerText || container.textContent || "") : "";
  const actionText = container
    ? Array.from(container.querySelectorAll("button, [role='button'], a"))
        .map((item) => item.innerText || item.textContent || item.getAttribute("aria-label") || "")
    : [];
  const statusText = container
    ? Array.from(container.querySelectorAll(
        "[role='alert'], [aria-live], [data-automation-id*='error'], " +
        "[data-testid*='error'], [class*='error'], [class*='status']"
      )).map((item) => item.innerText || item.textContent || "")
    : [];
  const busy = Boolean(
    el.getAttribute("aria-busy") === "true" ||
    (container && container.querySelector(
      "[aria-busy='true'], progress, [role='progressbar'], " +
      "[data-automation-id*='progress'], [class*='spinner'], [class*='loading']"
    ))
  );
  const file = el.files && el.files.length === 1 ? el.files[0] : null;
  return {
    labels,
    ancestor_texts: ancestorTexts,
    container_text: containerText,
    action_text: actionText,
    status_text: statusText,
    busy,
    uploaded_filename: file ? file.name : null,
    uploaded_size: file ? file.size : null,
  };
}
"""


@runtime_checkable
class FileInput(Protocol):
    """Small ElementHandle-compatible surface used by the driver."""

    def get_attribute(self, name: str) -> str | None: ...

    def evaluate(self, expression: str) -> object: ...

    def set_input_files(self, files: str) -> None: ...


@runtime_checkable
class ApplicationSurface(Protocol):
    """Small Page/Frame-compatible surface used by the driver."""

    def query_selector_all(self, selector: str) -> list[FileInput]: ...


@dataclass(frozen=True)
class LocalPdfArtifact:
    """Parent-supplied metadata for an exact local PDF; content is never read."""

    path: Path
    filename: str
    size_bytes: int


@dataclass(frozen=True)
class ResumeContainerProof:
    """Sanitized structural evidence from one upload container."""

    container_text_has_filename: bool
    has_remove_action: bool
    has_replace_action: bool
    has_acceptance_marker: bool
    has_upload_error: bool
    upload_in_progress: bool


@dataclass(frozen=True)
class ResumeUploadObservation:
    status: ObservationStatus
    provider: str
    reason: str
    container_key: str | None = None
    file_input_identity: str | None = None
    uploaded_filename: str | None = None
    uploaded_size: int | None = None
    proof: ResumeContainerProof | None = None
    _input: FileInput | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class ResumeUploadResult:
    status: UploadStatus
    reason: str
    observation: ResumeUploadObservation
    set_input_files_calls: int


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _truthy_attribute(value: str | None) -> bool:
    return value is not None and value.lower() not in {"false", "0"}


def _pdf_accepted(accept: str | None) -> bool:
    if not accept:
        return True
    values = {_clean(item).lower() for item in accept.split(",")}
    return bool(values & {".pdf", "application/pdf", "application/*", "*/*"})


def _safe_structure(input_element: FileInput) -> dict[str, object]:
    raw = input_element.evaluate(_STRUCTURE_SCRIPT)
    if not isinstance(raw, dict):
        return {}
    return raw


def _identity(input_element: FileInput) -> str | None:
    for attribute in ("id", "name", "data-testid", "data-automation-id", "aria-label"):
        value = _clean(input_element.get_attribute(attribute))
        if value:
            return f"{attribute}:{value}"
    return None


def _candidate_text(input_element: FileInput, structure: dict[str, object]) -> str:
    attributes = [
        input_element.get_attribute(name)
        for name in ("aria-label", "name", "id", "data-testid", "data-automation-id")
    ]
    labels = structure.get("labels", [])
    ancestors = structure.get("ancestor_texts", [])
    values = [*attributes]
    if isinstance(labels, list):
        values.extend(labels)
    if isinstance(ancestors, list):
        values.extend(ancestors)
    return _clean(" ".join(str(value or "") for value in values))


def _contains_exact_filename(container_text: str, filename: str | None) -> bool:
    if not filename:
        return False
    pattern = re.compile(
        rf"(?<![\w.-]){re.escape(filename)}(?![\w.-])",
        re.IGNORECASE,
    )
    return pattern.search(container_text) is not None


def _proof(structure: dict[str, object], filename: str | None) -> ResumeContainerProof:
    container_text = _clean(structure.get("container_text"))
    action_text = structure.get("action_text", [])
    actions = _clean(" ".join(str(item) for item in action_text)) if isinstance(action_text, list) else ""
    status_text = structure.get("status_text", [])
    statuses = (
        _clean(" ".join(str(item) for item in status_text))
        if isinstance(status_text, list)
        else ""
    )
    combined = f"{container_text} {actions} {statuses}"
    return ResumeContainerProof(
        container_text_has_filename=_contains_exact_filename(
            container_text,
            filename,
        ),
        has_remove_action=bool(re.search(r"\b(?:remove|delete)\b", actions, re.IGNORECASE)),
        has_replace_action=bool(re.search(r"\b(?:replace|change)\b", actions, re.IGNORECASE)),
        has_acceptance_marker=bool(
            re.search(
                r"\b(?:upload(?:ed)?|complete(?:d)?|success(?:ful(?:ly)?)?|"
                r"accepted|parsed|ready)\b",
                combined,
                re.IGNORECASE,
            )
        ),
        has_upload_error=bool(
            re.search(
                r"\b(?:upload\s+failed|failed\s+to\s+upload|invalid\s+file|"
                r"unsupported\s+file|could\s+not\s+upload|error)\b",
                combined,
                re.IGNORECASE,
            )
        ),
        upload_in_progress=bool(structure.get("busy"))
        or bool(
            re.search(
                r"\b(?:uploading|parsing|processing|scanning)\b",
                combined,
                re.IGNORECASE,
            )
        ),
    )


def _provider_accepted(proof: ResumeContainerProof | None) -> bool:
    return bool(
        proof is not None
        and proof.container_text_has_filename
        and not proof.has_upload_error
        and not proof.upload_in_progress
        and (
            proof.has_acceptance_marker
            or proof.has_remove_action
            or proof.has_replace_action
        )
    )


def provider_acceptance_verified(observation: ResumeUploadObservation) -> bool:
    """Return whether provider-visible evidence proves this upload is accepted."""

    return observation.status == "ready" and _provider_accepted(observation.proof)


def _make_observation(
    *,
    provider: str,
    input_element: FileInput,
    identity: str,
    structure: dict[str, object],
    expected_filename: str | None = None,
) -> ResumeUploadObservation:
    # Container text can gain a filename/remove affordance after upload.  The
    # element identity is the stable structural key; candidate_text is only
    # used to admit the element before mutation.
    key_material = f"{provider}\0{identity}"
    container_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:24]
    filename = structure.get("uploaded_filename")
    size = structure.get("uploaded_size")
    safe_filename = _clean(filename) or None
    safe_size = size if isinstance(size, int) and size >= 0 else None
    proof = _proof(structure, safe_filename or expected_filename)
    if safe_filename is None and expected_filename and _provider_accepted(proof):
        safe_filename = expected_filename
    return ResumeUploadObservation(
        status="ready",
        provider=provider,
        reason="exact_resume_container",
        container_key=container_key,
        file_input_identity=identity,
        uploaded_filename=safe_filename,
        uploaded_size=safe_size,
        proof=proof,
        _input=input_element,
    )


def observe_resume_upload(
    surface: ApplicationSurface,
    provider: str,
    *,
    expected: LocalPdfArtifact | None = None,
) -> ResumeUploadObservation:
    """Select exactly one safe Resume/CV file input without mutating the page."""

    normalized_provider = provider.strip().lower()
    if not provider_supports(normalized_provider, "semantic_upload"):
        return ResumeUploadObservation(
            status="unsupported",
            provider=normalized_provider,
            reason="unsupported_provider",
        )
    typed_provider: Provider = normalized_provider  # type: ignore[assignment]
    candidates: list[tuple[FileInput, str, str, dict[str, object]]] = []
    for input_element in surface.query_selector_all('input[type="file"]'):
        structure = _safe_structure(input_element)
        text = _candidate_text(input_element, structure)
        if not _POSITIVE_PATTERN.search(text) or _NEGATIVE_PATTERN.search(text):
            continue
        if _truthy_attribute(input_element.get_attribute("disabled")):
            continue
        if _truthy_attribute(input_element.get_attribute("readonly")):
            continue
        if not _pdf_accepted(input_element.get_attribute("accept")):
            continue
        if typed_provider == "smartrecruiters" and not (
            _truthy_attribute(input_element.get_attribute("required"))
            or _truthy_attribute(input_element.get_attribute("aria-required"))
        ):
            continue
        identity = _identity(input_element)
        if identity is None:
            continue
        candidates.append((input_element, identity, text, structure))

    if not candidates:
        return ResumeUploadObservation(
            status="unsupported",
            provider=typed_provider,
            reason="no_exact_compatible_resume_input",
        )
    if len(candidates) > 1:
        return ResumeUploadObservation(
            status="manual",
            provider=typed_provider,
            reason="ambiguous_resume_inputs",
        )
    input_element, identity, text, structure = candidates[0]
    return _make_observation(
        provider=typed_provider,
        input_element=input_element,
        identity=identity,
        structure=structure,
        expected_filename=expected.filename if expected is not None else None,
    )


def upload_resume(
    observation: ResumeUploadObservation,
    artifact: LocalPdfArtifact,
) -> ResumeUploadResult:
    """Upload once to the exact observed input, then verify the same container."""

    if observation.status != "ready" or observation._input is None:
        return ResumeUploadResult(observation.status, observation.reason, observation, 0)
    path = artifact.path.resolve()
    if path.suffix.casefold() != ".pdf" or artifact.filename != path.name:
        return ResumeUploadResult("unsupported", "artifact_not_exact_pdf", observation, 0)
    try:
        actual_size = path.stat().st_size
    except OSError:
        return ResumeUploadResult("failed", "artifact_unavailable", observation, 0)
    if artifact.size_bytes < 0 or actual_size != artifact.size_bytes:
        return ResumeUploadResult("failed", "artifact_metadata_mismatch", observation, 0)

    input_element = observation._input
    identity = _identity(input_element)
    if identity != observation.file_input_identity:
        return ResumeUploadResult("manual", "file_input_identity_changed", observation, 0)
    if _truthy_attribute(input_element.get_attribute("disabled")):
        return ResumeUploadResult("manual", "file_input_became_disabled", observation, 0)
    if not _pdf_accepted(input_element.get_attribute("accept")):
        return ResumeUploadResult("manual", "file_input_accept_changed", observation, 0)

    before_structure = _safe_structure(input_element)
    before_proof = _proof(before_structure, artifact.filename)
    if _provider_accepted(before_proof):
        refreshed = _make_observation(
            provider=observation.provider,
            input_element=input_element,
            identity=identity,
            structure=before_structure,
            expected_filename=artifact.filename,
        )
        return ResumeUploadResult(
            "uploaded",
            "existing_provider_acceptance_verified",
            refreshed,
            0,
        )
    before_filename = _clean(before_structure.get("uploaded_filename"))
    if (
        before_filename
        or before_proof.upload_in_progress
        or before_proof.has_upload_error
    ):
        unresolved = _make_observation(
            provider=observation.provider,
            input_element=input_element,
            identity=identity,
            structure=before_structure,
            expected_filename=artifact.filename,
        )
        return ResumeUploadResult(
            "manual",
            "existing_upload_state_unresolved",
            unresolved,
            0,
        )

    try:
        input_element.set_input_files(str(path))
    except (OSError, PlaywrightError):
        return ResumeUploadResult("failed", "set_input_files_failed", observation, 1)

    stable_observation: ResumeUploadObservation | None = None
    stable_count = 0
    for attempt in range(20):
        structure = _safe_structure(input_element)
        refreshed = _make_observation(
            provider=observation.provider,
            input_element=input_element,
            identity=identity,
            structure=structure,
            expected_filename=artifact.filename,
        )
        if refreshed.container_key != observation.container_key:
            return ResumeUploadResult(
                "failed", "container_identity_changed_after_upload", refreshed, 1
            )
        proof = refreshed.proof
        if proof is not None and proof.has_upload_error:
            return ResumeUploadResult(
                "failed", "provider_upload_error_visible", refreshed, 1
            )
        metadata_matches = (
            refreshed.uploaded_filename == artifact.filename
            and (
                refreshed.uploaded_size is None
                or refreshed.uploaded_size == artifact.size_bytes
            )
        )
        accepted = metadata_matches and _provider_accepted(proof)
        if accepted:
            if refreshed == stable_observation:
                stable_count += 1
            else:
                stable_observation = refreshed
                stable_count = 1
            if stable_count >= 3:
                return ResumeUploadResult(
                    "uploaded",
                    "provider_acceptance_stable",
                    refreshed,
                    1,
                )
        else:
            stable_observation = None
            stable_count = 0
        if attempt < 19:
            time.sleep(0.05)
    final = stable_observation or _make_observation(
        provider=observation.provider,
        input_element=input_element,
        identity=identity,
        structure=_safe_structure(input_element),
        expected_filename=artifact.filename,
    )
    return ResumeUploadResult(
        "failed", "provider_acceptance_unverified", final, 1
    )
