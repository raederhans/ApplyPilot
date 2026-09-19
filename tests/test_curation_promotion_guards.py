import importlib.util
import json
from pathlib import Path

import pytest

from applypilot.database import init_db


@pytest.fixture
def promotion(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "tools" / "curate_resume_library.py"
    spec = importlib.util.spec_from_file_location("curation_promotion_guards", path)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    db_path = tmp_path / "library.db"
    conn = init_db(db_path)
    tool.ensure_resume_library_schema(conn)
    monkeypatch.setattr(tool.config, "DB_PATH", db_path)
    monkeypatch.setattr(tool, "load_profile", dict)
    monkeypatch.setattr(tool, "assess_resume_artifact_health", lambda *_: {"status": "eligible"})
    monkeypatch.setattr(tool, "_record_artifact_health", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(tool, "_record_validation", lambda *_args, **_kwargs: None)

    def unexpected_render(*_args, **_kwargs):
        pytest.fail("Promotion must use the inspected files, not render new ones")

    monkeypatch.setattr(tool, "convert_to_pdf", unexpected_render)
    conn.execute("INSERT INTO jobs (url,title) VALUES ('https://example.test/job','Intern')")
    records = []
    reviews = []

    def insert_artifact(artifact_id, text_path, *, active=1, status="machine_validated"):
        pdf_path = text_path.with_suffix(".pdf")
        conn.execute(
            "INSERT INTO resume_artifacts (artifact_id,content_sha256,kind,track,text_path,pdf_path,"
            "pdf_sha256,pdf_size,validation_status,active,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (artifact_id, tool.fingerprint(text_path), "tailored", "data", str(text_path), str(pdf_path),
             tool.fingerprint(pdf_path), pdf_path.stat().st_size, status, active, "created", "staged"),
        )

    for index in range(2):
        parent = f"parent-{index}"
        old_text = tmp_path / f"old-{index}.txt"
        old_text.write_text(f"Original evidence {index}", encoding="utf-8")
        old_text.with_suffix(".pdf").write_bytes(f"Original PDF {index}".encode())
        insert_artifact(parent, old_text)
        conn.execute(
            "INSERT INTO resume_coverage_cells VALUES (?,?,?,?,?,?,?)",
            (parent, "taxonomy", "data", "analysis", "https://example.test/job", "fingerprint", "then"),
        )
        run = tmp_path / f"run-{index}"
        run.mkdir()
        text = run / "resume.txt"
        text.write_text(f"Edited evidence {index}", encoding="utf-8")
        pdf = text.with_suffix(".pdf")
        pdf.write_bytes(f"Inspected PDF {index}".encode())
        report = run / "report.json"
        report.write_text('{}', encoding="utf-8")
        artifact = dict(conn.execute("SELECT * FROM resume_artifacts WHERE artifact_id=?", (parent,)).fetchone())
        records.append({
            "parent_artifact_id": parent, "parent_binding": tool.parent_binding(artifact),
            "text_roundtrip": True, "validation": {"passed": True},
            "text_path": str(text), "pdf_path": str(pdf),
            "old_text_path": str(old_text), "old_pdf_path": str(old_text.with_suffix(".pdf")),
            "old_text_sha256": tool.fingerprint(old_text),
            "old_pdf_sha256": tool.fingerprint(old_text.with_suffix(".pdf")),
            "run_dir": str(run), "report_path": str(report), "changes": [], "track": "data",
            "source_resume_path": str(old_text), "job_url": "https://example.test/job",
        })
        reviews.append({"parent_artifact_id": parent, "text_sha256": tool.fingerprint(text),
                        "pdf_sha256": tool.fingerprint(pdf)})
    conn.commit()

    def write_reports():
        (tmp_path / "staged-editions.json").write_text(
            json.dumps({"source_hashes": {}, "records": records}), encoding="utf-8")
        (tmp_path / "visual-review.json").write_text(json.dumps({"accepted": reviews}), encoding="utf-8")

    calls = []

    def register(writer, **kwargs):
        parent = kwargs["metadata"]["parent_artifact_id"]
        calls.append(parent)
        successor = parent.replace("parent", "successor")
        existing = writer.execute("SELECT artifact_id FROM resume_artifacts WHERE artifact_id=?", (successor,)).fetchone()
        if existing:
            # Registration can write provenance even when deduplication returns
            # a retired artifact. The promotion transaction must undo that too.
            writer.execute("UPDATE resume_artifacts SET metadata_json=? WHERE artifact_id=?",
                           ('{"registration_attempt":true}', successor))
        else:
            writer.execute(
                "INSERT INTO resume_artifacts (artifact_id,content_sha256,kind,text_path,pdf_path,"
                "validation_status,active,created_at,updated_at) VALUES (?,?,?,?,?,'machine_validated',1,'now','now')",
                (successor, tool.fingerprint(kwargs["text_path"]), "tailored", str(kwargs["text_path"]),
                 str(kwargs["text_path"].with_suffix(".pdf"))),
            )
        return successor, None

    monkeypatch.setattr(tool, "_register_artifact", register)
    write_reports()
    yield tool, conn, tmp_path, records, write_reports, insert_artifact, calls
    conn.close()


def snapshot(conn):
    return {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("jobs", "resume_artifacts", "resume_coverage_cells")
    }


@pytest.mark.parametrize("field,value", [
    ("active", 0), ("validation_status", "retired"),
    ("updated_at", "changed-after-review"), ("pdf_sha256", "changed-binding"),
])
def test_stale_parent_binding_rejects_and_rolls_back_prior_promotion(promotion, field, value):
    tool, conn, report_dir, _, _, _, calls = promotion
    conn.execute(f"UPDATE resume_artifacts SET {field}=? WHERE artifact_id='parent-1'", (value,))
    conn.commit()
    before = snapshot(conn)
    with pytest.raises(ValueError, match="Parent edition changed after staging"):
        tool.promote(report_dir)
    assert calls == ["parent-0"]
    assert snapshot(conn) == before
    assert not (report_dir / "promotion.json").exists()


def test_missing_parent_binding_requires_restage(promotion):
    tool, conn, report_dir, records, write_reports, _, calls = promotion
    del records[0]["parent_binding"]
    write_reports()
    before = snapshot(conn)
    with pytest.raises(ValueError, match="Parent edition changed after staging"):
        tool.promote(report_dir)
    assert calls == []
    assert snapshot(conn) == before


@pytest.mark.parametrize("active,status", [(0, "retired"), (0, "machine_validated"), (1, "failed_validation")])
def test_retired_successor_is_not_promoted_and_both_parents_survive(promotion, active, status):
    tool, conn, report_dir, records, _, insert_artifact, calls = promotion
    insert_artifact("successor-1", Path(records[1]["text_path"]), active=active, status=status)
    conn.commit()
    before = snapshot(conn)
    with pytest.raises(ValueError, match="Cannot promote retired content"):
        tool.promote(report_dir)
    assert calls == ["parent-0", "parent-1"]
    assert snapshot(conn) == before
    assert tuple(conn.execute(
        "SELECT active,validation_status FROM resume_artifacts WHERE artifact_id='parent-0'"
    ).fetchone()) == (1, "machine_validated")
    assert not (report_dir / "promotion.json").exists()


def test_current_parent_and_eligible_successor_can_promote(promotion):
    tool, conn, report_dir, _, _, _, calls = promotion
    jobs_before = snapshot(conn)["jobs"]
    tool.promote(report_dir)
    assert calls == ["parent-0", "parent-1"]
    assert snapshot(conn)["jobs"] == jobs_before
    assert conn.execute("SELECT COUNT(*) FROM resume_artifacts WHERE active=1").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM resume_artifacts WHERE validation_status='superseded_editorial'").fetchone()[0] == 2
    report = json.loads((report_dir / "promotion.json").read_text(encoding="utf-8"))
    assert len(report["promoted"]) == 2
