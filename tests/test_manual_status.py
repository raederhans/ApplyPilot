"""Regression tests for manual application status updates."""

import sqlite3

import pytest
from typer.testing import CliRunner

from applypilot import cli
from applypilot.apply import launcher


@pytest.fixture
def jobs_db(monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    """Provide the job fields used by the current manual status contract."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY,
            application_url TEXT,
            applied_at TEXT,
            apply_status TEXT,
            apply_error TEXT,
            apply_attempts INTEGER DEFAULT 0,
            apply_retry_blocked INTEGER DEFAULT 0,
            apply_retry_reason TEXT,
            agent_id TEXT,
            verification_confidence TEXT,
            application_evidence TEXT,
            application_recorded_at TEXT
        )
        """
    )
    monkeypatch.setattr(launcher, "get_connection", lambda: connection)
    yield connection
    connection.close()


def _insert_job(
    connection: sqlite3.Connection,
    url: str,
    application_url: str,
) -> None:
    connection.execute(
        "INSERT INTO jobs (url, application_url) VALUES (?, ?)",
        (url, application_url),
    )
    connection.commit()


def test_mark_job_accepts_canonical_url_and_preserves_applied_contract(
    jobs_db: sqlite3.Connection,
) -> None:
    canonical_url = "https://board.example/jobs/123"
    _insert_job(jobs_db, canonical_url, "https://ats.example/apply/123")

    updated_url = launcher.mark_job(canonical_url, "applied")

    row = jobs_db.execute(
        "SELECT * FROM jobs WHERE url = ?", (canonical_url,)
    ).fetchone()
    assert updated_url == canonical_url
    assert row["apply_status"] == "applied"
    assert row["applied_at"] is not None
    assert row["apply_retry_blocked"] == 0
    assert row["apply_retry_reason"] is None
    assert row["verification_confidence"] == "manual_visual_confirmation"
    assert row["application_evidence"] == "manually_marked_applied"
    assert row["application_recorded_at"] is not None


def test_mark_job_resolves_unique_application_url_and_preserves_failed_contract(
    jobs_db: sqlite3.Connection,
) -> None:
    canonical_url = "https://board.example/jobs/123"
    application_url = "https://ats.example/apply/123"
    _insert_job(jobs_db, canonical_url, application_url)

    updated_url = launcher.mark_job(application_url, "failed", reason="manual email")

    row = jobs_db.execute(
        "SELECT * FROM jobs WHERE url = ?", (canonical_url,)
    ).fetchone()
    assert updated_url == canonical_url
    assert row["apply_status"] == "failed"
    assert row["apply_error"] == "manual email"
    assert row["apply_attempts"] == 0
    assert row["apply_retry_blocked"] == 1
    assert row["apply_retry_reason"] == "manual email"


def test_mark_job_rejects_unknown_url(jobs_db: sqlite3.Connection) -> None:
    with pytest.raises(LookupError, match="No job found"):
        launcher.mark_job("https://ats.example/apply/missing", "applied")


def test_mark_job_rejects_ambiguous_application_url(
    jobs_db: sqlite3.Connection,
) -> None:
    application_url = "https://ats.example/apply/shared"
    _insert_job(jobs_db, "https://board.example/jobs/123", application_url)
    _insert_job(jobs_db, "https://board.example/jobs/456", application_url)

    with pytest.raises(ValueError, match="matches multiple jobs"):
        launcher.mark_job(application_url, "applied")

    rows = jobs_db.execute("SELECT apply_status FROM jobs ORDER BY url").fetchall()
    assert [row["apply_status"] for row in rows] == [None, None]


def test_mark_job_rejects_invalid_status(jobs_db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="status must be"):
        launcher.mark_job("https://board.example/jobs/123", "previewed")


@pytest.mark.parametrize(
    ("option", "message"),
    [
        ("--mark-applied", "Could not mark job as applied"),
        ("--mark-failed", "Could not mark job as failed"),
    ],
)
def test_manual_status_cli_exits_when_no_job_matches(
    monkeypatch: pytest.MonkeyPatch,
    option: str,
    message: str,
) -> None:
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)

    def missing_job(*_args: object, **_kwargs: object) -> str:
        raise LookupError("No job found for URL: https://example.invalid/missing")

    monkeypatch.setattr(launcher, "mark_job", missing_job)

    result = CliRunner().invoke(
        cli.app,
        ["apply", option, "https://example.invalid/missing"],
    )

    assert result.exit_code == 1
    assert message in result.output
    assert "Marked as applied" not in result.output
    assert "Marked as failed" not in result.output
