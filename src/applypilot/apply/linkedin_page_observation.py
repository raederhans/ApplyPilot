"""LinkedIn Apply-click causality, login guard, and external handoff observation."""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from collections.abc import Mapping
from urllib.parse import parse_qs, urlparse

from applypilot.apply.page_surfaces import bound_application_pages
from applypilot.apply.provider_registry import provider_matches_host
from applypilot.storage.job_identity import extract_platform_job_id

logger = logging.getLogger(__name__)

def linkedin_external_handoff_pages(pages: list) -> list:
    """Return unambiguous HTTPS pages outside LinkedIn without inspecting forms."""
    external = []
    for page in pages:
        try:
            parsed = urlparse(str(page.url or "").strip())
        except ValueError:
            continue
        host = (parsed.hostname or "").casefold().rstrip(".")
        if (
            parsed.scheme == "https"
            and host
            and not parsed.username
            and not parsed.password
            and not provider_matches_host(
                "linkedin", host, "linkedin_external_handoff"
            )
        ):
            external.append(page)
    return external


def linkedin_job_id(url: object) -> str:
    """Return only a complete canonical LinkedIn /jobs/view/{id} identity."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    if not provider_matches_host("linkedin", host, "linkedin_external_handoff"):
        return ""
    match = re.fullmatch(r"/jobs/view/([^/]+)/?", parsed.path)
    if not match:
        return ""
    platform_id = extract_platform_job_id(parsed.geturl())
    if platform_id.startswith("linkedin:"):
        return platform_id.split(":", 1)[1]
    # Keep compact synthetic fixtures exact; production identities are parsed
    # through the shared canonical platform-ID parser above.
    return match.group(1) if match.group(1).isdigit() else ""


def linkedin_page_matches_job_id(url: object, expected_job_id: str) -> bool:
    return bool(expected_job_id) and linkedin_job_id(url) == expected_job_id


def linkedin_authwall_redirect_job_id(url: object) -> str:
    """Return the exact job ID bound to a narrow LinkedIn authwall entry.

    ``parse_qs`` performs the one allowed percent-decoding pass.  A second
    decode would turn a double-encoded, non-URL value into an admissible
    redirect and must not be performed here.
    """
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme.casefold() != "https"
        or not provider_matches_host(
            "linkedin", host, "linkedin_external_handoff"
        )
        or parsed.username
        or parsed.password
        or parsed.path.rstrip("/") != "/authwall"
        or parsed.fragment
    ):
        return ""
    redirects = parse_qs(parsed.query, keep_blank_values=True).get(
        "sessionRedirect", []
    )
    if len(redirects) != 1:
        return ""
    redirect = str(redirects[0] or "").strip()
    try:
        target = urlparse(redirect)
    except ValueError:
        return ""
    target_host = (target.hostname or "").casefold().rstrip(".")
    if (
        target.scheme.casefold() != "https"
        or not provider_matches_host(
            "linkedin", target_host, "linkedin_external_handoff"
        )
        or target.username
        or target.password
        or target.fragment
    ):
        return ""
    return linkedin_job_id(redirect)


def target_infos(session) -> dict[str, dict]:
    return {
        str(info.get("targetId") or ""): dict(info)
        for info in session.send("Target.getTargets").get("targetInfos", [])
        if info.get("targetId")
    }


def external_https_target(info: Mapping[str, object]) -> bool:
    try:
        parsed = urlparse(str(info.get("url") or ""))
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return bool(
        parsed.scheme == "https"
        and host
        and not provider_matches_host(
            "linkedin", host, "linkedin_external_handoff"
        )
        and not parsed.username
        and not parsed.password
    )


def classify_linkedin_causal_target(
    before: Mapping[str, Mapping[str, object]],
    after: Mapping[str, Mapping[str, object]],
    *,
    source_target_id: str,
) -> tuple[dict[str, object] | None, str]:
    """Admit only the source target navigation or a newly opened source popup."""
    candidates: list[dict[str, object]] = []
    source_after = after.get(source_target_id)
    if isinstance(source_after, Mapping) and external_https_target(source_after):
        candidates.append(
            {
                "target_id": source_target_id,
                "mode": "same_target_navigation",
                "initial_url": str(before.get(source_target_id, {}).get("url") or ""),
                "final_url": str(source_after.get("url") or ""),
            }
        )
    before_ids = set(before)
    for target_id, info in after.items():
        if target_id in before_ids or not external_https_target(info):
            continue
        if str(info.get("openerId") or info.get("parentId") or "") != source_target_id:
            continue
        candidates.append(
            {
                "target_id": target_id,
                "mode": "new_popup_from_source",
                "initial_url": str(info.get("url") or ""),
                "final_url": str(info.get("url") or ""),
            }
        )
    if len(candidates) != 1:
        return None, "linkedin_apply_click:no_causal_external_target"
    return candidates[0], "linkedin_apply_click:causal_external_target"


def target_id_digest(target_id: object) -> str:
    return hashlib.sha256(str(target_id or "").encode("utf-8")).hexdigest()


def admit_linkedin_causal_events(
    corroborated: Mapping[str, object] | None,
    *,
    source_target_id: str,
    navigation_events: list[str],
    redirect_lineage: list[str],
    popup_event_count: int,
    popup_candidates: list[Mapping[str, object]],
) -> tuple[dict[str, object] | None, str]:
    """Require exactly one Playwright click-epoch event plus target corroboration."""
    if popup_event_count != len(popup_candidates):
        return None, "linkedin_apply_click:target_lost_or_unclassified"
    candidates: list[dict[str, object]] = []
    external_navigations = [
        url for url in navigation_events if external_https_target({"url": url})
    ]
    if (
        external_navigations
        and corroborated is not None
        and corroborated.get("mode") == "same_target_navigation"
        and corroborated.get("target_id") == source_target_id
    ):
        candidates.append(
            {
                **corroborated,
                "final_url": external_navigations[-1],
                "redirect_lineage": redirect_lineage[-12:],
                "lineage_complete": bool(redirect_lineage)
                and len(redirect_lineage) < 12,
            }
        )
    candidates.extend(dict(candidate) for candidate in popup_candidates)
    if len(candidates) > 1:
        return None, "linkedin_apply_click:ambiguous_causal_targets"
    if not candidates:
        return None, "linkedin_apply_click:no_click_epoch_causal_target"
    candidate = candidates[0]
    if candidate.get("lineage_complete") is not True:
        return None, "linkedin_apply_click:redirect_lineage_incomplete"
    return candidate, "linkedin_apply_click:causal_external_target"


def resolve_linkedin_click_epoch(
    causal: Mapping[str, object] | None,
    causal_reason: str,
    *,
    source_job_page_matches: bool,
    login_surface: bool,
    native_surface: bool,
) -> tuple[str | None, str]:
    """Resolve exactly one mutually exclusive outcome from one Apply click epoch."""
    fatal_reasons = {
        "linkedin_apply_click:target_lost_or_unclassified",
        "linkedin_apply_click:ambiguous_causal_targets",
        "linkedin_apply_click:redirect_lineage_incomplete",
    }
    if causal_reason in fatal_reasons:
        return causal_reason, ""

    outcomes: list[str] = []
    if causal is not None:
        outcomes.append("linkedin_external_handoff")
    if source_job_page_matches and login_surface:
        outcomes.append("linkedin_login_required")
    if source_job_page_matches and native_surface:
        outcomes.append("linkedin_native_apply_opened")
    if len(outcomes) > 1:
        return "linkedin_apply_click:ambiguous_click_epoch_results", ""
    if outcomes:
        return None, outcomes[0]
    if not source_job_page_matches:
        return "linkedin_apply_click:returned_job_identity_mismatch", ""
    return causal_reason, ""


def page_target_id(page) -> str:
    try:
        info = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
            "targetInfo"
        ]
    except Exception:  # noqa: BLE001 - a target may detach during navigation
        return ""
    return str(info.get("targetId") or "")


def linkedin_source_page_is_still_exact(
    page,
    *,
    source_target_id: str,
    root_target_ids: set[str],
    source_job_id: str,
) -> bool:
    """Fail closed when a pre-click LinkedIn surface drifts from its bound job."""
    current_target_id = page_target_id(page)
    return bool(
        current_target_id
        and current_target_id == source_target_id
        and current_target_id in root_target_ids
        and linkedin_page_matches_job_id(page.url, source_job_id)
    )


def wait_for_linkedin_main_apply_control(page) -> None:
    """Wait on the observable unique top-card Apply condition, with a hard bound."""
    page.wait_for_function(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const tiers = [
            '.jobs-unified-top-card',
            '.job-details-jobs-unified-top-card__container--two-pane',
            '[data-job-details-top-card]',
            '.top-card-layout__card',
            '.top-card-layout',
          ];
          let topCards = [];
          for (const selector of tiers) {
            topCards = Array.from(document.querySelectorAll(selector)).filter(visible);
            if (topCards.length) break;
          }
          if (topCards.length !== 1) return false;
          const topCard = topCards[0];
          const controls = Array.from(new Set([
            ...topCard.querySelectorAll('.jobs-apply-button--top-card button.jobs-apply-button'),
            ...topCard.querySelectorAll('button.jobs-apply-button'),
            ...topCard.querySelectorAll('a.jobs-apply-button'),
            ...topCard.querySelectorAll('[data-live-test-job-apply-button]'),
            ...topCard.querySelectorAll('.top-card-layout__cta'),
            ...topCard.querySelectorAll('[data-tracking-control-name*="apply"]'),
            ...topCard.querySelectorAll('button[aria-label^="Apply to "]'),
          ])).filter((element) => {
            const label = String(element.getAttribute('aria-label') || element.textContent || '')
              .replace(/\s+/g, ' ').trim();
            const explicitApply = /\b(?:easy\s+)?apply\b/i.test(label)
              || /^(?:立即)?(?:轻松)?申请(?:此职位|职位)?$/.test(label)
              || /^申请.+(?:职位|岗位)$/.test(label);
            return visible(element) && !element.disabled && explicitApply;
          });
          return controls.length === 1;
        }""",
        timeout=10_000,
    )


