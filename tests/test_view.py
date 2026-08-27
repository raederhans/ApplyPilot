from __future__ import annotations

import sqlite3

from applypilot import view
from applypilot.database import close_connection, init_db
from applypilot.view import DATA_PLACEHOLDER, collect_dashboard_data, render_dashboard


def test_dashboard_is_local_self_contained_and_script_safe() -> None:
    payload = {
        "stats": {"total": 1, "ready": 1, "scored": 1, "highFit": 1},
        "scoreDistribution": {"8": 1},
        "sources": [{"name": "Official", "total": 1, "highFit": 1}],
        "jobs": [
            {
                "title": "</script><script>alert('x')</script>",
                "company": "Example & Co",
                "source": "Official",
                "location": "Singapore",
                "score": 8,
                "url": "javascript:alert(1)",
                "applicationUrl": "https://example.test/apply",
                "description": "Evidence-backed role",
            }
        ],
    }

    html = render_dashboard(payload)

    assert DATA_PLACEHOLDER not in html
    assert "</script><script>alert('x')</script>" not in html
    assert "\\u003c/script\\u003e" in html
    assert "https://fonts." not in html
    assert "Opportunity Workbench" in html
    assert "Private by default" in html


def test_verification_defaults_to_action_queue_with_bounded_pages() -> None:
    html = render_dashboard(
        {
            "stats": {"total": 0, "ready": 0, "scored": 0, "highFit": 0},
            "scoreDistribution": {},
            "sources": [],
            "jobs": [],
            "verify": {"jobs": []},
        }
    )

    assert 'data-verify-filter="action_needed" aria-pressed="true"' in html
    assert 'data-verify-filter="all" aria-pressed="false"' in html
    assert 'const VERIFY_PAGE_SIZE = 20;' in html
    assert 'const verifyState = { filter: "action_needed"' in html
    assert 'id="discover-tab"' in html
    assert 'id="discover-panel"' in html
    assert 'class="skip-link"' in html
    assert "LOCAL SNAPSHOT · NO SYNC" in html
    assert 'id="decide-sort"' in html
    assert 'role="group" aria-label="Minimum fit score"' in html
    assert 'url.protocol === "https:" && !url.username && !url.password' in html
    assert 'const copied = document.execCommand("copy")' in html
    assert 'copyStatus.setAttribute("role", "status")' in html
    assert "Copy unavailable. Command selected for manual copy." in html
    assert "const PAGE_SIZE = 60" in html
    assert "NEXT PAGE" in html
    assert 'id="queue-title" tabindex="-1"' in html


def test_dashboard_supports_persistent_chinese_and_english_interface() -> None:
    html = render_dashboard(
        {
            "stats": {"total": 0, "ready": 0, "scored": 0, "highFit": 0},
            "scoreDistribution": {},
            "sources": [],
            "jobs": [],
            "verify": {"jobs": []},
        }
    )

    assert '<html lang="zh-CN">' in html
    assert 'const LOCALE_KEY = "applypilot.locale";' in html
    assert 'data-locale="zh" aria-pressed="true"' in html
    assert 'data-locale="en" aria-pressed="false"' in html
    assert 'role="group" aria-label="Interface language"' in html
    assert 'document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";' in html
    assert 'localStorage.setItem(LOCALE_KEY, locale)' in html
    assert '"Verification workbench": "结果核验台"' in html
    assert '"No decisive receipt recorded": "未记录决定性回执"' in html
    assert "applypilot reconcile-receipts --file" in html


def test_dashboard_collection_is_read_only(tmp_path) -> None:
    db_path = tmp_path / "workspace.db"
    conn = init_db(db_path)
    conn.execute(
        "INSERT INTO jobs (url, title, company_name, source_site, site, "
        "full_description, application_url, fit_score, eligibility_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "https://example.test/job",
            "Data Analyst",
            "Example",
            "Official",
            "Official",
            "Evidence-backed role",
            "https://example.test/apply",
            8,
            "eligible",
        ),
    )
    conn.commit()
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    data = collect_dashboard_data(conn)

    assert data["system"]["state"] == "ready"
    assert data["stats"] == {"total": 1, "ready": 1, "scored": 1, "highFit": 1}
    assert len(data["jobs"]) == 1
    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    close_connection(db_path)


