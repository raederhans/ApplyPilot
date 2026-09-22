import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from applypilot.apply import attended_application as attended
from applypilot.apply.attended_plan import build_attended_plan
from applypilot.apply.authorization import build_bound_manifest
from applypilot.database import init_db
from applypilot.storage import application_ledger as ledger


def test_attended_plan_cli_reads_existing_fixture(env):
    from typer.testing import CliRunner

    from applypilot.cli import app

    conn, _run, base, _pdf, _profile = env
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    result = CliRunner().invoke(app, ["attended-plan", "--db", database, "--attempt-id", base["attempt_id"]])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["attempt_id"] == base["attempt_id"]
    assert payload["submit_authority"] is False
    assert payload["next_action"] == "host_review_then_existing_gate"


@pytest.fixture
def env(tmp_path):
    conn = init_db(tmp_path / "jobs.db")
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 confidential candidate resume text")
    job = {
        "url": "https://boards.greenhouse.io/example/jobs/123",
        "application_url": "https://boards.greenhouse.io/example/jobs/123",
        "company_name": "Example",
        "title": "Engineer",
        "full_description": "Secret job description",
        "fit_score": 8,
        "tailor_status": "machine_validated",
        "tailored_resume_path": str(pdf),
        "cover_letter_status": "not_required",
        "eligibility_status": "eligible",
    }
    conn.execute(
        "INSERT INTO jobs (" + ",".join(job) + ") VALUES (" + ",".join("?" for _ in job) + ")",
        tuple(job.values()),
    )
    conn.commit()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(build_bound_manifest([job])))
    profile = {"submission_policy": {"allow_runtime_readiness_review": True}}
    base = {"job_url": job["url"], "host_session_id": "host-1", "tab_id": "tab-1"}

    def run(action, **extra):
        return attended.execute(conn, dict(base, action=action, **extra), profile)

    state = run("begin", manifest_path=str(manifest))
    base["attempt_id"] = state["attempt_id"]
    yield conn, run, base, pdf, profile
    conn.close()


def observation():
    return {
        "source": "attending_host",
        "observed_at": datetime.now(UTC).isoformat(),
        "evidence_refs": ["fixture-capture-1"],
    }


def snapshot():
    return {
        "url": "https://boards.greenhouse.io/example/jobs/123",
        "required_unfilled": [],
        "sensitive_required_unknown": [],
        "file_fields": [],
        "form_fields": [],
        "text_fields": [],
        "select_fields": [],
        "full_name_values": [],
        "email_values": [],
        "captcha_visible": False,
        "verification_visible": False,
        "assessment_visible": False,
        "resume_field_present": True,
        "resume_uploaded": True,
        "submit_control_count": 1,
    }


def test_build_attended_plan_prepare_contract(env):
    conn, _, base, _, _ = env
    attempt_id = base["attempt_id"]

    plan = build_attended_plan(conn, attempt_id)

    assert plan["schema_version"] == "1"
    assert plan["attempt_id"] == attempt_id
    assert plan["job_url"] == base["job_url"]
    assert plan["host_session_id"] == "host-1"
    assert plan["tab_id"] == "tab-1"

    # Durable phase and status
    assert plan["phase"] == "prepare"
    assert plan["status"] == "in_progress"
    assert plan["submit_started"] is False

    # Review state: prepare requires host review; checkpoint is not present yet
    assert plan["needs_host_review"] is True
    assert plan["checkpoint_present"] is False
    assert plan["checkpoint_observed_at"] is None
    assert plan["next_action"] == "host_review_then_existing_gate"

    assert plan["submitted"] is False
    assert plan["submit_authority"] is False
    assert plan["receipt_ref"] is None
    assert plan["blockers"] == []

    # Materials: refs and digests present without raw content or validation text
    assert len(plan["material_refs"]) > 0
    resume_ref = next(m for m in plan["material_refs"] if m["purpose"] == "resume")
    assert resume_ref["content_sha256"]
    assert resume_ref["ref"].startswith("sha256:")
    assert "validation" not in resume_ref
    assert plan["material_digests"]["resume"] == resume_ref["content_sha256"]


def test_build_attended_plan_checkpoint_and_claim_contract(env):
    conn, run, base, _, _ = env
    attempt_id = base["attempt_id"]

    obs = observation()
    run("checkpoint", snapshot=snapshot(), **obs)

    plan_chk = build_attended_plan(conn, attempt_id)
    assert plan_chk["phase"] == "prepared"
    assert plan_chk["checkpoint_present"] is True
    assert plan_chk["checkpoint_observed_at"] == obs["observed_at"]
    assert plan_chk["checkpoint_digest"]
    assert "fixture-capture-1" in plan_chk["evidence_refs"]
    assert plan_chk["needs_host_review"] is True
    assert plan_chk["next_action"] == "host_review_then_existing_gate"
    assert plan_chk["submitted"] is False

    run("claim", snapshot=snapshot())
    plan_claimed = build_attended_plan(conn, attempt_id)
    assert plan_claimed["phase"] == "claimed"
    assert plan_claimed["needs_host_review"] is True
    assert plan_claimed["next_action"] == "host_review_then_existing_gate"
    assert plan_claimed["submitted"] is False


