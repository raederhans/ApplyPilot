import json
import sqlite3
from pathlib import Path

import pytest

from applypilot.resume_library import _artifact_is_current, _register_artifact, ensure_resume_library_schema
from applypilot.resume_versions import (
    changed_used_facts,
    finish_resume_run,
    health_input_digest,
    start_resume_run,
)


def test_same_content_new_pdf_keeps_distinct_editions_and_reports(tmp_path: Path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_resume_library_schema(conn)
    ids = []
    for name in ("first", "second"):
        text = tmp_path / "tailored_resumes" / f"{name}.txt"
        text.parent.mkdir(exist_ok=True)
        text.write_text("A real unchanged resume", encoding="utf-8")
        text.with_suffix(".pdf").write_bytes(name.encode())
        report = text.with_suffix(".json")
        report.write_text(json.dumps({"edition": name}), encoding="utf-8")
        ids.append(_register_artifact(
            conn, text_path=text, kind="tailored", track="ai",
            source_resume_path="source.docx", validation_status="machine_validated",
            report_path=str(report),
        )[0])
    assert ids[0] == ids[1]
    renders = conn.execute("SELECT * FROM resume_render_versions ORDER BY created_at").fetchall()
    assert len(renders) == 2
    assert [Path(r["pdf_path"]).read_bytes() for r in renders] == [b"first", b"second"]
    assert [json.loads(Path(r["validation_report_path"]).read_text())["edition"] for r in renders] == ["first", "second"]
    current = dict(conn.execute("SELECT * FROM resume_artifacts").fetchone())
    assert Path(current["pdf_path"]).read_bytes() == b"second"
    _register_artifact(
        conn, text_path=tmp_path / "tailored_resumes" / "first.txt", kind="tailored", track="ai",
        source_resume_path="source.docx", validation_status="machine_validated", promote=False,
    )
    assert Path(conn.execute("SELECT pdf_path FROM resume_artifacts").fetchone()[0]).read_bytes() == b"second"
    assert all(r["layout_version"] == "legacy-unknown" for r in renders)
    assert _artifact_is_current(current)
    Path(current["text_path"]).write_text("Unexpectedly changed text", encoding="utf-8")
    assert not _artifact_is_current(current)


def test_repeated_job_runs_keep_separate_process_and_verdict(tmp_path: Path):
    job = {"url": "https://example.test/1", "title": "Same title", "company_name": "Same company"}
    runs = [start_resume_run(tmp_path, job, kind="tailoring") for _ in range(2)]
    for n, run in enumerate(runs):
        finish_resume_run(run, {"status": "machine_validated", "generation_diagnostics": [n]}, source_text="source")
        assert "generation_diagnostics" not in json.loads((run / "validation.json").read_text())
        assert json.loads((run / "generation.json").read_text())["generation_diagnostics"] == [n]
    assert runs[0] != runs[1]
    with pytest.raises(ValueError, match="Immutable"):
        finish_resume_run(runs[0], {"status": "failed", "generation_diagnostics": [2]})


def test_health_cache_tracks_source_and_layout_and_only_used_fact_changes(tmp_path: Path):
    source = tmp_path / "base.txt"
    source.write_text("Original")
    artifact = {"text_path": str(source), "source_resume_path": str(source)}
    first = health_input_digest(artifact, {})
    source.write_text("Corrected")
    assert first != health_input_digest(artifact, {})
    assert health_input_digest(artifact, {}) != health_input_digest(
        artifact, {"tailoring": {"resume_layout": {"font_size": 11}}}
    )
    assert changed_used_facts({"gpa": "3.46"}, {"gpa": "3.60"}, "GPA 3.46") == ["gpa"]
    assert changed_used_facts({"gpa": "3.46"}, {"gpa": "3.60"}, "No GPA disclosed") == []


def test_curation_adds_relevant_source_project_without_fabricating():
    from applypilot.resume_curation import refine_existing_text

    source = """Candidate
contact@example.test
PROJECTS
Verified Data Dashboard
Builder | 2025
- Used Python and SQL to build a dashboard for source-proven users.
"""
    original = "Candidate\ncontact@example.test\nEDUCATION\nUniversity of Testing\n"
    result = refine_existing_text(original, relevance_terms=["python", "sql"], supplemental_text=source)
    assert result["claims_preserved"]
    assert "Verified Data Dashboard" in result["text"]
    assert result["text"].index("PROJECTS") < result["text"].index("EDUCATION")
    assert "SUMMARY" not in result["text"]
    assert refine_existing_text(original, relevance_terms=["unrelated"], supplemental_text=source)["text"] == original


def test_curation_consolidates_duplicate_project_from_exact_source():
    from applypilot.resume_curation import refine_existing_text
    from applypilot.scoring.pdf import parse_entries, parse_resume

    entry = "Research Project\nLead | 2026\n- An older description of the same research.\n"
    original = "Candidate\ncontact@example.test\nPROJECTS\n" + entry + "\n" + entry
    evidence = "PROJECTS\nResearch Project\nLead | 2026\n- Source-verified methods and measured results.\n"
    result = refine_existing_text(original, supplemental_text=evidence,
                                   refresh_project_titles=["Research Project"])
    entries = parse_entries(parse_resume(result["text"])["sections"]["PROJECTS"])
    assert len(entries) == 1
    assert entries[0]["bullets"] == ["Source-verified methods and measured results."]
    assert result["claims_preserved"] is False


def test_submitted_job_revalidation_preserves_all_history(tmp_path, monkeypatch):
    from applypilot import single_job
    from applypilot.database import init_db

    database = tmp_path / "jobs.db"
    conn = init_db(database)
    conn.execute("INSERT INTO jobs (url,title,apply_status,applied_at) VALUES ('https://example.test/done','Role','applied','2026-09-01')")
    conn.commit()
    before = tuple(conn.execute("SELECT * FROM jobs").fetchone())
    monkeypatch.setattr(single_job, "get_connection", lambda: conn)
    monkeypatch.setattr(single_job.config, "APP_DIR", tmp_path)
    result = single_job.revalidate_tailored_resume_for_url("https://example.test/done")
    assert result["status"] == "failed_revalidation"
    assert "frozen" in result["error"]
    with sqlite3.connect(database) as verify:
        assert tuple(verify.execute("SELECT * FROM jobs").fetchone()) == before
    assert not (tmp_path / "resume-runs").exists()


def test_page_span_uses_page_transform_and_excludes_reset_text_matrix(monkeypatch):
    from types import SimpleNamespace

    import pypdf

    from applypilot.scoring.pdf import _pdf_page_text_spans

    class Page:
        mediabox = SimpleNamespace(bottom=0, top=792)

        def extract_text(self, visitor_text):
            cm = [0.75, 0, 0, -0.75, 36, 1476]
            visitor_text("top text", cm, [1, 0, 0, 1, 12, 980], {}, 10)
            visitor_text("bottom text", cm, [1, 0, 0, 1, 12, 1180], {}, 10)
            visitor_text("reset date flush", cm, [1, 0, 0, 1, 0, 0], {}, 10)

    monkeypatch.setattr(pypdf, "PdfReader", lambda _: SimpleNamespace(pages=[Page()]))
    assert _pdf_page_text_spans("test.pdf") == [150]


def test_docx_source_retains_lists_sections_and_gpa(tmp_path: Path):
    from zipfile import ZipFile

    from applypilot.scoring.cover_letter import read_resume_source
    from applypilot.scoring.pdf import parse_entries, parse_resume

    docx = tmp_path / "source.docx"
    with ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", '''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
        <w:p><w:r><w:t>Candidate</w:t></w:r></w:p>
        <w:p><w:r><w:t>WORK EXPERIENCE</w:t></w:r></w:p>
        <w:p><w:r><w:t>Real Company</w:t></w:r></w:p>
        <w:p><w:r><w:t>Analyst | 2024 - Present</w:t></w:r></w:p>
        <w:p><w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Built source-proven analytics.</w:t></w:r></w:p>
        <w:p><w:r><w:t>EDUCATION</w:t></w:r></w:p>
        <w:p><w:r><w:t>University</w:t></w:r></w:p>
        <w:p><w:r><w:t>GPA: 3.46/4.0</w:t></w:r></w:p>
        </w:body></w:document>''')
    original = docx.read_bytes()
    parsed = parse_resume(read_resume_source(docx))
    assert parse_entries(parsed["sections"]["EXPERIENCE"])[0]["bullets"] == ["Built source-proven analytics."]
    assert "GPA: 3.46/4.0" in parsed["sections"]["EDUCATION"]
    assert docx.read_bytes() == original


def test_curation_reorders_without_changing_claims():
    from applypilot.resume_curation import refine_existing_text

    text = '''Candidate
Analyst
candidate@example.test

EXPERIENCE
Old Company
Analyst | 2020 - 2021
- An original factual claim.

Current Company
Analyst | 2024 - Present
- A current factual claim.

EDUCATION
University One, Degree; University Two, Degree
'''
    result = refine_existing_text(text)
    assert result["claims_preserved"]
    assert result["text"].index("Current Company") < result["text"].index("Old Company")
    assert "Candidate\ncandidate@example.test" in result["text"]
    assert "University One, Degree\nUniversity Two" in result["text"]

def test_reviewed_edits_require_unique_text_and_evidence():
    from applypilot.resume_curation import apply_reviewed_edits
    edit = {'before': 'Built a tool.', 'after': 'Built a reporting tool for the planning team.',
            'reason': 'Restore project purpose.', 'source_evidence': 'Source entry: reporting tool for planning team.'}
    result = apply_reviewed_edits('Built a tool.', [edit])
    assert result['text'] == edit['after']
    assert result['changes'][0]['source_evidence'] == edit['source_evidence']
    with pytest.raises(ValueError, match='exactly one'):
        apply_reviewed_edits('Built a tool. Built a tool.', [edit])
    with pytest.raises(ValueError, match='source evidence'):
        apply_reviewed_edits('Built a tool.', [{**edit, 'source_evidence': ''}])

def test_merged_edition_is_not_reactivated_by_historical_registration(tmp_path: Path):
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    ensure_resume_library_schema(conn)
    text = tmp_path / 'resume.txt'
    text.write_text('Original historical evidence', encoding='utf-8')
    text.with_suffix('.pdf').write_bytes(b'historical-render')
    args = {'text_path': text, 'kind': 'tailored', 'track': 'ai', 'source_resume_path': 'source.txt',
            'validation_status': 'machine_validated', 'report_path': None, 'metadata': {}}
    artifact_id, _ = _register_artifact(conn, **args)
    conn.execute("UPDATE resume_artifacts SET active=0, validation_status='superseded_editorial', metadata_json=? WHERE artifact_id=?",
                 (json.dumps({'merged_into': 'retained-successor'}), artifact_id))
    _register_artifact(conn, **args)
    row = conn.execute('SELECT active,validation_status,metadata_json FROM resume_artifacts WHERE artifact_id=?', (artifact_id,)).fetchone()
    assert row['active'] == 0
    assert row['validation_status'] == 'superseded_editorial'
    assert json.loads(row['metadata_json'])['merged_into'] == 'retained-successor'
