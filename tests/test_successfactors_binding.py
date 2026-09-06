from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from applypilot.apply import launcher
from applypilot.apply.successfactors_binding import (
    parse_successfactors_public_job_page,
    resolve_successfactors_application_binding,
    successfactors_probe_candidate,
)

TEMASEK_URL = (
    "https://jobs.temasek.com.sg/job/Data-Engineer-Intern%2C-Technology-"
    "%28Jan-Jun-2027%29-238891/1369169257/"
)
NTUC_URL = "https://careers.ntuchealth.sg/job/Intern%2C-Data-Analyst/959-en_GB"
SGX_URL = "https://careers.sgx.com/job/Singapore-Intern-Data/1358606266/"


def _public_html(
    *,
    posting_id: str,
    req_id: str,
    company: str,
    sso_url: str,
) -> str:
    return f"""
        <!doctype html><html><head><script>
        window.config = {{
          "ssoCompanyId" : '{company}',
          "ssoUrl" : '{sso_url}'
        }};
        </script></head><body>
        <span class="joblayouttoken-label">Req ID:</span>
        <span>{req_id}</span>
        <a href="/talentcommunity/apply/{posting_id}/?locale=en_GB">Apply now</a>
        <a href="/talentcommunity/apply/{posting_id}/?locale=en_GB">Apply now</a>
        </body></html>
    """


@pytest.mark.parametrize(
    ("source_url", "posting_id", "req_id", "company", "sso_url"),
    [
        (
            TEMASEK_URL,
            "1369169257",
            "12189",
            "temasekcapP2",
            "https://career2.successfactors.eu",
        ),
        (
            NTUC_URL,
            "959",
            "959",
            "ntuchealth",
            "https://career44.sapsf.com",
        ),
        (
            SGX_URL,
            "1358606266",
            "3194",
            "SGX",
            "https://career10.successfactors.com",
        ),
    ],
)
@pytest.mark.parametrize("label", ["Req ID:", "Requisition ID:"])
def test_official_jobs2web_dom_resolves_exact_safe_tuple(
    source_url: str,
    posting_id: str,
    req_id: str,
    company: str,
    sso_url: str,
    label: str,
) -> None:
    binding = parse_successfactors_public_job_page(
        source_url,
        _public_html(
            posting_id=posting_id,
            req_id=req_id,
            company=company,
            sso_url=sso_url,
        ).replace("Req ID:", label),
    )

    assert binding == {
        "provider": "successfactors",
        "resolved": True,
        "source_url": source_url.rstrip("/") + ("/" if source_url.endswith("/") else ""),
        "source_host": source_url.split("/", 3)[2],
        "source_posting_id": posting_id,
        "public_apply_url": (
            f"https://{source_url.split('/', 3)[2]}"
            f"/talentcommunity/apply/{posting_id}/"
        ),
        "ats_host": sso_url.removeprefix("https://"),
        "company": company,
        "job_req_id": req_id,
        "job_application_url": (
            f"{sso_url}/career?company={company}&career_ns=job_application"
            f"&career_job_req_id={req_id}"
        ),
        "evidence_kind": "official_jobs2web_dom",
    }


def test_resolver_uses_only_transport_html_and_rejects_redirect_identity_drift() -> None:
    html = _public_html(
        posting_id="1369169257",
        req_id="12189",
        company="temasekcapP2",
        sso_url="https://career2.successfactors.eu",
    )
    exact = resolve_successfactors_application_binding(
        {"url": TEMASEK_URL, "full_description": "Req ID: 12189"},
        transport=lambda url: {"status_code": 200, "final_url": url, "html": html},
    )
    drifted = resolve_successfactors_application_binding(
        {"url": TEMASEK_URL, "full_description": "Req ID: 12189"},
        transport=lambda _url: {
            "status_code": 200,
            "final_url": "https://jobs.temasek.com.sg/job/other/999/",
            "html": html,
        },
    )

    assert exact and exact["resolved"] is True
    assert drifted == {
        "provider": "successfactors",
        "resolved": False,
        "reason": "source_redirect_identity_mismatch",
    }


@pytest.mark.parametrize(
    ("url", "description"),
    [
        (TEMASEK_URL, "Req ID: 12189"),
        (NTUC_URL, "Req ID: 959"),
        (
            SGX_URL,
            "Requisition ID: 3194",
        ),
    ],
)
def test_probe_requires_numeric_jobs2web_route_and_unique_requisition(
    url: str,
    description: str,
) -> None:
    assert successfactors_probe_candidate(
        {"url": url, "application_url": url, "full_description": description}
    )
    assert not successfactors_probe_candidate(
        {"url": url, "application_url": url, "full_description": "No requisition"}
    )
    assert not successfactors_probe_candidate(
        {
            "url": url,
            "application_url": url,
            "full_description": f"{description}\nReq ID: 999",
        }
    )


