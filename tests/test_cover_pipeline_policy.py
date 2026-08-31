"""Narrow regression tests for cover-letter pipeline policy."""

import threading
from pathlib import Path

import pytest

from applypilot import pipeline
from applypilot.database import init_db
from applypilot.pipeline import _run_sequential
from applypilot.scoring import cover_letter


@pytest.mark.parametrize(
    "profile",
    [
        {},
        {"cover_letter": {}},
        {"cover_letter": {"auto_generate_in_full_pipeline": False}},
    ],
)
def test_full_pipeline_skips_speculative_cover_by_default(monkeypatch, profile) -> None:
    """A full pipeline must not manufacture cover letters without opt-in."""
    monkeypatch.setattr(pipeline, "load_profile", lambda: profile)
    monkeypatch.setattr(
        cover_letter,
        "run_cover_letters",
        lambda **kwargs: pytest.fail("speculative cover generation was invoked"),
    )

    result = _run_sequential(["cover"], min_score=7, full_pipeline=True)

    assert len(result["stages"]) == 1
    assert result["stages"][0]["stage"] == "cover"
    assert result["stages"][0]["status"] == "skipped"
    assert result["errors"] == {}


def test_explicit_cover_stage_remains_available(monkeypatch) -> None:
    """An explicitly requested cover stage keeps the existing generation path."""
    calls = []

    def fake_run_cover_letters(**kwargs):
        calls.append(kwargs)
        return {"generated": 1, "errors": 0}

    monkeypatch.setattr(cover_letter, "run_cover_letters", fake_run_cover_letters)

    result = _run_sequential(
        ["cover"], min_score=8, validation_mode="strict", full_pipeline=False
    )

    assert result["stages"][0]["status"] == "ok"
    assert calls == [{"min_score": 8, "validation_mode": "strict"}]


def test_streaming_skip_waits_for_tailor_before_marking_cover_done(monkeypatch) -> None:
    """A skipped cover stage preserves the streaming dependency boundary."""
    monkeypatch.setattr(pipeline, "load_profile", dict)
    tracker = pipeline._StageTracker()
    stop_event = threading.Event()
    worker = threading.Thread(
        target=pipeline._run_stage_streaming,
        args=("cover", tracker, stop_event, 7, 1, "normal", True),
    )
    worker.start()

    assert not tracker.wait("cover", timeout=0.05)
    tracker.mark_done("tailor")
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert tracker.get_results()["cover"]["status"] == "skipped"


def test_all_marker_enables_full_pipeline_policy(monkeypatch) -> None:
    """The all marker remains a full pipeline even with an extra stage name."""
    captured = {}
    stats = {
        "total": 0,
        "pending_detail": 0,
        "with_description": 0,
        "scored": 0,
        "tailored": 0,
        "with_cover_letter": 0,
        "ready_to_apply": 0,
        "applied": 0,
    }

    monkeypatch.setattr(pipeline, "load_env", lambda: None)
    monkeypatch.setattr(pipeline, "ensure_dirs", lambda: None)
    monkeypatch.setattr(pipeline, "init_db", lambda: None)
    monkeypatch.setattr(pipeline, "get_stats", lambda: stats)

    def fake_sequential(ordered, min_score, workers, validation_mode, full_pipeline):
        captured.update(
            ordered=ordered,
            min_score=min_score,
            workers=workers,
            validation_mode=validation_mode,
            full_pipeline=full_pipeline,
        )
        return {"stages": [], "errors": {}, "elapsed": 0.0}

    monkeypatch.setattr(pipeline, "_run_sequential", fake_sequential)

    pipeline.run_pipeline(stages=["cover", "all"])

    assert captured["full_pipeline"] is True
    assert captured["ordered"] == list(pipeline.STAGE_ORDER)


def test_batch_cover_excludes_not_required_jobs(monkeypatch, tmp_path: Path) -> None:
    """The batch query must never regenerate a job explicitly marked not required."""
    conn = init_db(tmp_path / "jobs.db")
    skipped_resume = tmp_path / "skipped.txt"
    selected_resume = tmp_path / "selected.txt"
    skipped_resume.write_text("SKIPPED RESUME", encoding="utf-8")
    selected_resume.write_text("SELECTED RESUME", encoding="utf-8")

    conn.executemany(
        "INSERT INTO jobs (url, title, company_name, source_site, site, full_description, "
        "fit_score, tailored_resume_path, tailor_status, eligibility_status, cover_letter_status) "
        "VALUES (?, ?, 'Example', 'linkedin', 'linkedin', 'Verified JD', 9, ?, "
        "'machine_validated', 'eligible', ?)",
        [
            ("https://example.com/not-required", "Not Required", str(skipped_resume), "not_required"),
            ("https://example.com/needs-cover", "Needs Cover", str(selected_resume), None),
        ],
    )
    conn.commit()

    generated_urls = []

    def fake_generate(resume_text, job, profile, **kwargs):
        generated_urls.append(job["url"])
        return {
            "text": "Dear Hiring Manager,\n\nVerified application evidence.\n\nSincerely,\n\nApplicant",
            "validation": {"passed": True, "errors": [], "warnings": []},
            "evidence_plan": {"requirements": []},
            "surface": "formal",
        }

    monkeypatch.setattr(cover_letter, "get_connection", lambda: conn)
    monkeypatch.setattr(cover_letter, "load_profile", lambda: {"cover_letter": {}})
    monkeypatch.setattr(cover_letter, "COVER_LETTER_DIR", tmp_path / "letters")
    monkeypatch.setattr(cover_letter, "load_evidence_sources", lambda *args: [])
    monkeypatch.setattr(cover_letter, "generate_cover_letter_document", fake_generate)
    monkeypatch.setattr("applypilot.eligibility.refresh_job_eligibility", lambda _conn: None)

    result = cover_letter.run_cover_letters(min_score=7, limit=10)

    assert result["generated"] == 1
    assert generated_urls == ["https://example.com/needs-cover"]
    assert conn.execute(
        "SELECT cover_letter_status FROM jobs WHERE url='https://example.com/not-required'"
    ).fetchone()[0] == "not_required"