def test_build_attended_plan_blockers_contract(env):
    conn, run, base, pdf, _ = env
    attempt_id = base["attempt_id"]

    # 1. material_changed
    run("checkpoint", snapshot=snapshot(), **observation())
    pdf.write_bytes(b"modified resume content")
    result = run("resume")
    assert result["phase"] == "material_changed"

    plan_mat = build_attended_plan(conn, attempt_id)
    assert "material_changed" in plan_mat["blockers"]
    assert plan_mat["needs_host_review"] is True

    # 2. lease_expired
    conn.execute(
        "UPDATE application_attempts SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE attempt_id=?",
        (attempt_id,),
    )
    conn.commit()
    plan_lease = build_attended_plan(conn, attempt_id)
    assert "lease_expired" in plan_lease["blockers"]

    # 3. open risk event
    future = (datetime.now(UTC) + timedelta(minutes=45)).isoformat()
    conn.execute(
        "UPDATE application_attempts SET lease_expires_at=? WHERE attempt_id=?",
        (future, attempt_id),
    )
    ledger.record_risk_event(conn, base["job_url"], "captcha_challenge", "high", attempt_id=attempt_id)
    conn.commit()
    plan_risk = build_attended_plan(conn, attempt_id)
    assert "risk:captcha_challenge" in plan_risk["blockers"]

    # 4. job retry blocked
    conn.execute(
        "UPDATE jobs SET apply_retry_blocked=1, apply_retry_reason='manual_hold' WHERE url=?",
        (base["job_url"],),
    )
    conn.commit()
    plan_retry = build_attended_plan(conn, attempt_id)
    assert "job_retry_blocked" in plan_retry["blockers"]


def test_build_attended_plan_submission_uncertain_contract(env):
    conn, run, base, _, _ = env
    attempt_id = base["attempt_id"]

    run("checkpoint", snapshot=snapshot(), **observation())
    claimed = run("claim", snapshot=snapshot())

    started = run(
        "submit-intent",
        snapshot=snapshot(),
        checkpoint_digest=claimed["checkpoint_digest"],
        **observation(),
    )
    assert started["submit_started"]

    failed = run("fail", reason="network timeout waiting for receipt")
    assert failed["phase"] == "submission_uncertain"

    plan = build_attended_plan(conn, attempt_id)
    assert plan["phase"] == "submission_uncertain"
    assert plan["status"] == "submission_uncertain"
    assert plan["submit_started"] is True
    assert "submission_uncertain" in plan["blockers"]
    assert plan["needs_host_review"] is True
    assert plan["next_action"] == "reconcile_receipt_only"
    assert plan["submitted"] is False
    assert plan["receipt_ref"] is None


def test_build_attended_plan_receipt_contract(env):
    conn, run, base, _, _ = env
    attempt_id = base["attempt_id"]

    run("checkpoint", snapshot=snapshot(), **observation())
    claimed = run("claim", snapshot=snapshot())
    run(
        "submit-intent",
        snapshot=snapshot(),
        checkpoint_digest=claimed["checkpoint_digest"],
        **observation(),
    )

    receipt = {
        "source": "browser_receipt",
        "receipt_id": "receipt-verified-123",
        "company_name": "Example",
        "job_title": "Engineer",
        "confirmation_text": "Your application has been submitted",
    }
    obs = observation()
    obs["evidence_refs"] = ["obs-receipt-1"]
    result = run("receipt", receipt=receipt, **obs)
    assert result["status"] == "applied"

    plan = build_attended_plan(conn, attempt_id)
    assert plan["phase"] == "applied"
    assert plan["status"] == "applied"
    assert plan["submitted"] is True
    assert plan["receipt_ref"] == "browser_receipt:receipt-verified-123"
    assert plan["needs_host_review"] is False
    assert plan["next_action"] == "terminal"
    assert plan["submit_authority"] is False
    assert "obs-receipt-1" in plan["evidence_refs"]