def linkedin_main_apply_handle(page):
    """Return the exact unique top-card Apply element as a Playwright handle."""
    handle = page.evaluate_handle(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const tiers = [
            '.jobs-unified-top-card',
            '.job-details-jobs-unified-top-card__container--two-pane',
            '[data-job-details-top-card]',
            '.top-card-layout__card',
            '.top-card-layout',
          ];
          let topCards = [];
          for (const selector of tiers) {
            topCards = Array.from(document.querySelectorAll(selector)).filter(visible);
            if (topCards.length) break;
          }
          if (topCards.length !== 1) return null;
          const topCard = topCards[0];
          const controls = Array.from(new Set([
            ...topCard.querySelectorAll('.jobs-apply-button--top-card button.jobs-apply-button'),
            ...topCard.querySelectorAll('button.jobs-apply-button'),
            ...topCard.querySelectorAll('a.jobs-apply-button'),
            ...topCard.querySelectorAll('[data-live-test-job-apply-button]'),
            ...topCard.querySelectorAll('.top-card-layout__cta'),
            ...topCard.querySelectorAll('[data-tracking-control-name*="apply"]'),
            ...topCard.querySelectorAll('button[aria-label^="Apply to "]'),
          ])).filter((element) => {
            const label = String(element.getAttribute('aria-label') || element.textContent || '')
              .replace(/\s+/g, ' ').trim();
            const explicitApply = /\b(?:easy\s+)?apply\b/i.test(label)
              || /^(?:立即)?(?:轻松)?申请(?:此职位|职位)?$/.test(label)
              || /^申请.+(?:职位|岗位)$/.test(label);
            return visible(element) && !element.disabled && explicitApply;
          });
          return controls.length === 1 ? controls[0] : null;
        }"""
    )
    return handle.as_element()


def linkedin_app_promo_dismiss_handle(page):
    """Return only the exact non-application LinkedIn app-promotion dismiss control."""
    handle = page.evaluate_handle(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const dialogs = Array.from(document.querySelectorAll(
            '[role="dialog"].cta-modal, .cta-modal[role="dialog"]'
          )).filter(visible);
          if (dialogs.length !== 1) return null;
          const dialog = dialogs[0];
          const text = String(dialog.textContent || '').replace(/\s+/g, ' ').trim();
          if (!/LinkedIn is better on the app/i.test(text)) return null;
          if (dialog.querySelector('form, input, textarea, select')) return null;
          const controls = Array.from(dialog.querySelectorAll(
            'button, a, [role="button"]'
          )).filter((element) => {
            const label = String(
              element.getAttribute('aria-label') || element.textContent || ''
            ).replace(/\s+/g, ' ').trim();
            return visible(element) && /^Dismiss$/i.test(label);
          });
          return controls.length === 1 ? controls[0] : null;
        }"""
    )
    return handle.as_element()