def test_generic_job_route_without_requisition_signal_is_not_fetched() -> None:
    calls: list[str] = []

    binding = resolve_successfactors_application_binding(
        {"url": "https://jobs.example.test/job/data-role/123/"},
        transport=lambda url: calls.append(url) or {},
    )

    assert binding is None
    assert calls == []


def test_launcher_target_keeps_exact_sf_tuple_and_drops_opaque_query() -> None:
    job_application_url = (
        "https://career2.successfactors.eu/career?company=temasekcapP2"
        "&career_ns=job_application&career_job_req_id=12189"
        "&requestParams=opaque%2Froute&_s.crb=opaque-token"
    )
    routes = launcher._credential_target_urls(
        {
            "url": TEMASEK_URL,
            "application_url": TEMASEK_URL,
            "_ats_application_binding": {
                "provider": "successfactors",
                "resolved": True,
                "ats_host": "career2.successfactors.eu",
                "company": "temasekcapP2",
                "job_req_id": "12189",
                "job_application_url": job_application_url,
            },
        },
        attempt_id="attempt-sf",
    )

    target = next(
        route for route in routes if urlparse(route).hostname == "career2.successfactors.eu"
    )
    assert parse_qs(urlparse(target).query) == {
        "career_job_req_id": ["12189"],
        "career_ns": ["job_application"],
        "company": ["temasekcapP2"],
    }
    assert "requestParams" not in " ".join(routes)
    assert "_s.crb" not in " ".join(routes)


@pytest.mark.parametrize(
    "final_url",
    [
        "https://evil.test/job/other/1369169257/",
        "https://jobs.temasek.com.sg/job/other/1369169257/",
    ],
)
def test_resolver_rejects_same_numeric_id_on_other_origin_or_path(
    final_url: str,
) -> None:
    html = _public_html(
        posting_id="1369169257",
        req_id="12189",
        company="temasekcapP2",
        sso_url="https://career2.successfactors.eu",
    )

    binding = resolve_successfactors_application_binding(
        {"url": TEMASEK_URL, "full_description": "Req ID: 12189"},
        transport=lambda _url: {
            "status_code": 200,
            "final_url": final_url,
            "html": html,
        },
    )

    assert binding == {
        "provider": "successfactors",
        "resolved": False,
        "reason": "source_redirect_identity_mismatch",
    }


@pytest.mark.parametrize(
    ("source_url", "html", "reason"),
    [
        (
            TEMASEK_URL,
            _public_html(
                posting_id="999",
                req_id="12189",
                company="temasekcapP2",
                sso_url="https://career2.successfactors.eu",
            ),
            "public_apply_identity_mismatch",
        ),
        (
            TEMASEK_URL,
            _public_html(
                posting_id="1369169257",
                req_id="12189",
                company="temasekcapP2",
                sso_url="http://career2.successfactors.eu",
            ),
            "sso_url_identity_invalid",
        ),
        (
            TEMASEK_URL,
            _public_html(
                posting_id="1369169257",
                req_id="12189",
                company="temasekcapP2",
                sso_url="https://successfactors.eu.evil.test",
            ),
            "sso_url_identity_invalid",
        ),
        (
            TEMASEK_URL,
            _public_html(
                posting_id="1369169257",
                req_id="12189",
                company="temasekcapP2",
                sso_url="https://user@career2.successfactors.eu",
            ),
            "sso_url_identity_invalid",
        ),
        (
            TEMASEK_URL,
            _public_html(
                posting_id="1369169257",
                req_id="12189",
                company="temasekcapP2",
                sso_url="https://career2.successfactors.eu/%2fother",
            ),
            "sso_url_identity_invalid",
        ),
        (
            TEMASEK_URL,
            _public_html(
                posting_id="1369169257",
                req_id="12189",
                company="temasekcapP2",
                sso_url="https://career2.successfactors.eu",
            ).replace(
                "\"ssoCompanyId\" : 'temasekcapP2',",
                "\"ssoCompanyId\" : 'temasekcapP2', ssoCompanyId: 'other',",
            ),
            "sso_company_identity_invalid",
        ),
    ],
)
def test_public_binding_rejects_ambiguous_or_unsafe_identity(
    source_url: str,
    html: str,
    reason: str,
) -> None:
    assert parse_successfactors_public_job_page(source_url, html) == {
        "provider": "successfactors",
        "resolved": False,
        "reason": reason,
    }
