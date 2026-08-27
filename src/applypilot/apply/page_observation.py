"""Browser-page observation and application form audit contracts.

This module owns browser-derived facts only.  It does not acquire jobs, mutate
application ledgers, launch workers, or decide batch progress.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlparse

from applypilot import config
from applypilot.apply import prompt as prompt_mod

logger = logging.getLogger(__name__)


def _application_fact_value(profile: dict, key: str) -> object | None:
    """Return the newest confirmed profile fact for one stable key."""
    for fact in reversed(profile.get("application_facts", [])):
        if isinstance(fact, dict) and str(fact.get("key") or "").strip() == key:
            return fact.get("value")
    return None


def _yes_no_value(value: object) -> bool | None:
    text = str(value or "").strip().casefold()
    if re.match(r"^(?:yes|true)\b", text):
        return True
    if re.match(r"^(?:no|false|none|not applicable|n/?a)\b", text):
        return False
    return None


def _selected_matches_boolean(selected: object, expected: bool) -> bool:
    text = " ".join(str(selected or "").strip().casefold().split())
    if expected:
        return bool(re.match(r"^(?:yes|true)\b", text))
    return bool(
        re.match(r"^(?:no|false|none|neither|not applicable|n/?a)\b", text)
        or "none of the above" in text
        or "citizen of a different country" in text
    )


def _work_authorization_answers(profile: dict, job: dict) -> tuple[bool, bool] | None:
    """Return (authorized, sponsorship-needed) for a clearly classified role."""
    policy = profile.get("work_authorization", {}).get("form_answer_policy", {})
    job_text = " ".join(
        str(job.get(field) or "").casefold()
        for field in ("title", "full_description", "application_readiness_reason")
    )
    branch = None
    if "intern" in job_text:
        if "non-credit" in job_text or "part-time" in job_text:
            branch = policy.get("non_credit_internship")
        branch = branch or policy.get("programme_credit_bearing_internship")
    elif any(term in job_text for term in ("full-time", "full time", "permanent")):
        branch = policy.get("post_graduation_full_time")
    if not isinstance(branch, dict):
        return None
    authorized = _yes_no_value(branch.get("legally_authorized"))
    sponsorship = _yes_no_value(branch.get("requires_sponsorship"))
    if authorized is None or sponsorship is None:
        return None
    return authorized, sponsorship


def _expected_screening_answer(
    question: object, profile: dict, job: dict
) -> tuple[str, bool] | None:
    """Map common legal/screening questions to confirmed, contextual facts."""
    text = " ".join(str(question or "").casefold().split())
    if not text:
        return None

    if re.search(r"\bf[\s-]?1\b|\bcpt\b|\bopt\b", text):
        expected = _yes_no_value(_application_fact_value(profile, "f1_student_status"))
        return ("f1_student_status", expected) if expected is not None else None
    if re.search(r"\bu\.?s\.? person\b|\bunited states person\b", text):
        expected = _yes_no_value(
            _application_fact_value(profile, "united_states_person_status")
        )
        return ("united_states_person_status", expected) if expected is not None else None

    work_answers = _work_authorization_answers(profile, job)
    if re.search(r"sponsor|sponsorship", text) and work_answers is not None:
        return "requires_sponsorship", work_answers[1]
    if re.search(
        r"(?:authori[sz]ed|legal(?:ly)? (?:eligible|entitled)|right) to work",
        text,
    ) and work_answers is not None:
        return "legally_authorized_to_work", work_answers[0]

    company = re.sub(r"[^a-z0-9]+", " ", str(job.get("company_name") or "").casefold()).strip()
    employer_question = re.search(r"\b(previously|ever)\b.*\b(worked|employed)\b", text)
    if employer_question and (not company or company in re.sub(r"[^a-z0-9]+", " ", text)):
        preserved = {
            re.sub(r"[^a-z0-9]+", " ", str(name).casefold()).strip()
            for name in profile.get("resume_facts", {}).get("preserved_companies", [])
        }
        return "previously_worked_for_target_employer", company in preserved

    if re.search(r"non[ -]?compete|non[ -]?solicitation|contractual .*restrict|legal .*restrict", text):
        value = _application_fact_value(
            profile, "employment_or_non_compete_restrictions"
        ) or profile.get("screening", {}).get("employment_or_non_compete_restrictions")
        expected = _yes_no_value(value)
        return ("employment_or_non_compete_restrictions", expected) if expected is not None else None
    if re.search(r"criminal|convict", text):
        value = _application_fact_value(
            profile, "criminal_convictions_to_disclose"
        ) or profile.get("screening", {}).get("criminal_convictions_to_disclose")
        expected = _yes_no_value(value)
        return ("criminal_convictions_to_disclose", expected) if expected is not None else None
    if "background check" in text:
        expected = _yes_no_value(
            profile.get("screening", {}).get("willing_to_complete_background_check")
        )
        return ("background_check", expected) if expected is not None else None
    return None


def _visible_captcha_overlay(page) -> bool:
    """Detect a user-visible CAPTCHA challenge without reading its contents."""
    for iframe in page.locator("iframe").all():
        try:
            title = (iframe.get_attribute("title") or "").casefold()
            source = (iframe.get_attribute("src") or "").casefold()
            if not iframe.is_visible():
                continue
            box = iframe.bounding_box()
            if (
                box
                and box["width"] >= 200
                and box["height"] >= 150
                and ("captcha" in title or "captcha" in source)
            ):
                return True
        except Exception:
            logger.debug("Unable to inspect a CAPTCHA iframe", exc_info=True)
            continue
    return False


def _captcha_response_present(page) -> bool:
    """Return true after the applicant has produced a CAPTCHA response token."""
    selector = (
        'textarea[name*="captcha" i], textarea[name*="recaptcha" i], '
        'input[name*="captcha" i], input[name*="recaptcha" i]'
    )
    try:
        response_fields = page.locator(selector).all()
    except Exception:
        logger.debug("Unable to enumerate CAPTCHA response fields", exc_info=True)
        return False
    for field in response_fields:
        try:
            if (field.input_value(timeout=500) or "").strip():
                return True
        except Exception:
            logger.debug("Unable to read a CAPTCHA response field", exc_info=True)
            continue
    return False


def _visible_verification_gate(page) -> bool:
    """Detect a visible CAPTCHA or email/OTP gate without reading its value."""
    if _visible_captcha_overlay(page):
        return True
    try:
        return bool(page.evaluate(
            r"""() => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  el.getClientRects().length > 0;
              };
              const verification = /security code|verification code|one[- ]time (?:code|password)|\botp\b|verify (?:your )?email|email verification|验证码|校验码|验证邮箱/i;
              const inputs = [...document.querySelectorAll('input')].filter(visible);
              const codeInputs = inputs.filter((el) => {
                const maxLength = Number(el.maxLength || 0);
                return maxLength === 1 || /otp|verification|security.?code/i.test(
                  `${el.name || ''} ${el.id || ''} ${el.autocomplete || ''}`
                );
              });
              if (codeInputs.length < 4) return false;
              return [...document.querySelectorAll('form,section,dialog,[role="dialog"]')]
                .filter(visible).some((el) => verification.test(el.innerText || ''));
            }"""
        ))
    except Exception:
        logger.debug("Unable to inspect a verification gate", exc_info=True)
        return False


def _verification_clear_state_stable(page) -> bool:
    """Require a normal form or receipt after a gate disappears, not a blank page."""
    try:
        return bool(
            page.evaluate(
                r"""() => {
                  if (!document.body || location.href === 'about:blank') return false;
                  const visible = (el) => {
                    const style = getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                      el.getClientRects().length > 0;
                  };
                  const text = (document.body.innerText || '').replace(/\s+/g, ' ').trim();
                  const receipt = /application (?:was |has been )?(?:successfully )?(?:submitted|received)|thank you for (?:applying|submitting your application)|申请已提交|投递成功|申请成功/i.test(text);
                  const ordinaryForm = [...document.querySelectorAll('form,input,select,textarea,button')]
                    .some(visible);
                  return text.length >= 40 && (ordinaryForm || receipt);
                }"""
            )
        )
    except Exception:
        logger.debug("Unable to confirm stable post-verification state", exc_info=True)
        return False


def _validate_pre_submit_snapshot(snapshot: dict, profile: dict, job: dict) -> list[str]:
    """Return browser-observed attention signals for the next agent turn."""
    issues: list[str] = []
    expected_url = job.get("application_url") or job.get("url") or ""
    actual_url = snapshot.get("url", "")
    if expected_url and actual_url:
        expected = urlparse(expected_url)
        actual = urlparse(actual_url)
        expected_path = expected.path.rstrip("/").removesuffix("/apply")
        actual_path = actual.path.rstrip("/").removesuffix("/apply")
        if expected.netloc.casefold() != actual.netloc.casefold() or expected_path != actual_path:
            issues.append("unexpected_application_url")

    if snapshot.get("captcha_visible"):
        issues.append("visible_captcha")

    issues.extend(
        f"required_field_empty:{label[:80]}"
        for label in snapshot.get("required_unfilled", [])
    )

    issues.extend(
        f"sensitive_required_unknown:{label[:80]}"
        for label in snapshot.get("sensitive_required_unknown", [])
    )

    if snapshot.get("assessment_visible"):
        issues.append("assessment_present")

    if "resume_field_present" in snapshot:
        if not snapshot.get("resume_field_present"):
            issues.append("resume_state_unconfirmed")
        elif not snapshot.get("resume_uploaded"):
            resume_cards = [
                str(value or "") for value in snapshot.get("resume_card_texts", [])
            ]
            expected_variant = prompt_mod._linkedin_resume_preference(profile, job)
            expected_text = str(expected_variant or "").casefold()
            existing_document_confirmed = any(
                re.search(r"\.(?:pdf|docx?)\b", text, re.IGNORECASE)
                and (not expected_text or expected_text in text.casefold())
                for text in resume_cards
            )
            if not existing_document_confirmed:
                issues.append("resume_not_uploaded")

    personal = profile.get("personal", {})
    legal_name = personal.get("full_name", "").strip().casefold()
    for value in snapshot.get("full_name_values", []):
        if legal_name and value.strip().casefold() != legal_name:
            issues.append("legal_name_mismatch")
            break

    expected_email = personal.get("email", "").strip().casefold()
    for value in snapshot.get("email_values", []):
        if expected_email and value.strip().casefold() != expected_email:
            issues.append("email_mismatch")
            break

    for value in snapshot.get("current_location_values", []):
        if "singapore" not in value.strip().casefold():
            issues.append("current_location_not_singapore")
            break

    screening = profile.get("screening", {})
    hard_answers = {
        "starting_september": screening.get(
            "available_for_full_time_3_6_month_internship_starting_september"
        ),
        "startup_internship": screening.get(
            "prior_internship_product_startup_logistics_ecommerce_b2b_saas"
        ),
    }
    for question in snapshot.get("radio_questions", []):
        text = question.get("text", "").casefold()
        selected = question.get("selected", "").strip().casefold()
        expected: bool | None = None
        key = ""
        if "starting september" in text and "full-time" in text:
            key = "starting_september"
            expected = hard_answers[key]
        elif (
            "prior internship" in text
            and "product-based startup" in text
            and any(term in text for term in ("logistics", "ecommerce", "b2b saas"))
        ):
            key = "startup_internship"
            expected = hard_answers[key]
        if expected is not None and selected != ("yes" if expected else "no"):
            issues.append(f"hard_answer_mismatch:{key}")

        generic_expected = _expected_screening_answer(text, profile, job)
        if generic_expected is not None:
            generic_key, generic_value = generic_expected
            if not _selected_matches_boolean(selected, generic_value):
                issues.append(f"hard_answer_mismatch:{generic_key}")

    for field in snapshot.get("select_fields", []):
        text = field.get("text", "").casefold()
        selected = field.get("selected", "").strip().casefold()
        if "currently based" in text and "legal right to work" in text and selected != "singapore":
            issues.append("work_location_selection_not_singapore")
        generic_expected = _expected_screening_answer(text, profile, job)
        if generic_expected is not None:
            generic_key, generic_value = generic_expected
            if not _selected_matches_boolean(selected, generic_value):
                issues.append(f"hard_answer_mismatch:{generic_key}")

    readiness_text = str(job.get("application_readiness_reason") or "").casefold()
    non_credit_part_time = "non-credit" in readiness_text or "part-time" in readiness_text
    weekly_limit = profile.get("availability", {}).get(
        "non_credit_internship_hours_per_week_max"
    )
    if non_credit_part_time and isinstance(weekly_limit, (int, float)):
        for field in snapshot.get("text_fields", []):
            text = str(field.get("text") or "").casefold()
            if not re.search(r"hours? (?:per|a) week|weekly hours?", text):
                continue
            match = re.search(r"\d+(?:\.\d+)?", str(field.get("value") or ""))
            if match and float(match.group()) > float(weekly_limit):
                issues.append("non_credit_hours_exceed_confirmed_limit")

    if snapshot.get("submit_control_count", 0) < 1:
        issues.append("submit_control_missing")
    return list(dict.fromkeys(issues))


def _select_application_page(pages: list):
    """Choose the tab carrying a review/receipt rather than relying on tab order."""
    selected = pages[-1]
    selected_score = -1
    for page in pages:
        try:
            signals = page.evaluate(
                r"""() => {
                  const visible = (el) => {
                    const style = getComputedStyle(el);
                    return style.display !== 'none' && style.visibility !== 'hidden' &&
                      el.getClientRects().length > 0;
                  };
                  const text = document.body ? document.body.innerText : '';
                  const receipt = /your application has been submitted|application (?:was |has been )?(?:successfully )?submitted|thank you for (?:applying|submitting your application)|we (?:have )?received your application|申请已提交|投递成功|申请成功/i.test(text);
                  const finalSubmit = [...document.querySelectorAll('button,input[type=submit]')]
                    .some((el) => visible(el) && /^(submit|submit application|send application|finish|complete application|提交申请|投递)$/i.test(
                      (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
                    ));
                  const review = /review your application/i.test(text);
                  const dialog = [...document.querySelectorAll('dialog,[role="dialog"]')].some(visible);
                  return {receipt, final_submit: finalSubmit, review, dialog};
                }"""
            )
            score = (
                100 * int(bool(signals.get("receipt")))
                + 50 * int(bool(signals.get("final_submit")))
                + 20 * int(bool(signals.get("review")))
                + 10 * int(bool(signals.get("dialog")))
            )
            if score > selected_score:
                selected = page
                selected_score = score
        except Exception:
            logger.debug("Unable to score browser page for application evidence", exc_info=True)
    return selected


def _bound_application_pages(browser, pages: list, job: dict) -> list:
    """Restrict browser evidence to the application's immutable target lineage."""
    if "_browser_root_target_ids" not in job:
        return pages
    roots = set(job.get("_browser_root_target_ids") or [])
    if not roots:
        return []
    session = browser.new_browser_cdp_session()
    infos = {
        str(info.get("targetId") or ""): info
        for info in session.send("Target.getTargets").get("targetInfos", [])
        if info.get("targetId")
    }
    from applypilot.apply.credential_relay import _target_descends_from

    bound = []
    for page in pages:
        try:
            info = page.context.new_cdp_session(page).send("Target.getTargetInfo")[
                "targetInfo"
            ]
        except Exception:  # noqa: BLE001, S112 - page can detach during navigation
            continue
        target_id = str(info.get("targetId") or "")
        infos[target_id] = info
        if _target_descends_from(target_id, roots, infos):
            bound.append(page)
    return bound


def _audit_live_pre_submit_page(
    port: int, worker_id: int, job: dict
) -> tuple[str | None, dict]:
    """Observe the visible form without changing it or deciding whether to proceed."""
    from playwright.sync_api import sync_playwright

    profile = config.load_profile()
    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        pages = _bound_application_pages(browser, pages, job)
        if not pages:
            return "pre_submit_audit:no_bound_application_page", {}
        page = _select_application_page(pages)
        page.bring_to_front()
        snapshot = page.evaluate(
            r"""() => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  el.getClientRects().length > 0 && !el.disabled;
              };
              const context = (el) => el.closest(
                'li, fieldset, [data-qa*="field"], [class*="application-field"], [class*="question"]'
              ) || el.parentElement;
              const labelText = (el) => {
                const node = context(el);
                return ((node && node.innerText) || el.getAttribute('aria-label') || el.name || '')
                  .replace(/\s+/g, ' ').trim().slice(0, 500);
              };
              const required = (el) => el.required || el.getAttribute('aria-required') === 'true' || /[✱*]/.test(labelText(el));
              const responseSelector =
                'textarea[name*="captcha" i],textarea[name*="recaptcha" i],input[name*="captcha" i],input[name*="recaptcha" i]';
              const responseFields = [...document.querySelectorAll(responseSelector)];
              const inputs = [...document.querySelectorAll(
                'input:not([type=hidden]):not([type=radio]):not([type=checkbox]):not([type=file]):not([type=submit]):not([type=button]), textarea, select'
              )].filter((el) => visible(el) && !el.matches(responseSelector));
              const requiredUnfilled = [];
              const sensitiveRequiredUnknown = [];
              const fullNameValues = [];
              const emailValues = [];
              const currentLocationValues = [];
              const selectFields = [];
              const textFields = [];
              for (const el of inputs) {
                const text = labelText(el);
                const value = el.tagName === 'SELECT'
                  ? (el.selectedOptions[0] ? el.selectedOptions[0].textContent.trim() : '')
                  : (el.value || '').trim();
                if (required(el) && (!value || /^(select|choose)(\.\.\.)?$/i.test(value))) {
                  requiredUnfilled.push(text);
                }
                if (
                  required(el) &&
                  /work (authorization|authorisation)|right to work|visa|sponsorship|citizenship|legal identity|passport|national id/i.test(text) &&
                  (!value || /^(select|choose|unknown|not sure|prefer not)(\.\.\.)?$/i.test(value))
                ) sensitiveRequiredUnknown.push(text);
                if (/\b(full|legal) name\b/i.test(text) && !/preferred|display/i.test(text)) {
                  fullNameValues.push(value);
                }
                if (el.type === 'email' || /\bemail(?: address)?\b/i.test(text)) emailValues.push(value);
                if (/current location/i.test(text)) currentLocationValues.push(value);
                if (el.tagName === 'SELECT') selectFields.push({text, selected: value});
                else textFields.push({text, value});
              }
              const nearbyUploadText = (el) => {
                let node = el;
                for (let depth = 0; node && node !== document.body && depth < 7; depth += 1) {
                  const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
                  if (/\.(?:pdf|docx?)\b|uploaded|replace|remove|download/i.test(text)) return text;
                  node = node.parentElement;
                }
                return '';
              };
              const fileFields = [...document.querySelectorAll('input[type=file]')]
                .map((el) => ({
                  text: labelText(el),
                  nearby_text: nearbyUploadText(el),
                  count: el.files ? el.files.length : 0
                }));
              const resumeFields = fileFields.filter((f) => /\bresume\b|\bcv\b/i.test(f.text));
              const attributedResumeCards = [...document.querySelectorAll(
                '[data-qa*="resume" i],[data-testid*="resume" i],[class*="resume" i],[aria-label*="resume" i],[aria-label*="cv" i]'
              )].filter(visible).map((el) => (el.innerText || el.getAttribute('aria-label') || '').trim());
              const reviewResumeCards = [...document.querySelectorAll('h1,h2,h3,h4')]
                .filter((el) => visible(el) && /^(resume|cv)$/i.test((el.innerText || '').trim()))
                .map((heading) => {
                  let node = heading.parentElement;
                  for (let depth = 0; node && node !== document.body && depth < 5; depth += 1) {
                    const text = (node.innerText || '').replace(/\s+/g, ' ').trim();
                    if (/\.(?:pdf|docx?)\b/i.test(text)) return text;
                    node = node.parentElement;
                  }
                  return '';
                }).filter(Boolean);
              const resumeCards = [...attributedResumeCards, ...reviewResumeCards];
              const resumeUploaded = resumeFields.some((f) =>
                f.count > 0 || /success|uploaded|replace|remove|\.(?:pdf|docx?)/i.test(
                  `${f.text} ${f.nearby_text}`
                )
              ) || resumeCards.some((text) => /\b[^\s]+\.(?:pdf|docx?)\b|uploaded|replace|remove|download/i.test(text));
              const radios = [...document.querySelectorAll('input[type=radio]')].filter(visible);
              const seen = new Set();
              const radioQuestions = [];
              for (const radio of radios) {
                const node = context(radio);
                const key = radio.name || (node ? node.innerText : '') || String(radioQuestions.length);
                if (seen.has(key)) continue;
                seen.add(key);
                const group = node ? [...node.querySelectorAll('input[type=radio]')] : [radio];
                const checked = group.find((item) => item.checked);
                let selected = '';
                if (checked) {
                  const checkedLabel = checked.closest('label') || checked.parentElement;
                  selected = ((checkedLabel && checkedLabel.innerText) || checked.value || '').trim();
                }
                const text = labelText(radio);
                if (required(radio) && !checked) requiredUnfilled.push(text);
                if (
                  required(radio) && !checked &&
                  /work (authorization|authorisation)|right to work|visa|sponsorship|citizenship|legal identity|passport|national id/i.test(text)
                ) sensitiveRequiredUnknown.push(text);
                radioQuestions.push({text, selected});
              }
              const requiredChecks = [...document.querySelectorAll('input[type=checkbox]')]
                .filter((el) => visible(el) && required(el));
              for (const checkbox of requiredChecks) {
                if (!checkbox.checked) requiredUnfilled.push(labelText(checkbox));
              }
              const submitControls = [...document.querySelectorAll('button,input[type=submit]')]
                .filter((el) => visible(el) && /submit|send application|finish|complete application/i.test(
                  (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
                ));
              const captchaCandidates = [...document.querySelectorAll(
                'iframe,[class*="turnstile" i],[id*="turnstile" i],[class*="hcaptcha" i],[class*="recaptcha" i],[data-sitekey]'
              )].map((el) => {
                const rect = el.getBoundingClientRect();
                const style = getComputedStyle(el);
                const marker = `${el.title || ''} ${el.src || ''} ${el.id || ''} ${el.className || ''}`.toLowerCase();
                return {
                  marker: marker.slice(0, 240),
                  width: Math.round(rect.width),
                  height: Math.round(rect.height),
                  display: style.display,
                  visibility: style.visibility,
                  opacity: style.opacity,
                  aria_hidden: el.getAttribute('aria-hidden') || '',
                  visible: visible(el) && rect.width >= 80 && rect.height >= 40 &&
                    /captcha|turnstile|challenge/.test(marker)
                };
              });
              const captchaVisible = captchaCandidates.some((candidate) => candidate.visible);
              const visibleText = document.body ? document.body.innerText : '';
              const assessmentVisible = /\b(complete|take|start) (an? )?(online |coding |video )?assessment\b|\bcoding assessment\b|\bonline assessment\b/i.test(visibleText);
              return {
                url: location.href,
                required_unfilled: requiredUnfilled,
                sensitive_required_unknown: sensitiveRequiredUnknown,
                resume_field_present: resumeFields.length > 0 || resumeCards.length > 0,
                resume_uploaded: resumeUploaded,
                resume_card_texts: resumeCards,
                full_name_values: fullNameValues,
                email_values: emailValues,
                current_location_values: currentLocationValues,
                select_fields: selectFields,
                text_fields: textFields,
                radio_questions: radioQuestions,
                submit_control_count: submitControls.length,
                assessment_visible: assessmentVisible,
                captcha_visible: captchaVisible,
                captcha_candidates: captchaCandidates,
                captcha_token_present: responseFields.some((el) => (el.value || '').trim().length > 0)
              };
            }"""
        )
        issues = _validate_pre_submit_snapshot(snapshot, profile, job)
        report = {
            "status": "clear" if not issues else "attention",
            "page_url": snapshot.get("url", ""),
            "issues": issues,
            "advisory_only": False,
            "submission_gate": True,
            "required_unfilled_count": len(snapshot.get("required_unfilled", [])),
            "resume_field_present": snapshot.get("resume_field_present", False),
            "resume_uploaded": snapshot.get("resume_uploaded", False),
            "submit_control_count": snapshot.get("submit_control_count", 0),
            "captcha_token_present": snapshot.get("captcha_token_present", False),
            "captcha_candidates": snapshot.get("captcha_candidates", []),
            "assessment_visible": snapshot.get("assessment_visible", False),
            "sensitive_required_unknown_count": len(
                snapshot.get("sensitive_required_unknown", [])
            ),
        }
        report_path = (
            config.APPLY_WORKER_DIR / f"worker-{worker_id}" / "pre-submit-audit.json"
        )
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        if "visible_captcha" in issues:
            return "visible_captcha", report
        if issues:
            return "pre_submit_audit:" + ",".join(issues[:5]), report
        return None, report
    except Exception as exc:
        logger.exception("Pre-submit browser audit failed")
        return f"pre_submit_audit_error:{type(exc).__name__}", {}
    finally:
        playwright.stop()


def _classify_post_submit_observation(observation: dict) -> str:
    """Classify the browser state after a final action without guessing success.

    A visible receipt is success. A visible verification gate or deterministic
    field validation rejection proves the application is not yet submitted and
    must not be collapsed into the retry-blocking ``submission_uncertain`` state.
    """
    if observation.get("confirmed") is True:
        return "confirmed"
    if (
        observation.get("verification_visible") is True
        or observation.get("captcha_visible") is True
    ):
        return "verification_required"
    if int(observation.get("validation_error_count") or 0) > 0:
        if int(observation.get("manual_validation_error_count") or 0) > 0:
            return "validation_blocked_manual"
        if int(observation.get("repairable_validation_error_count") or 0) > 0:
            return "validation_blocked_repairable"
        return "validation_blocked_manual"
    return "uncertain"


def _observe_post_submit_page(
    port: int, worker_id: int, job: dict, attempt: int = 1
) -> dict:
    """Independently observe visible post-submit state through the existing CDP browser."""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        pages = _bound_application_pages(browser, pages, job)
        if not pages:
            return {"confirmed": False, "reason": "post_submit_no_bound_application_page"}
        page = _select_application_page(pages)
        observed = page.evaluate(
            r"""() => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  el.getClientRects().length > 0;
              };
              const strongReceipt = /your application has been submitted|application (?:was |has been )?(?:successfully )?submitted|thank you for (?:applying|submitting your application)|we (have )?received your application|申请已提交|投递成功|申请成功/i;
              const exactBadge = /^(applied|已申请|已投递)$/i;
              const submitLabel = /submit|send application|finish|complete application|提交申请|投递/i;
              const verificationText = /security code|verification code|one[- ]time (?:code|password)|\botp\b|verify (?:your )?email|email verification|验证码|校验码|验证邮箱/i;
              const unsafeRepairText = /video|audio|record(?:ing)?|camera|microphone|passport|national id|identity document|bank account|credit card|tax id|ssn|nric|身份证|护照|银行卡|录音|录像|摄像头|麦克风/i;
              const candidates = [...document.querySelectorAll(
                '[role="status"],[aria-live],[data-qa*="confirm" i],[data-testid*="confirm" i],[class*="confirmation" i],[class*="success" i]'
              )].filter(visible).map((el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
              const lines = (document.body ? document.body.innerText : '').split(/\n+/)
                .map((line) => line.replace(/\s+/g, ' ').trim()).filter(Boolean);
              const structuredReceipt = candidates.find((text) => strongReceipt.test(text)) || '';
              const receiptText = structuredReceipt || lines.find((text) => strongReceipt.test(text)) || '';
              const badgeText = [...document.querySelectorAll('button,a,span,div')]
                .filter(visible).map((el) => (el.innerText || '').replace(/\s+/g, ' ').trim())
                .find((text) => exactBadge.test(text)) || '';
              const context = (el) => el.closest(
                'li,fieldset,[data-qa*="field" i],[data-testid*="field" i],[class*="application-field" i],[class*="question" i],[class*="field" i]'
              ) || el.parentElement;
              const labelText = (el) => {
                const node = context(el);
                return ((node && node.innerText) || el.getAttribute('aria-label') || el.name || '')
                  .replace(/\s+/g, ' ').trim().slice(0, 500);
              };
              const controls = [...document.querySelectorAll('input:not([type=hidden]),textarea,select')]
                .filter(visible);
              const validationErrors = [];
              const seenErrors = new Set();
              const seenMessages = new Set();
              for (const el of controls) {
                let described = '';
                const describedBy = (el.getAttribute('aria-describedby') || '').trim().split(/\s+/).filter(Boolean);
                if (describedBy.length) {
                  described = describedBy.map((id) => {
                    const node = document.getElementById(id);
                    return node ? (node.innerText || node.textContent || '') : '';
                  }).join(' ').replace(/\s+/g, ' ').trim();
                }
                const nativeInvalid = Boolean(el.willValidate && !el.validity.valid);
                const ariaInvalid = el.getAttribute('aria-invalid') === 'true';
                const message = (el.validationMessage || described || '').replace(/\s+/g, ' ').trim();
                if (!nativeInvalid && !ariaInvalid && !message) continue;
                const label = labelText(el);
                const key = `${el.name || el.id || label}|${message}`;
                if (seenErrors.has(key)) continue;
                seenErrors.add(key);
                if (message) seenMessages.add(message);
                const type = el.tagName === 'SELECT' ? 'select' : (el.type || el.tagName.toLowerCase());
                const optionalClaimed = /\boptional\b|可选|非必填/i.test(label);
                const repairable = !unsafeRepairText.test(`${label} ${message}`) &&
                  !['file', 'password'].includes(type);
                validationErrors.push({
                  label: label.slice(0, 240),
                  message: message.slice(0, 240),
                  field_type: type,
                  optional_claimed: optionalClaimed,
                  repairable
                });
              }
              for (const alert of [...document.querySelectorAll('[role="alert"],[aria-live="assertive"]')].filter(visible)) {
                const message = (alert.innerText || alert.textContent || '').replace(/\s+/g, ' ').trim();
                if (!message || !/required|invalid|error|please (?:enter|select|complete|provide|upload)|必填|无效|错误|请选择|请填写/i.test(message)) continue;
                if (seenMessages.has(message)) continue;
                const key = `alert|${message}`;
                if (seenErrors.has(key)) continue;
                seenErrors.add(key);
                validationErrors.push({
                  label: 'page validation alert',
                  message: message.slice(0, 240),
                  field_type: 'unknown',
                  optional_claimed: /\boptional\b|可选|非必填/i.test(message),
                  repairable: false
                });
              }
              const submitControls = [...document.querySelectorAll('button,input[type=submit],input[type=button]')]
                .filter((el) => visible(el) && submitLabel.test(
                  (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
                ));
              const captchaVisible = [...document.querySelectorAll(
                'iframe,[class*="turnstile" i],[id*="turnstile" i],[class*="hcaptcha" i],[class*="recaptcha" i],[data-sitekey]'
              )].filter(visible).some((el) => {
                const rect = el.getBoundingClientRect();
                const marker = `${el.title || ''} ${el.src || ''} ${el.id || ''} ${el.className || ''}`.toLowerCase();
                return rect.width >= 80 && rect.height >= 40 && /captcha|turnstile|challenge/.test(marker);
              });
              const codeInputs = controls.filter((el) => {
                const maxLength = Number(el.maxLength || 0);
                return maxLength === 1 || /otp|verification|security.?code/i.test(`${el.name || ''} ${el.id || ''} ${el.autocomplete || ''}`);
              });
              const verificationVisible = captchaVisible ||
                (codeInputs.length >= 4 && verificationText.test(document.body ? document.body.innerText : '')) ||
                [...document.querySelectorAll('form,section,dialog,[role="dialog"]')]
                  .filter(visible).some((el) => verificationText.test(el.innerText || ''));
              const repairableCount = validationErrors.filter((item) => item.repairable).length;
              const manualCount = validationErrors.length - repairableCount;
              return {
                current_url: location.href,
                page_title: document.title || '',
                receipt_visible: Boolean(receiptText),
                receipt_structured: Boolean(structuredReceipt),
                applied_badge_visible: Boolean(badgeText),
                confirmation_text: receiptText || badgeText,
                form_visible: [...document.querySelectorAll('form')].some(visible),
                submit_control_count: submitControls.length,
                validation_errors: validationErrors.slice(0, 12),
                validation_error_count: validationErrors.length,
                repairable_validation_error_count: repairableCount,
                manual_validation_error_count: manualCount,
                verification_visible: verificationVisible,
                captcha_visible: captchaVisible
              };
            }"""
        )
        screenshot = (
            config.APPLY_WORKER_DIR
            / f"worker-{worker_id}"
            / (
                "submission-confirmation-observer.png"
                if attempt == 1
                else f"submission-confirmation-observer-attempt-{attempt}.png"
            )
        )
        try:
            page.screenshot(path=str(screenshot), full_page=True)
            observed["screenshot_path"] = str(screenshot)
        except Exception:
            logger.exception("Post-submit screenshot capture failed")
            observed["screenshot_path"] = None
        receipt_is_decisive = bool(observed.get("receipt_visible")) and (
            bool(observed.get("receipt_structured"))
            or (
                observed.get("form_visible") is False
                and int(observed.get("submit_control_count") or 0) == 0
            )
        )
        observed["confirmed"] = bool(
            receipt_is_decisive or observed.get("applied_badge_visible")
        )
        observed["disposition"] = _classify_post_submit_observation(observed)
        observed["job_url"] = job.get("url")
        return observed
    except Exception as exc:
        logger.exception("Post-submit browser observation failed")
        return {
            "confirmed": False,
            "reason": f"post_submit_observer_error:{type(exc).__name__}",
        }
    finally:
        playwright.stop()


def _submission_evidence_consistent(model: dict | None, observer: dict) -> bool:
    """Require independent visible confirmation that agrees with the model claim."""
    if not model or observer.get("confirmed") is not True:
        return False
    receipt_agrees = (
        model.get("receipt_visible") is True
        and observer.get("receipt_visible") is True
    )
    badge_agrees = (
        model.get("applied_badge_visible") is True
        and observer.get("applied_badge_visible") is True
    )
    if not (receipt_agrees or badge_agrees):
        return False

    model_text = " ".join(
        re.sub(
            r"[^\w]+", " ", str(model.get("confirmation_text") or "").casefold()
        ).split()
    )
    observed_text = " ".join(
        re.sub(
            r"[^\w]+", " ", str(observer.get("confirmation_text") or "").casefold()
        ).split()
    )
    if not model_text or model_text not in observed_text:
        return False

    claimed_url = str(model.get("confirmation_url") or "").strip().rstrip("/")
    current_url = str(observer.get("current_url") or "").strip().rstrip("/")
    return not claimed_url or claimed_url == current_url
