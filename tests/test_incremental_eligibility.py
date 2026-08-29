from __future__ import annotations

import sqlite3

from applypilot.eligibility import refresh_job_eligibility


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY,
            title TEXT,
            description TEXT,
            full_description TEXT,
            eligibility_status TEXT,
            eligibility_reason TEXT,
            eligibility_evaluated_at TEXT
        )
        """
    )
    return conn


def test_eligibility_refresh_skips_unchanged_inputs_and_invalidates_one_job() -> None:
    conn = _connection()
    conn.executemany(
        "INSERT INTO jobs (url, title, description, full_description) VALUES (?, ?, ?, ?)",
        [
            ("https://example.test/1", "One", "Open role", "Open role"),
            ("https://example.test/2", "Two", "Open role", "Open role"),
        ],
    )
    profile = {"personal": {"nationality": "China"}}

    first = refresh_job_eligibility(conn, profile=profile)
    second = refresh_job_eligibility(conn, profile=profile)
    conn.execute(
        "UPDATE jobs SET description = ?, full_description = ? WHERE url = ?",
        (
            "If you are not a Singapore citizen, please do not apply.",
            "If you are not a Singapore citizen, please do not apply.",
            "https://example.test/2",
        ),
    )
    third = refresh_job_eligibility(conn, profile=profile)

    assert first["evaluated"] == 2
    assert second["evaluated"] == 0
    assert second["skipped"] == 2
    assert third["evaluated"] == 1
    assert third["ineligible"] == 1


def test_profile_or_policy_revision_rechecks_cached_jobs() -> None:
    conn = _connection()
    conn.execute(
        "INSERT INTO jobs (url, title, description) VALUES (?, ?, ?)",
        ("https://example.test/1", "One", "Open role"),
    )

    refresh_job_eligibility(conn, profile={}, profile_revision="profile-a", policy_revision="p1")
    profile_changed = refresh_job_eligibility(
        conn, profile={}, profile_revision="profile-b", policy_revision="p1"
    )
    policy_changed = refresh_job_eligibility(
        conn, profile={}, profile_revision="profile-b", policy_revision="p2"
    )

    assert profile_changed["evaluated"] == 1
    assert policy_changed["evaluated"] == 1