def test_completed_applications_leave_active_queue_but_remain_verifiable(tmp_path) -> None:
    db_path = tmp_path / "workspace.db"
    conn = init_db(db_path)
    conn.executemany(
        "INSERT INTO jobs (url, title, company_name, source_site, full_description, "
        "application_url, fit_score, eligibility_status, apply_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "https://example.test/active",
                "Active role",
                "Example",
                "Official",
                "Evidence-backed role",
                "https://example.test/active/apply",
                8,
                "eligible",
                None,
            ),
            (
                "https://example.test/applied",
                "Completed role",
                "Example",
                "Official",
                "Evidence-backed role",
                "https://example.test/applied/apply",
                9,
                "eligible",
                "applied",
            ),
            (
                "https://example.test/uncertain",
                "Pending receipt role",
                "Example",
                "Official",
                "Evidence-backed role",
                "https://example.test/uncertain/apply",
                9,
                "eligible",
                "submission_uncertain",
            ),
            (
                "https://example.test/blocked",
                "Blocked retry role",
                "Example",
                "Official",
                "Evidence-backed role",
                "https://example.test/blocked/apply",
                9,
                "eligible",
                "failed",
            ),
        ],
    )
    conn.execute(
        "UPDATE jobs SET apply_retry_blocked = 1 WHERE url = ?",
        ("https://example.test/blocked",),
    )
    conn.commit()

    data = collect_dashboard_data(conn)

    assert data["stats"] == {"total": 1, "ready": 1, "scored": 1, "highFit": 1}
    assert [job["title"] for job in data["jobs"]] == ["Active role"]
    assert {job["title"] for job in data["verify"]["jobs"]} == {
        "Completed role",
        "Pending receipt role",
        "Blocked retry role",
    }
    close_connection(db_path)


def test_missing_database_returns_actionable_empty_state_without_creating_it(
    tmp_path, monkeypatch
) -> None:
    db_path = tmp_path / "not-created.db"
    monkeypatch.setattr(view, "DB_PATH", db_path)

    data = collect_dashboard_data()

    assert data["system"]["state"] == "empty"
    assert data["system"]["actions"][0]["command"] == "applypilot radar collect"
    assert data["jobs"] == []
    assert not db_path.exists()


def test_generation_renders_recoverable_database_error(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "broken.db"
    db_path.write_bytes(b"not a sqlite database")
    monkeypatch.setattr(view, "DB_PATH", db_path)
    output = tmp_path / "dashboard.html"

    result = view.generate_dashboard(str(output))
    html = output.read_text(encoding="utf-8")

    assert result == str(output.resolve())
    assert '"state":"error"' in html
    assert "Unable to read the local workspace" in html
    assert "applypilot doctor" in html
    assert "DatabaseError: file is not a database" in html


def test_generation_renders_old_schema_as_read_only_error(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "old-schema.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE jobs (url TEXT PRIMARY KEY)")
    conn.close()
    before = db_path.read_bytes()
    monkeypatch.setattr(view, "DB_PATH", db_path)
    output = tmp_path / "dashboard.html"

    view.generate_dashboard(str(output))
    html = output.read_text(encoding="utf-8")

    assert '"state":"error"' in html
    assert "OperationalError:" in html
    assert db_path.read_bytes() == before


def test_partially_scored_workspace_keeps_scoring_action(tmp_path) -> None:
    db_path = tmp_path / "workspace.db"
    conn = init_db(db_path)
    conn.executemany(
        "INSERT INTO jobs (url, title, company_name, eligibility_status, fit_score) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("https://example.test/scored", "Scored", "Example", "eligible", 4),
            ("https://example.test/unscored", "Unscored", "Example", "eligible", None),
        ],
    )
    conn.commit()

    data = collect_dashboard_data(conn)

    assert data["system"]["state"] == "needs_scoring"
    assert data["system"]["actions"][0]["command"] == "applypilot run enrich score"
    close_connection(db_path)


def test_large_dashboard_preserves_complete_data_with_bounded_initial_render() -> None:
    jobs = [
        {
            "title": f"Role {index}",
            "company": "Example",
            "source": "Official",
            "location": "Singapore",
            "score": 8,
            "url": f"https://example.test/jobs/{index}",
            "applicationUrl": f"https://example.test/jobs/{index}/apply",
            "description": "Evidence " * 400,
        }
        for index in range(1000)
    ]

    html = render_dashboard(
        {
            "system": {"state": "ready"},
            "stats": {"total": 1000, "ready": 1000, "scored": 1000, "highFit": 1000},
            "scoreDistribution": {"8": 1000},
            "sources": [{"name": "Official", "total": 1000, "highFit": 1000}],
            "jobs": jobs,
        }
    )

    assert "Role 0" in html
    assert "Role 999" in html
    assert "const PAGE_SIZE = 60" in html
    assert len(html.encode("utf-8")) < 6_000_000