def dismiss_linkedin_app_promo(page) -> bool:
    """Dismiss one verified app-promotion modal before the Apply click epoch."""
    control = linkedin_app_promo_dismiss_handle(page)
    if control is None:
        return False
    control.click(timeout=5_000)
    page.wait_for_function(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          return !Array.from(document.querySelectorAll(
            '[role="dialog"].cta-modal, .cta-modal[role="dialog"]'
          )).some(visible);
        }""",
        timeout=5_000,
    )
    return True


def linkedin_click_page_state(page) -> dict[str, object]:
    result = page.evaluate(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const dialogs = Array.from(document.querySelectorAll(
            '[role="dialog"], .artdeco-modal, [data-test-modal]'
          )).filter(visible);
          const dialogText = dialogs.map((dialog) => String(dialog.textContent || '')
            .replace(/\s+/g, ' ').trim()).join(' ').slice(0, 1000);
          const login = /continue\s+with\s+google|sign\s+in|log\s+in|通过\s*Google\s*继续|登录/i
            .test(dialogText)
            || /\/authwall\/?$|\/login\b|\/uas\/login\b/.test(location.pathname);
          const nativeApply = dialogs.some((dialog) =>
            dialog.querySelector('form, input, textarea, select, button[aria-label*="Submit"]')
          ) && /easy\s+apply|轻松申请|申请/i.test(dialogText);
          return {login, native_apply: nativeApply, page_url: location.href};
        }"""
    )
    return dict(result) if isinstance(result, Mapping) else {}


