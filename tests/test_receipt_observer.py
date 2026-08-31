from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from applypilot.apply.receipt_observer import (
    mailbox_watermark,
    process_receipt_observation,
    receipt_observer_context,
)
from applypilot.database import init_db


def job() -> dict[str, object]:
    return {
        "url": "https://jobs.example.test/data-intern",
        "company_name": "Example Data Pte. Ltd.",
        "title": "Data Engineer Intern",
        "platform_job_id": "JOB-123",
    }


def connection(path: Path):
    conn = init_db(path)
    current = job()
    conn.execute(
        "INSERT INTO jobs (url, company_name, title, platform_job_id, apply_status, "
        "apply_retry_blocked, apply_retry_reason) VALUES (?, ?, ?, ?, "
        "'submission_uncertain', 1, 'submission_uncertain_requires_review')",
        (
            current["url"],
            current["company_name"],
            current["title"],
            current["platform_job_id"],
        ),
    )
    conn.commit()
    return conn


def matching_observation(provider: str, received_at: datetime) -> dict[str, object]:
    return {
        "receipt_scan": {
            "provider": provider,
            "scan_succeeded": True,
            "ambiguous": False,
            "candidate_count": 1,
            "max_received_at": received_at.isoformat(),
            "max_message_id": "message-1",
        },
        "confirmation_receipt": {
            "provider": provider,
            "provider_message_id": "message-1",
            "received_at": received_at.isoformat(),
            "sender_domain": "notifications.example-ats.com",
            "company_name": "Example Data",
            "job_title": "Data Engineer Internship",
            "platform_job_id": "JOB-123",
            "confirmation_text": "We have received your application.",
            "exact_job_identity_matched": True,
        },
    }


def test_gmail_confirmation_reconciles_exact_job_and_advances_watermark(
    tmp_path: Path,
) -> None:
    conn = connection(tmp_path / "gmail.db")
    submitted_at = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)

    result = process_receipt_observation(
        conn,
        job(),
        provider="gmail",
        submitted_at=submitted_at,
        observation=matching_observation(
            "gmail", submitted_at + timedelta(minutes=1)
        ),
    )

    assert result["status"] == "applied"
    assert result["watermark_advanced"] is True
    assert mailbox_watermark(conn, "gmail") == {
        "received_at": "2026-08-31T10:01:00+00:00",
        "message_id": "message-1",
    }
    stored = conn.execute(
        "SELECT apply_status, application_evidence FROM jobs WHERE url=?",
        (job()["url"],),
    ).fetchone()
    assert stored["apply_status"] == "applied"
    assert stored["application_evidence"] == "confirmation_email:gmail:message-1"


def test_outlook_has_an_independent_incremental_watermark(tmp_path: Path) -> None:
    conn = connection(tmp_path / "outlook.db")
    submitted_at = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    observation = {
        "receipt_scan": {
            "provider": "outlook",
            "scan_succeeded": True,
            "ambiguous": False,
            "candidate_count": 0,
            "max_received_at": (submitted_at + timedelta(minutes=2)).isoformat(),
            "max_message_id": "outlook-max-1",
        },
        "confirmation_receipt": None,
    }

    result = process_receipt_observation(
        conn,
        job(),
        provider="outlook",
        submitted_at=submitted_at,
        observation=observation,
    )

    assert result == {
        "status": "no_match",
        "provider": "outlook",
        "watermark_advanced": True,
    }
    assert mailbox_watermark(conn, "outlook")["message_id"] == "outlook-max-1"
    assert mailbox_watermark(conn, "gmail") is None


def test_provider_failure_never_advances_its_watermark(tmp_path: Path) -> None:
    conn = connection(tmp_path / "provider-error.db")
    submitted_at = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)

    result = process_receipt_observation(
        conn,
        job(),
        provider="gmail",
        submitted_at=submitted_at,
        observation={
            "receipt_scan": {
                "provider": "gmail",
                "scan_succeeded": False,
                "candidate_count": 0,
                "max_received_at": (submitted_at + timedelta(minutes=3)).isoformat(),
                "max_message_id": "must-not-advance",
            }
        },
    )

    assert result["status"] == "provider_error"
    assert result["watermark_advanced"] is False
    assert mailbox_watermark(conn, "gmail") is None


