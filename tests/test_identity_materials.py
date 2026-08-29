from __future__ import annotations

from applypilot.apply.identity_materials import (
    classify_identity_requirement,
    identity_requirement_is_satisfied,
)
from applypilot.apply.page_observation import _validate_pre_submit_snapshot


def _snapshot(**updates):
    value = {
        "url": "https://jobs.example/apply/1",
        "submit_control_count": 1,
        "required_unfilled": [],
        "sensitive_required_unknown": [],
        "file_fields": [],
        "select_fields": [],
        "radio_questions": [],
        "text_fields": [],
    }
    value.update(updates)
    return value


def _job(**updates):
    value = {
        "url": "https://jobs.example/apply/1",
        "application_url": "https://jobs.example/apply/1",
    }
    value.update(updates)
    return value


def test_identity_requirements_are_classified_instead_of_blanket_blocked() -> None:
    ordinary = classify_identity_requirement("Country of citizenship")
    identifier = classify_identity_requirement("Passport number")
    document = classify_identity_requirement("Upload passport", field_type="file")
    biometric = classify_identity_requirement("Record a video identity verification")

    assert ordinary.kind == "ordinary_fact"
    assert ordinary.requires_human is False
    assert identifier.kind == "protected_identifier"
    assert identifier.requires_secure_source is True
    assert document.kind == "document_artifact"
    assert document.requires_verified_artifact is True
    assert biometric.kind == "biometric_or_media"
    assert biometric.requires_human is True
    assert identity_requirement_is_satisfied(
        ordinary,
        value_present=True,
        confirmed_source=True,
    )
    assert not identity_requirement_is_satisfied(
        identifier,
        value_present=True,
        confirmed_source=False,
    )


def test_missing_ordinary_identity_fact_is_repairable_not_blanket_manual() -> None:
    issues = _validate_pre_submit_snapshot(
        _snapshot(sensitive_required_unknown=["Work authorization status"]),
        {},
        _job(),
    )

    assert "required_field_empty:confirmed_identity_fact:Work authorization status" in issues
    assert not any(item.startswith("sensitive_required_unknown:") for item in issues)


def test_identity_document_requires_matching_verified_authorization() -> None:
    file_field = {
        "text": "Upload passport or national ID",
        "required": True,
        "count": 0,
    }
    unapproved = _validate_pre_submit_snapshot(
        _snapshot(file_fields=[file_field]),
        {},
        _job(),
    )
    approved = _validate_pre_submit_snapshot(
        _snapshot(file_fields=[file_field]),
        {},
        _job(
            _authorized_identity_materials=[
                {
                    "field_label": "Upload passport or national ID",
                    "verified": True,
                    "explicitly_authorized": True,
                }
            ]
        ),
    )

    assert any(item.startswith("identity_material_missing:") for item in unapproved)
    assert any(
        item.startswith("required_field_empty:authorized_identity_material:")
        for item in approved
    )
