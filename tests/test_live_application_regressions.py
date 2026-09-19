"""Offline regressions for the September multi-platform application run."""

import io
import json

import pytest
from rich.console import Console
from typer.testing import CliRunner

from applypilot import cli, single_job
from applypilot.database import init_db, reconcile_submission_receipt


@pytest.mark.parametrize("encoding", ["gbk", "ascii", "utf-8"])
def test_import_commits_and_reports_unicode_losslessly(tmp_path, monkeypatch, encoding):
    conn = init_db(tmp_path / "import.db")
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(single_job, "get_connection", lambda: conn)
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding=encoding, errors="strict")
    monkeypatch.setattr(cli, "console", Console(file=stream, color_system=None, width=200))
    result = CliRunner().invoke(cli.app, [
        "import-job", "--url", "https://example.test/ai-intern",
        "--company", "Dräger 智能 🤖", "--title", "AI Intern",
    ])
    stream.flush()
    assert result.exit_code == 0, result.exception
    output = json.loads(raw.getvalue().decode(encoding))
    assert "Dräger 智能 🤖" in json.dumps(output, ensure_ascii=False)
    assert conn.execute("SELECT company_name FROM jobs").fetchone()[0] == "Dräger 智能 🤖"


@pytest.mark.parametrize("text,accepted", [
    ("Your application was sent to Dräger!", True),
    ("Your application has been successfully sent to Dräger.", True),
    ("Your application was not sent to Dräger!", False),
    ("Your application will be sent to Dräger!", False),
    ("Your application was sent to Dräger?", False),
    ("If your application was sent to Dräger, check your email.", False),
    ('Look for "Your application was sent to Dräger!"', False),
    ("Your application was sent to Dräger after you click Submit.", False),
    ("Your application was sent to Another Company!", False),
    ("Submission Successful", True),
    ("Submission successful!", True),
    ("Submission unsuccessful", False),
    ("Submission Successful?", False),
    ('Look for "Submission Successful"', False),
    ("Submission Successful after you click Submit", False),
    ("Your application has been sent. Thank you!", True),
    ("Application sent successfully!", True),
    ("Your application was sent.\nThank you.", True),
    ("Your application has been not sent. Thank you!", False),
    ("Your application has been sent?", False),
    ("Your application will be sent. Thank you!", False),
    ("If your application has been sent. Thank you!", False),
    ('Look for "Your application has been sent. Thank you!"', False),
    ("Your application has been sent after you click Submit.", False),
])
def test_linkedin_completed_sent_receipt(tmp_path, text, accepted):
    conn = init_db(tmp_path / "receipt.db")
    url = "https://example.test/ai-intern"
    conn.execute("INSERT INTO jobs (url, title, company_name) VALUES (?, ?, ?)",
                 (url, "AI Intern", "Dräger"))
    conn.commit()
    evidence = {
        "job_url": url, "source": "browser_receipt", "receipt_id": "test-receipt",
        "company_name": "Dräger", "job_title": "AI Intern", "confirmation_text": text,
    }
    result = reconcile_submission_receipt(evidence, conn)
    assert result["status"] == ("applied" if accepted else "rejected")
    assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] == (
        "applied" if accepted else None
    )
    if accepted:
        assert reconcile_submission_receipt(evidence, conn)["changed"] is False
        assert reconcile_submission_receipt({**evidence, "job_title": "Sales Manager"}, conn)[
            "reason"
        ] == "job_title_mismatch"


def test_generic_success_heading_does_not_admit_email(tmp_path):
    conn = init_db(tmp_path / "email.db")
    url = "https://example.test/ai-intern"
    conn.execute("INSERT INTO jobs (url, title, company_name) VALUES (?, ?, ?)",
                 (url, "AI Intern", "Example"))
    conn.commit()
    result = reconcile_submission_receipt({
        "job_url": url, "source": "confirmation_email", "receipt_id": "email-1",
        "company_name": "Example", "job_title": "AI Intern",
        "confirmation_text": "Submission Successful",
    }, conn)
    assert result["reason"] == "no_decisive_submission_signal"
    assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] is None