def test_ambiguous_or_pre_submit_receipt_never_reconciles_or_advances(
    tmp_path: Path,
) -> None:
    conn = connection(tmp_path / "ambiguous.db")
    submitted_at = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    ambiguous = matching_observation(
        "gmail", submitted_at + timedelta(minutes=1)
    )
    ambiguous["receipt_scan"]["ambiguous"] = True

    result = process_receipt_observation(
        conn,
        job(),
        provider="gmail",
        submitted_at=submitted_at,
        observation=ambiguous,
    )
    assert result["status"] == "ambiguous"
    assert mailbox_watermark(conn, "gmail") is None

    old = matching_observation("gmail", submitted_at - timedelta(seconds=1))
    result = process_receipt_observation(
        conn,
        job(),
        provider="gmail",
        submitted_at=submitted_at,
        observation=old,
    )
    assert result["status"] == "ambiguous"
    assert mailbox_watermark(conn, "gmail") is None
    assert conn.execute(
        "SELECT apply_status FROM jobs WHERE url=?", (job()["url"],)
    ).fetchone()[0] == "submission_uncertain"


def test_receipt_context_carries_provider_specific_watermark_only(
    tmp_path: Path,
) -> None:
    conn = connection(tmp_path / "context.db")
    submitted_at = datetime(2026, 8, 31, 10, 0, tzinfo=UTC)
    process_receipt_observation(
        conn,
        job(),
        provider="outlook",
        submitted_at=submitted_at,
        observation={
            "receipt_scan": {
                "provider": "outlook",
                "scan_succeeded": True,
                "ambiguous": False,
                "candidate_count": 0,
                "max_received_at": (submitted_at + timedelta(minutes=1)).isoformat(),
                "max_message_id": "outlook-1",
            },
            "confirmation_receipt": None,
        },
    )

    gmail = receipt_observer_context(
        conn, job(), provider="gmail", submitted_at=submitted_at
    )
    outlook = receipt_observer_context(
        conn, job(), provider="outlook", submitted_at=submitted_at
    )
    assert gmail["watermark"] is None
    assert gmail["search_after"] == submitted_at.isoformat()
    assert outlook["watermark"]["message_id"] == "outlook-1"
    assert outlook["search_after"] == submitted_at.isoformat()


def test_watermark_compares_offset_timestamps_by_instant(tmp_path: Path) -> None:
    conn = connection(tmp_path / "watermark-offset.db")
    submitted_at = datetime.fromisoformat("2026-08-31T09:00:00+00:00")

    first = process_receipt_observation(
        conn,
        job(),
        provider="gmail",
        submitted_at=submitted_at,
        observation={
            "receipt_scan": {
                "provider": "gmail",
                "scan_succeeded": True,
                "ambiguous": False,
                "candidate_count": 0,
                "max_received_at": "2026-08-31T18:00:00+08:00",
                "max_message_id": "newer",
            },
            "confirmation_receipt": None,
        },
    )
    second = process_receipt_observation(
        conn,
        job(),
        provider="gmail",
        submitted_at=submitted_at,
        observation={
            "receipt_scan": {
                "provider": "gmail",
                "scan_succeeded": True,
                "ambiguous": False,
                "candidate_count": 0,
                "max_received_at": "2026-08-31T10:30:00+01:00",
                "max_message_id": "older",
            },
            "confirmation_receipt": None,
        },
    )

    assert first["watermark_advanced"] is True
    assert second["watermark_advanced"] is False
    assert mailbox_watermark(conn, "gmail") == {
        "received_at": "2026-08-31T10:00:00+00:00",
        "message_id": "newer",
    }


def test_newer_provider_watermark_never_excludes_exact_job_window(
    tmp_path: Path,
) -> None:
    conn = connection(tmp_path / "job-window.db")
    first_submitted = datetime.fromisoformat("2026-08-31T10:00:00+00:00")
    process_receipt_observation(
        conn,
        job(),
        provider="gmail",
        submitted_at=first_submitted,
        observation={
            "receipt_scan": {
                "provider": "gmail",
                "scan_succeeded": True,
                "ambiguous": False,
                "candidate_count": 0,
                "max_received_at": "2026-08-31T10:05:00+00:00",
                "max_message_id": "other-job-latest",
            },
            "confirmation_receipt": None,
        },
    )
    this_job_submitted = datetime.fromisoformat("2026-08-31T10:02:00+00:00")

    context = receipt_observer_context(
        conn,
        job(),
        provider="gmail",
        submitted_at=this_job_submitted,
    )

    assert context["watermark"]["received_at"] == "2026-08-31T10:05:00+00:00"
    assert context["search_after"] == "2026-08-31T10:02:00+00:00"
