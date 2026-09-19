import json
from datetime import UTC, datetime

import pytest

from applypilot.apply import attended_application as attended
from applypilot.apply.authorization import build_bound_manifest
from applypilot.database import init_db


@pytest.fixture
def env(tmp_path):
    conn = init_db(tmp_path / "jobs.db")
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 fixture")
    job = {
        "url": "https://boards.greenhouse.io/example/jobs/123",
        "application_url": "https://boards.greenhouse.io/example/jobs/123",
        "company_name": "Example",
        "title": "Engineer",
        "full_description": "Engineer role",
        "fit_score": 8,
        "tailor_status": "machine_validated",
        "tailored_resume_path": str(pdf),
        "cover_letter_status": "not_required",
        "eligibility_status": "eligible",
    }
    conn.execute(
        "INSERT INTO jobs (" + ",".join(job) + ") VALUES (" + ",".join("?" for _ in job) + ")", tuple(job.values())
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
    yield conn, run, base, pdf
    conn.close()


def observation():
    return {
        "source": "attending_host",
        "observed_at": datetime.now(UTC).isoformat(),
        "evidence_refs": ["fixture-capture"],
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


def prepare(run):
    run("checkpoint", snapshot=snapshot(), **observation())
    return run("claim", snapshot=snapshot())


def test_roundtrip_gate_receipt_and_no_replay(env):
    conn, run, _base, _ = env
    claimed = prepare(run)
    assert run("claim", snapshot=snapshot())["gate_id"] == claimed["gate_id"]
    started = run("submit-intent", snapshot=snapshot(), checkpoint_digest=claimed["checkpoint_digest"], **observation())
    assert started["submit_started"]
    with pytest.raises(ValueError, match="already started"):
        run("submit-intent", snapshot=snapshot(), checkpoint_digest=claimed["checkpoint_digest"], **observation())
    assert run("resume")["next_action"] == "reconcile_receipt_only"
    receipt = {
        "source": "browser_receipt",
        "receipt_id": "receipt-1",
        "company_name": "Example",
        "job_title": "Engineer",
        "confirmation_text": "Your application has been submitted",
    }
    result = run("receipt", receipt=receipt, **observation())
    assert result["status"] == "applied", result
    result = run("receipt", receipt=receipt, **observation())
    assert result["status"] == "applied", result
    assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] == "applied"


def test_binding_and_private_checkpoint(env):
    conn, run, base, _ = env
    data = snapshot()
    data["email_values"] = ["private@example.test"]
    run("checkpoint", snapshot=data, **observation())
    assert "private@example.test" not in conn.execute("SELECT payload FROM attended_applications").fetchone()[0]
    with pytest.raises(ValueError, match="binding mismatch"):
        attended.execute(conn, dict(base, action="resume", tab_id="other"), {})
    with pytest.raises(ValueError, match="existing attempt"):
        run("begin", manifest_path="not-read")


def test_material_change_invalidates(env):
    _, run, _, pdf = env
    run("checkpoint", snapshot=snapshot(), **observation())
    pdf.write_bytes(b"changed")
    result = run("resume")
    assert result["phase"] == "material_changed" and "checkpoint" not in result


def test_audit_required_and_incomplete_snapshot(env):
    _, run, _, _ = env
    data = snapshot()
    data["required_unfilled"] = ["Start date"]
    run("checkpoint", snapshot=data, **observation())
    with pytest.raises(ValueError, match="required_field_empty"):
        run("claim", snapshot=data)
    run("checkpoint", snapshot={"url": data["url"]}, **observation())
    with pytest.raises(ValueError, match="incomplete pre-submit"):
        run("claim", snapshot={"url": data["url"]})


def test_failure_after_intent_is_uncertain(env):
    conn, run, _, _ = env
    claimed = prepare(run)
    run("submit-intent", snapshot=snapshot(), checkpoint_digest=claimed["checkpoint_digest"], **observation())
    assert run("fail", reason="host connection lost")["phase"] == "submission_uncertain"
    assert conn.execute("SELECT submit_started FROM application_attempts").fetchone()[0] == 1
    assert run("resume")["next_action"] == "reconcile_receipt_only"


def test_expired_and_terminal_attempts(env):
    conn, run, _, _ = env
    conn.execute("UPDATE application_attempts SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    conn.commit()
    with pytest.raises(ValueError, match="lease expired"):
        run("resume")
    run("fail", reason="abandoned before submit")
    with pytest.raises(ValueError, match="terminal"):
        run("resume")


def test_cross_job_receipt_rejected(env):
    _, run, _, _ = env
    claimed = prepare(run)
    run("submit-intent", snapshot=snapshot(), checkpoint_digest=claimed["checkpoint_digest"], **observation())
    with pytest.raises(ValueError, match="receipt binding mismatch"):
        run("receipt", receipt={"job_url": "https://other.test/2"}, **observation())


def test_cli_bad_envelope_is_structured_failure(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from applypilot import cli

    db = tmp_path / "jobs.db"
    init_db(db).close()
    request = tmp_path / "request.json"
    request.write_text("[]")
    result = CliRunner().invoke(cli.app, ["attended-application", "--db", str(db), "--request-file", str(request)])
    assert result.exit_code == 2
    assert "request must be an object" in result.output


def test_admission_and_snapshot_changes_block(env):
    conn, run, _, _ = env
    run("checkpoint", snapshot=snapshot(), **observation())
    changed = snapshot()
    changed["url"] += "?changed=1"
    with pytest.raises(ValueError, match="fresh snapshot"):
        run("claim", snapshot=changed)
    conn.execute("UPDATE jobs SET fit_score=1")
    conn.commit()
    with pytest.raises(ValueError, match="submission admission"):
        run("claim", snapshot=snapshot())
    assert conn.execute("SELECT COUNT(*) FROM application_submission_gates").fetchone()[0] == 0


def test_expired_observation_and_worker_assertion_block(env):
    _, run, _, _ = env
    obs = observation()
    obs["observed_at"] = "2000-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="five minutes"):
        run("checkpoint", snapshot=snapshot(), **obs)
    obs = observation()
    obs["source"] = "child_worker"
    with pytest.raises(ValueError, match="independent"):
        run("checkpoint", snapshot=snapshot(), **obs)


def test_false_receipt_does_not_mark_success(env):
    conn, run, _, _ = env
    claimed = prepare(run)
    run("submit-intent", snapshot=snapshot(), checkpoint_digest=claimed["checkpoint_digest"], **observation())
    result = run(
        "receipt",
        receipt={
            "source": "browser_receipt",
            "receipt_id": "bad",
            "company_name": "Example",
            "job_title": "Engineer",
            "confirmation_text": "Please click Submit",
        },
        **observation(),
    )
    assert result["status"] == "rejected"
    assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] == "applying"


def test_cli_resume_reads_durable_identity(env, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from applypilot import cli

    conn, _, base, _ = env
    db = conn.execute("PRAGMA database_list").fetchone()[2]
    request = tmp_path / "resume.json"
    request.write_text(json.dumps(dict(base, action="resume")))
    from applypilot import config

    monkeypatch.setattr(config, "load_profile", lambda: {"submission_policy": {"allow_runtime_readiness_review": True}})
    result = CliRunner().invoke(cli.app, ["attended-application", "--db", db, "--request-file", str(request)])
    assert result.exit_code == 0, result.output
    assert base["attempt_id"] in result.output


def test_lost_begin_response_recovers_same_attempt(env):
    conn, run, base, _ = env
    stored = json.loads(conn.execute("SELECT payload FROM attended_applications").fetchone()[0])
    recovered = run("begin", manifest_path=stored["manifest_path"])
    assert recovered["attempt_id"] == base["attempt_id"]
    assert conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 1


def test_native_acquire_cannot_take_attended_job(env, monkeypatch):
    from applypilot import config
    from applypilot.apply.application_jobs import acquire_job

    conn, run, base, _ = env
    monkeypatch.setattr(config, "load_profile", lambda: {"submission_policy": {"allow_runtime_readiness_review": True}})
    before = dict(conn.execute("SELECT * FROM jobs").fetchone())
    assert before["apply_status"] == "in_progress"
    assert before["agent_id"] == "attending_host"
    assert before["apply_task_id"] == base["attempt_id"]
    for target in (base["job_url"], None):
        assert (
            acquire_job(
                conn,
                target_url=target,
                min_score=4,
                worker_id=99,
                load_blocked=lambda: ([], []),
                application_lease_minutes=45,
            )
            is None
        )
    after = dict(conn.execute("SELECT * FROM jobs").fetchone())
    assert after["apply_task_id"] == base["attempt_id"]
    assert conn.execute("SELECT COUNT(*) FROM application_attempts").fetchone()[0] == 1
    # Once explicitly finalized before submission, native acquisition works.
    run("fail", reason="handoff before submit")
    acquired = acquire_job(
        conn,
        target_url=base["job_url"],
        min_score=4,
        worker_id=99,
        load_blocked=lambda: ([], []),
        application_lease_minutes=45,
    )
    assert acquired is not None
    assert acquired["_attempt_id"] != base["attempt_id"]


def test_wrong_company_receipt_has_no_ledger_side_effect(env):
    conn, run, _, _ = env
    claimed = prepare(run)
    run("submit-intent", snapshot=snapshot(), checkpoint_digest=claimed["checkpoint_digest"], **observation())
    result = run(
        "receipt",
        receipt={
            "source": "browser_receipt",
            "receipt_id": "other-company",
            "company_name": "Different",
            "job_title": "Engineer",
            "confirmation_text": "Your application has been submitted",
        },
        **observation(),
    )
    assert result["reason"] == "company_mismatch"
    assert conn.execute("SELECT apply_status FROM jobs").fetchone()[0] == "applying"
    assert conn.execute("SELECT state FROM application_submission_gates").fetchone()[0] == "claimed"
    assert conn.execute("SELECT status FROM application_batch_consumptions").fetchone()[0] == "reserved"
    assert conn.execute("SELECT COUNT(*) FROM application_receipts").fetchone()[0] == 0


def upload_evidence(conn, base):
    state = json.loads(
        conn.execute("SELECT payload FROM attended_applications WHERE attempt_id=?", (base["attempt_id"],)).fetchone()[
            0
        ]
    )
    material = next(x for x in state["materials"]["materials"] if x["kind"] == "resume")
    return {
        "attempt_id": base["attempt_id"],
        "page_url": base["job_url"],
        "field_label": "Resume",
        "visible_filename": "resume.pdf",
        "sha256": material["sha256"],
        "size": material["size"],
        "acceptance_marker": "attachment_card",
        "accepted_attachment_text": "resume.pdf Remove attachment",
    }


def test_multistep_final_page_requires_accepted_upload_proof(env):
    conn, run, base, _ = env
    final = snapshot()
    final.update(resume_field_present=False, resume_uploaded=False)
    run("checkpoint", snapshot=final, **observation())
    with pytest.raises(ValueError, match="resume_state_unconfirmed"):
        run("claim", snapshot=final)
    run("upload-checkpoint", upload=upload_evidence(conn, base), **observation())
    run("checkpoint", snapshot=final, **observation())
    claimed = run("claim", snapshot=final)
    assert claimed["gate_id"]
    assert run("submit-intent", snapshot=final, checkpoint_digest=claimed["checkpoint_digest"], **observation())[
        "submit_started"
    ]


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("sha256", "wrong", "bytes differ"),
        ("size", 1, "bytes differ"),
        ("page_url", "https://boards.greenhouse.io/other/jobs/321", "outside"),
        ("visible_filename", "other.pdf", "filename differs"),
        ("attempt_id", "other-attempt", "current attempt"),
        ("field_label", "Cover letter", "Resume or CV"),
        ("acceptance_marker", "file_input", "accepted attachment"),
        ("accepted_attachment_text", "Upload complete", "accepted attachment"),
    ],
)
def test_upload_proof_rejects_unbound_or_unaccepted_values(env, field, value, reason):
    conn, run, base, _ = env
    upload = upload_evidence(conn, base)
    upload[field] = value
    with pytest.raises(ValueError, match=reason):
        run("upload-checkpoint", upload=upload, **observation())
    assert "resume_upload" not in json.loads(conn.execute("SELECT payload FROM attended_applications").fetchone()[0])


def test_upload_proof_invalidated_by_material_change(env):
    conn, run, base, pdf = env
    run("upload-checkpoint", upload=upload_evidence(conn, base), **observation())
    pdf.write_bytes(b"other resume")
    result = run("resume")
    assert result["phase"] == "material_changed"
    assert "resume_upload" not in result


def test_upload_proof_does_not_override_observed_removal(env):
    conn, run, base, _ = env
    run("upload-checkpoint", upload=upload_evidence(conn, base), **observation())
    removal = snapshot()
    removal["resume_uploaded"] = False
    run("checkpoint", snapshot=removal, **observation())
    final = snapshot()
    final.update(resume_field_present=False, resume_uploaded=False)
    run("checkpoint", snapshot=final, **observation())
    with pytest.raises(ValueError, match="resume_state_unconfirmed"):
        run("claim", snapshot=final)


def test_upload_proof_cannot_be_replayed_into_new_attempt(env):
    conn, run, base, _ = env
    old_proof = upload_evidence(conn, base)
    manifest = json.loads(conn.execute("SELECT payload FROM attended_applications").fetchone()[0])["manifest_path"]
    run("fail", reason="stop before submit")
    new_state = run("begin", manifest_path=manifest)
    assert new_state["attempt_id"] != base["attempt_id"]
    with pytest.raises(ValueError, match="current attempt"):
        attended.execute(
            conn,
            dict(
                base, attempt_id=new_state["attempt_id"], action="upload-checkpoint", upload=old_proof, **observation()
            ),
            {"submission_policy": {"allow_runtime_readiness_review": True}},
        )


def test_upload_query_identity_cannot_drift_on_same_path(env):
    conn, run, base, _ = env
    upload = upload_evidence(conn, base)
    upload["page_url"] += "?token=other-job"
    with pytest.raises(ValueError, match="query job identity"):
        run("upload-checkpoint", upload=upload, **observation())


@pytest.mark.parametrize("key", ["token", "for", "gh_jid", "jobId", "job_id"])
def test_final_page_cannot_change_query_job_identity(env, key):
    conn, run, _, _ = env
    final = snapshot()
    final["url"] += f"?{key}=different-job"
    run("checkpoint", snapshot=final, **observation())
    with pytest.raises(ValueError, match="query job identity"):
        run("claim", snapshot=final)
    assert conn.execute("SELECT COUNT(*) FROM application_submission_gates").fetchone()[0] == 0
    assert conn.execute("SELECT submit_started FROM application_attempts").fetchone()[0] == 0


def test_final_page_tracking_query_does_not_change_job_identity(env):
    _, run, _, _ = env
    final = snapshot()
    final["url"] += "?utm_source=careers"
    run("checkpoint", snapshot=final, **observation())
    claimed = run("claim", snapshot=final)
    assert run("submit-intent", snapshot=final, checkpoint_digest=claimed["checkpoint_digest"], **observation())[
        "submit_started"
    ]


@pytest.mark.parametrize("fact", ["experience", "project_references", "resume_facts", "skills_boundary"])
@pytest.mark.parametrize("action", ["resume", "claim", "submit-intent"])
def test_resume_fact_changes_invalidate_active_preparation(env, fact, action):
    conn, run, base, _ = env
    claimed = prepare(run)
    updated_profile = {
        "submission_policy": {"allow_runtime_readiness_review": True},
        fact: {"corrected": "new factual evidence"},
    }
    result = attended.execute(
        conn,
        dict(base, action=action, snapshot=snapshot(), checkpoint_digest=claimed["checkpoint_digest"], **observation()),
        updated_profile,
    )
    assert result["phase"] == "material_changed"
    assert "checkpoint" not in result
    assert conn.execute("SELECT submit_started FROM application_attempts").fetchone()[0] == 0
    assert "new factual evidence" not in conn.execute("SELECT payload FROM attended_applications").fetchone()[0]


def test_upload_proof_rejected_after_lease_expiry(env):
    conn, run, base, _ = env
    upload = upload_evidence(conn, base)
    conn.execute("UPDATE application_attempts SET lease_expires_at='2000-01-01T00:00:00+00:00'")
    conn.commit()
    with pytest.raises(ValueError, match="lease expired"):
        run("upload-checkpoint", upload=upload, **observation())
