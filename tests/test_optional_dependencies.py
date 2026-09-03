from __future__ import annotations

import importlib
import sys

import pytest

from applypilot.optional_dependencies import (
    OptionalDependencyError,
    require_jobboards,
    require_module,
)

pytestmark = pytest.mark.compatibility


def test_optional_dependency_error_names_installable_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_import(_module_name: str):
        raise ModuleNotFoundError("missing optional package", name="jobspy")

    monkeypatch.setattr(importlib, "import_module", fail_import)

    with pytest.raises(OptionalDependencyError, match=r"applypilot-local\[jobboards\]"):
        require_module(
            "jobspy",
            extra="jobboards",
            purpose="Broad third-party job-board discovery",
        )


def test_jobspy_storage_uses_record_conversion_instead_of_series_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from applypilot.discovery import jobspy

    class RecordsOnlyFrame:
        def to_dict(self, *, orient: str):
            assert orient == "records"
            return [
                {
                    "job_url": "https://example.test/jobs/1",
                    "title": "Data Analyst",
                    "company": "Example",
                    "location": "Singapore",
                    "description": "Verified data workflow " * 20,
                    "site": "linkedin",
                    "job_url_direct": "https://example.test/apply/1",
                }
            ]

        def iterrows(self):
            raise AssertionError("the ingestion hot path must not construct per-row Series")

    captured: list[dict] = []

    def fake_store(_conn, rows, _site, _strategy):
        captured.extend(rows)
        return len(rows), 0

    monkeypatch.setattr(jobspy, "store_jobs", fake_store)

    assert jobspy.store_jobspy_results(object(), RecordsOnlyFrame(), "linkedin") == (1, 0)
    assert captured[0]["company_name"] == "Example"


def test_jobspy_discovery_fails_before_expanding_search_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from applypilot.discovery import jobspy

    def fail_dependency(*_args, **_kwargs):
        raise OptionalDependencyError("install the jobboards extra")

    monkeypatch.setattr(jobspy, "require_jobboards", fail_dependency)
    monkeypatch.setattr(
        jobspy,
        "_full_crawl",
        lambda **_kwargs: pytest.fail("search matrix should not start"),
    )

    with pytest.raises(OptionalDependencyError, match="jobboards extra"):
        jobspy.run_discovery({"queries": [{"query": "analyst"}]})


def test_jobboards_error_explains_python_313_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 13, 0))

    with pytest.raises(OptionalDependencyError, match=r"Python 3\.11-3\.12"):
        require_jobboards()
