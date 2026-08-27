from __future__ import annotations

from applypilot.view import DATA_PLACEHOLDER, render_dashboard


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