def test_build_attended_plan_missing_receipt_applied_state(env):
    conn, _, base, _, _ = env
    attempt_id = base["attempt_id"]

    # Manually set phase='applied' in attended_applications and status='applied' in application_attempts
    # without inserting an exact receipt joined to application_receipts
    payload = json.loads(
        conn.execute("SELECT payload FROM attended_applications WHERE attempt_id=?", (attempt_id,)).fetchone()[0]
    )
    payload["phase"] = "applied"
    conn.execute("UPDATE attended_applications SET payload=? WHERE attempt_id=?", (json.dumps(payload), attempt_id))
    conn.execute("UPDATE application_attempts SET phase='applied', status='applied' WHERE attempt_id=?", (attempt_id,))
    conn.commit()

    plan = build_attended_plan(conn, attempt_id)
    # State phase applied alone is insufficient: without exact admitted receipt binding, submitted is False
    assert plan["submitted"] is False
    assert plan["receipt_ref"] is None
    assert plan["needs_host_review"] is True


def test_build_attended_plan_exact_binding_mismatch(env):
    conn, _, base, _, _ = env
    attempt_id = base["attempt_id"]

    raw = conn.execute("SELECT payload FROM attended_applications WHERE attempt_id=?", (attempt_id,)).fetchone()[0]

    # Subcase A: payload attempt_id mismatch
    p_bad_attempt = json.loads(raw)
    p_bad_attempt["attempt_id"] = "attempt-other"
    conn.execute("UPDATE attended_applications SET payload=? WHERE attempt_id=?", (json.dumps(p_bad_attempt), attempt_id))
    conn.commit()
    with pytest.raises(ValueError, match="attempt_id mismatch"):
        build_attended_plan(conn, attempt_id)

    # Subcase B: payload job_url mismatch
    p_bad_url = json.loads(raw)
    p_bad_url["job_url"] = "https://other.example/jobs/999"
    conn.execute("UPDATE attended_applications SET payload=? WHERE attempt_id=?", (json.dumps(p_bad_url), attempt_id))
    conn.commit()
    with pytest.raises(ValueError, match="job_url mismatch"):
        build_attended_plan(conn, attempt_id)

    # Subcase C: payload batch_id mismatch
    p_bad_batch = json.loads(raw)
    p_bad_batch["batch_id"] = "batch-different"
    conn.execute("UPDATE attended_applications SET payload=? WHERE attempt_id=?", (json.dumps(p_bad_batch), attempt_id))
    conn.commit()
    with pytest.raises(ValueError, match="batch_id mismatch"):
        build_attended_plan(conn, attempt_id)

    # Subcase D: empty host_session_id or tab_id
    p_bad_host = json.loads(raw)
    p_bad_host["host_session_id"] = "   "
    conn.execute("UPDATE attended_applications SET payload=? WHERE attempt_id=?", (json.dumps(p_bad_host), attempt_id))
    conn.commit()
    with pytest.raises(ValueError, match="host_session_id must be non-empty string"):
        build_attended_plan(conn, attempt_id)

    p_bad_tab = json.loads(raw)
    p_bad_tab["tab_id"] = ""
    conn.execute("UPDATE attended_applications SET payload=? WHERE attempt_id=?", (json.dumps(p_bad_tab), attempt_id))
    conn.commit()
    with pytest.raises(ValueError, match="tab_id must be non-empty string"):
        build_attended_plan(conn, attempt_id)

    # Subcase E: missing / empty attempt_id
    with pytest.raises(ValueError, match="attempt_id is required"):
        build_attended_plan(conn, "")
    with pytest.raises(ValueError, match="attempt_id is required"):
        build_attended_plan(conn, None)  # type: ignore[arg-type]

    # Subcase F: attempt not found
    with pytest.raises(ValueError, match="attended attempt not found"):
        build_attended_plan(conn, "attempt-nonexistent-999")


def test_build_attended_plan_read_only_authorizer_and_no_changes(env):
    conn, _, base, _, _ = env
    attempt_id = base["attempt_id"]

    # Authorizer to strictly forbid any write operations
    def authorizer(action, _arg1, _arg2, _dbname, _source):
        if action in (
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_ALTER_TABLE,
            sqlite3.SQLITE_DROP_TABLE,
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    changes_before = conn.total_changes

    plan = build_attended_plan(conn, attempt_id)
    assert isinstance(plan, dict)

    assert conn.total_changes == changes_before
    assert not conn.in_transaction
    conn.set_authorizer(None)


def test_build_attended_plan_sensitive_raw_text_nonleak(env):
    conn, _, base, _, _ = env
    attempt_id = base["attempt_id"]

    plan = build_attended_plan(conn, attempt_id)
    plan_json = json.dumps(plan)

    # Confidential candidate text and secret job description must not leak
    assert "confidential candidate resume text" not in plan_json
    assert "Secret job description" not in plan_json
    assert "password" not in plan_json.casefold()
    assert "credential" not in plan_json.casefold()