def click_linkedin_main_apply_causally(
    port: int, worker_id: int, job: dict
) -> tuple[str | None, dict]:
    """Perform one launcher-owned Apply click and attest only its direct target."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    del worker_id
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        bound_pages = bound_application_pages(browser, pages, job)
        if linkedin_external_handoff_pages(bound_pages):
            return "linkedin_apply_click:preexisting_external_target", {}
        source_job_id = linkedin_job_id(job.get("application_url") or job.get("url"))
        if not source_job_id:
            return "linkedin_apply_click:source_job_id_missing", {}
        candidates = [
            page
            for page in bound_pages
            if linkedin_page_matches_job_id(page.url, source_job_id)
        ]
        if not candidates:
            authwall_candidates = [
                page
                for page in bound_pages
                if linkedin_authwall_redirect_job_id(page.url) == source_job_id
            ]
            if len(bound_pages) == 1 and len(authwall_candidates) == 1:
                page = authwall_candidates[0]
                source_target_id = page_target_id(page)
                roots = set(job.get("_browser_root_target_ids") or [])
                if not source_target_id or source_target_id not in roots:
                    return "linkedin_apply_click:source_root_mismatch", {}
                session = browser.new_browser_cdp_session()
                job["_linkedin_login_baseline_target_ids"] = sorted(
                    target_infos(session)
                )
                job["_linkedin_login_source_job_id"] = source_job_id
                job["_linkedin_login_entry_stage"] = "pre_entry_authwall"
                return (
                    None,
                    {
                        "disposition": "linkedin_login_required",
                        "stage": "pre_entry_authwall",
                    },
                )
        if len(candidates) != 1:
            return f"linkedin_apply_click:exact_job_page_count:{len(candidates)}", {}
        page = candidates[0]
        source_target_id = page_target_id(page)
        roots = set(job.get("_browser_root_target_ids") or [])
        if not source_target_id or source_target_id not in roots:
            return "linkedin_apply_click:source_root_mismatch", {}
        wait_for_linkedin_main_apply_control(page)
        dismiss_linkedin_app_promo(page)
        if not linkedin_source_page_is_still_exact(
            page,
            source_target_id=source_target_id,
            root_target_ids=roots,
            source_job_id=source_job_id,
        ):
            return "linkedin_apply_click:source_changed_after_app_promo", {}
        session = browser.new_browser_cdp_session()
        before = target_infos(session)
        pre_click_state = linkedin_click_page_state(page)
        if pre_click_state.get("login") is True:
            job["_linkedin_login_baseline_target_ids"] = sorted(before)
            job["_linkedin_login_source_job_id"] = source_job_id
            job["_linkedin_login_entry_stage"] = "pre_entry_login_dialog"
            return (
                None,
                {
                    "disposition": "linkedin_login_required",
                    "stage": "pre_entry_login_dialog",
                },
            )
        control = linkedin_main_apply_handle(page)
        if control is None:
            return "linkedin_apply_click:main_apply_not_unique", {}

        popup_events: list[object] = []
        popup_redirects: dict[int, list[str]] = {}
        navigation_events: list[str] = []
        redirect_lineage: list[str] = []

        def on_popup(popup) -> None:
            popup_events.append(popup)
            popup_redirects[id(popup)] = []

            def on_popup_request(request) -> None:
                is_navigation = request.is_navigation_request
                if callable(is_navigation):
                    is_navigation = is_navigation()
                if not is_navigation or request.frame != popup.main_frame:
                    return
                chain = []
                current = request
                while current is not None and len(chain) < 12:
                    chain.append(str(current.url or "")[:2000])
                    current = current.redirected_from
                popup_redirects[id(popup)] = list(reversed(chain))

            popup.on("request", on_popup_request)

        def on_navigation(frame) -> None:
            if frame == page.main_frame:
                navigation_events.append(str(frame.url or "")[:2000])

        def on_request(request) -> None:
            is_navigation = request.is_navigation_request
            if callable(is_navigation):
                is_navigation = is_navigation()
            if not is_navigation or request.frame != page.main_frame:
                return
            chain = []
            current = request
            while current is not None and len(chain) < 12:
                chain.append(str(current.url or "")[:2000])
                current = current.redirected_from
            redirect_lineage[:] = list(reversed(chain))

        page.on("popup", on_popup)
        page.on("framenavigated", on_navigation)
        page.on("request", on_request)
        if not linkedin_source_page_is_still_exact(
            page,
            source_target_id=source_target_id,
            root_target_ids=roots,
            source_job_id=source_job_id,
        ):
            return "linkedin_apply_click:source_changed_before_apply", {}
        control.click(timeout=10_000)

        after = target_infos(session)
        corroborated, reason = classify_linkedin_causal_target(
            before, after, source_target_id=source_target_id
        )
        if corroborated is None and not popup_events and not navigation_events:
            try:
                page.wait_for_function(
                    r"""() => {
                      const host = location.hostname.toLowerCase().replace(/\.$/, '');
                      if (host !== 'linkedin.com' && !host.endsWith('.linkedin.com')) return true;
                      const visible = (element) => {
                        const style = getComputedStyle(element);
                        const rect = element.getBoundingClientRect();
                        return style.visibility !== 'hidden' && style.display !== 'none'
                          && rect.width > 0 && rect.height > 0;
                      };
                      return Array.from(document.querySelectorAll(
                        '[role="dialog"], .artdeco-modal, [data-test-modal]'
                      )).some(visible) || /\/login\b|\/uas\/login\b/.test(location.pathname);
                    }""",
                    timeout=10_000,
                )
            except PlaywrightTimeoutError:
                pass
            after = target_infos(session)
            corroborated, reason = classify_linkedin_causal_target(
                before, after, source_target_id=source_target_id
            )
        popup_candidates: list[dict[str, object]] = []
        for popup in popup_events:
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
            try:
                popup_target_id = page_target_id(popup)
                popup_url = str(popup.url or "")[:2000]
                popup_info = target_infos(session).get(popup_target_id, {})
                classified = bool(
                    popup_target_id
                    and external_https_target({"url": popup_url})
                    and popup_target_id not in before
                    and str(
                        popup_info.get("openerId")
                        or popup_info.get("parentId")
                        or ""
                    )
                    == source_target_id
                )
                if not classified:
                    continue
                popup_lineage = popup_redirects.get(id(popup)) or [popup_url]
                popup_candidates.append(
                    {
                        "target_id": popup_target_id,
                        "mode": "new_popup_from_source",
                        "initial_url": popup_lineage[0],
                        "final_url": popup_url,
                        "redirect_lineage": popup_lineage[-12:],
                        "lineage_complete": len(popup_lineage) < 12,
                    }
                )
            except Exception:
                logger.debug(
                    "LinkedIn click popup detached before classification",
                    exc_info=True,
                )
                continue
        causal, causal_reason = admit_linkedin_causal_events(
            corroborated,
            source_target_id=source_target_id,
            navigation_events=navigation_events,
            redirect_lineage=redirect_lineage,
            popup_event_count=len(popup_events),
            popup_candidates=popup_candidates,
        )
        fatal_signal, _ = resolve_linkedin_click_epoch(
            causal,
            causal_reason,
            source_job_page_matches=False,
            login_surface=False,
            native_surface=False,
        )
        if fatal_signal in {
            "linkedin_apply_click:target_lost_or_unclassified",
            "linkedin_apply_click:ambiguous_causal_targets",
            "linkedin_apply_click:redirect_lineage_incomplete",
        }:
            return fatal_signal, {}

        source_page_matches = linkedin_page_matches_job_id(page.url, source_job_id)
        authwall_matches = (
            linkedin_authwall_redirect_job_id(page.url) == source_job_id
        )
        source_surface_matches = source_page_matches or authwall_matches
        state = linkedin_click_page_state(page) if source_surface_matches else {}
        epoch_signal, disposition = resolve_linkedin_click_epoch(
            causal,
            causal_reason,
            source_job_page_matches=source_surface_matches,
            login_surface=state.get("login") is True,
            native_surface=state.get("native_apply") is True,
        )
        if epoch_signal:
            return epoch_signal, {}

        if disposition == "linkedin_external_handoff" and causal is not None:
            attestation_id = uuid.uuid4().hex
            target_id = str(causal["target_id"])
            final_info = after.get(target_id, {})
            causal["final_url"] = str(final_info.get("url") or causal["final_url"])
            job["_linkedin_causal_apply_attestation"] = {
                "version": 1,
                "attestation_id": attestation_id,
                "source_job_id": source_job_id,
                "source_target_id": source_target_id,
                "target_id": target_id,
                "target_id_digest": target_id_digest(target_id),
                "mode": causal["mode"],
                "initial_url": causal["initial_url"],
                "final_url": causal["final_url"],
                "redirect_lineage": list(causal.get("redirect_lineage") or [])[:12],
                "lineage_complete": causal.get("lineage_complete") is True,
                "before_target_ids": sorted(before),
            }
            return (
                None,
                {
                    "disposition": disposition,
                    "page_url": causal["final_url"],
                },
            )
        if disposition == "linkedin_login_required":
            job["_linkedin_login_baseline_target_ids"] = sorted(after)
            job["_linkedin_login_source_job_id"] = source_job_id
            job["_linkedin_login_entry_stage"] = (
                "post_apply_authwall"
                if authwall_matches
                else "post_apply_login_dialog"
            )
            return None, {
                "disposition": disposition,
                "stage": job["_linkedin_login_entry_stage"],
            }
        if disposition == "linkedin_native_apply_opened":
            return None, {"disposition": disposition}
        return reason, {}
    except Exception as exc:
        logger.exception("LinkedIn launcher-owned Apply click failed")
        return f"linkedin_apply_click:{type(exc).__name__}", {}
    finally:
        playwright.stop()


def verify_linkedin_post_login_state(
    port: int, worker_id: int, job: dict
) -> tuple[bool, str]:
    """Admit a login turn only when it returns cleanly to the exact source job."""
    from playwright.sync_api import sync_playwright

    del worker_id
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        bound_pages = bound_application_pages(browser, pages, job)
        if linkedin_external_handoff_pages(bound_pages):
            return False, "linkedin_login_guard:external_target_created"
        source_job_id = str(job.get("_linkedin_login_source_job_id") or "")
        candidates = [
            page
            for page in bound_pages
            if linkedin_page_matches_job_id(page.url, source_job_id)
        ]
        if len(candidates) != 1:
            return False, "linkedin_login_guard:exact_job_not_restored"
        session = browser.new_browser_cdp_session()
        current_ids = set(target_infos(session))
        baseline_ids = set(job.get("_linkedin_login_baseline_target_ids") or [])
        unexpected = current_ids - baseline_ids
        if unexpected:
            return False, "linkedin_login_guard:unexpected_target_created"
        state = linkedin_click_page_state(candidates[0])
        if state.get("login") is True:
            return False, "linkedin_login_guard:login_not_completed"
        if state.get("native_apply") is True:
            return False, "linkedin_login_guard:agent_opened_native_apply"
        wait_for_linkedin_main_apply_control(candidates[0])
        return True, "linkedin_login_guard:verified"
    except Exception as exc:
        logger.exception("LinkedIn post-login guard failed")
        return False, f"linkedin_login_guard:{type(exc).__name__}"
    finally:
        playwright.stop()


def linkedin_external_page_identity(page) -> dict[str, object]:
    """Read only bounded, non-form text that identifies the external posting."""
    raw = page.evaluate(
        r"""() => {
          const visible = (element) => {
            const style = getComputedStyle(element);
            const rect = element.getBoundingClientRect();
            return style.visibility !== 'hidden' && style.display !== 'none'
              && rect.width > 0 && rect.height > 0;
          };
          const compact = (value) => String(value || '').replace(/\s+/g, ' ').trim();
          const headings = Array.from(document.querySelectorAll('h1, h2, [role="heading"]'))
            .filter(visible)
            .map((element) => compact(element.textContent))
            .filter(Boolean);
          return {
            page_title: compact(document.title),
            primary_headings: headings,
          };
        }"""
    )
    if not isinstance(raw, dict):
        raw = {}
    page_title = " ".join(str(raw.get("page_title") or "").split())[:300]
    raw_headings = raw.get("primary_headings")
    headings = (
        [" ".join(str(value).split())[:300] for value in raw_headings[:6]]
        if isinstance(raw_headings, list)
        else []
    )
    return {
        "version": 1,
        "page_title": page_title,
        "primary_headings": [value for value in headings if value],
    }


def observe_linkedin_external_handoff_page(
    port: int, worker_id: int, job: dict
) -> tuple[str | None, dict]:
    """Observe only the launcher-attested causal Apply target."""
    from playwright.sync_api import sync_playwright

    del worker_id  # reserved for parity with the other observer ports
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        attestation = job.get("_linkedin_causal_apply_attestation")
        if not isinstance(attestation, Mapping) or attestation.get("version") != 1:
            return "linkedin_handoff_observer:causal_attestation_required", {}
        target_id = str(attestation.get("target_id") or "")
        if (
            not target_id
            or attestation.get("target_id_digest") != target_id_digest(target_id)
        ):
            return "linkedin_handoff_observer:causal_attestation_invalid", {}
        pages = [page for context in browser.contexts for page in context.pages]
        page = next((page for page in pages if page_target_id(page) == target_id), None)
        if page is None:
            return "linkedin_handoff_observer:attested_target_missing", {}
        parsed = urlparse(str(page.url or ""))
        if not external_https_target({"url": parsed.geturl()}):
            return "linkedin_handoff_observer:attested_target_not_external", {}
        page.bring_to_front()
        page_identity = linkedin_external_page_identity(page)
        final_url = str(page.url or "")[:2000]
        redirect_lineage = [
            str(value)[:2000]
            for value in list(attestation.get("redirect_lineage") or [])[:12]
        ]
        if final_url and (not redirect_lineage or redirect_lineage[-1] != final_url):
            if len(redirect_lineage) >= 12:
                return "linkedin_handoff_observer:redirect_lineage_overflow", {}
            redirect_lineage.append(final_url)
        attestation["final_url"] = final_url
        attestation["redirect_lineage"] = redirect_lineage
        attestation_evidence = {
            "version": 1,
            "verified": True,
            "attestation_id_digest": hashlib.sha256(
                str(attestation.get("attestation_id") or "").encode("utf-8")
            ).hexdigest(),
            "sourcetarget_id_digest": target_id_digest(
                attestation.get("source_target_id")
            ),
            "target_id_digest": target_id_digest(target_id),
            "mode": str(attestation.get("mode") or ""),
            "initial_url": str(attestation.get("initial_url") or "")[:2000],
            "final_url": final_url,
            "redirect_lineage": redirect_lineage,
            "lineage_complete": attestation.get("lineage_complete") is True,
        }
        return (
            None,
            {
                "status": "attention",
                "disposition": "linkedin_external_handoff",
                "page_url": str(page.url or "").strip(),
                "page_identity": page_identity,
                "causal_apply_attestation": attestation_evidence,
                "submit_control_count": 0,
            },
        )
    except Exception as exc:
        logger.exception("LinkedIn external handoff observation failed")
        return f"linkedin_handoff_observer:{type(exc).__name__}", {}
    finally:
        playwright.stop()
