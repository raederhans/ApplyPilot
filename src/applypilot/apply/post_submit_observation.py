"""Independent post-submit browser observation and receipt consistency policy."""

from __future__ import annotations

import logging
import re

from applypilot import config
from applypilot.apply.page_surfaces import (
    bound_application_pages,
    select_application_page_and_frame,
)

logger = logging.getLogger(__name__)

HISTORICAL_DUPLICATE_RE = re.compile(
    r"^(?:your application (?:was|has been) already submitted|"
    r"application already submitted|you have already applied"
    r"(?: for this (?:job|position|role))?)"
    r"(?:[.!]\s*)?(?:view (?:your )?application|check (?:your )?status)?$",
    re.IGNORECASE,
)


def is_historical_duplicate_text(value: object) -> bool:
    """Recognize provider text describing a prior application, not this turn."""
    normalized = " ".join(str(value or "").split())
    return bool(HISTORICAL_DUPLICATE_RE.fullmatch(normalized))


def classify_post_submit_observation(observation: dict) -> str:
    """Classify the browser state after a final action without guessing success.

    A visible receipt is success. A visible verification gate or deterministic
    field validation rejection proves the application is not yet submitted and
    must not be collapsed into the retry-blocking ``submission_uncertain`` state.
    """
    # A provider can show a positive-looking status because this exact
    # application was submitted in an earlier session.  Keep that distinct
    # from a receipt produced by the current final action: it must not count
    # as a new application and must not enter the ordinary uncertain path.
    historical_duplicate = observation.get(
        "historical_duplicate"
    ) is True or is_historical_duplicate_text(
        observation.get("historical_duplicate_text")
        or observation.get("confirmation_text")
    )
    if historical_duplicate and observation.get("confirmed") is True:
        return "conflicting_post_submit_status"
    if historical_duplicate:
        return "historical_duplicate"
    if observation.get("confirmed") is True:
        return "confirmed"
    if (
        observation.get("verification_visible") is True
        or observation.get("captcha_visible") is True
    ):
        return "verification_required"
    if observation.get("provider_submission_error_visible") is True:
        return "provider_submission_error"
    if int(observation.get("validation_error_count") or 0) > 0:
        if int(observation.get("manual_validation_error_count") or 0) > 0:
            return "validation_blocked_manual"
        if int(observation.get("repairable_validation_error_count") or 0) > 0:
            return "validation_blocked_repairable"
        return "validation_blocked_manual"
    return "uncertain"


def observe_post_submit_page(
    port: int, worker_id: int, job: dict, attempt: int = 1
) -> dict:
    """Independently observe visible post-submit state through the existing CDP browser."""
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        pages = [page for context in browser.contexts for page in context.pages]
        pages = bound_application_pages(browser, pages, job)
        if not pages:
            return {"confirmed": False, "reason": "post_submit_no_bound_application_page"}
        page, application_surface = select_application_page_and_frame(pages)
        observed = application_surface.evaluate(
            r"""() => {
              const visible = (el) => {
                const style = getComputedStyle(el);
                return style.display !== 'none' && style.visibility !== 'hidden' &&
                  el.getClientRects().length > 0;
              };
              const deepRoots = [document];
              const deepElements = [];
              for (let index = 0; index < deepRoots.length; index += 1) {
                const elements = [...deepRoots[index].querySelectorAll('*')];
                deepElements.push(...elements);
                for (const element of elements) {
                  if (element.shadowRoot) deepRoots.push(element.shadowRoot);
                }
              }
              const deepAll = (selector) => deepElements.filter(
                (element) => element.matches(selector)
              );
              const elementsById = new Map();
              for (const element of deepElements) {
                if (element.id && !elementsById.has(element.id)) {
                  elementsById.set(element.id, element);
                }
              }
              const byId = (id) => elementsById.get(id) || null;
              const historicalDuplicate = /^(?:your application (?:was|has been) already submitted|application already submitted|you have already applied(?: for this (?:job|position|role))?)(?:[.!]\s*)?(?:view (?:your )?application|check (?:your )?status)?$/i;
              const strongReceipt = /your application has been submitted|application (?:was |has been )?(?:successfully )?submitted|thank you for (?:applying|submitting your application)|we (have )?received your application|申请已提交|投递成功|申请成功/i;
              const providerSubmissionError = /there was an error (?:verifying|submitting|processing) (?:your )?application|error (?:verifying|submitting|processing) (?:your )?application|(?:unable|failed) to (?:verify|submit|process) (?:your )?application|(?:application|submission) (?:could not|couldn't|was not) (?:be )?(?:verified|submitted|processed)/i;
              const exactBadge = /^(applied|已申请|已投递)$/i;
              const submitLabel = /submit|send application|finish|complete application|提交申请|投递/i;
              const verificationText = /security code|verification code|one[- ]time (?:code|password)|\botp\b|verify (?:your )?email|email verification|验证码|校验码|验证邮箱/i;
              const unsafeRepairText = /video|audio|record(?:ing)?|camera|microphone|passport|national id|identity document|bank account|credit card|tax id|ssn|nric|身份证|护照|银行卡|录音|录像|摄像头|麦克风/i;
              const candidates = deepAll(
                '[role="status"],[aria-live],[data-qa*="confirm" i],[data-testid*="confirm" i],[class*="confirmation" i],[class*="success" i]'
              ).filter(visible).map((el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
              const lines = (document.body ? document.body.innerText : '').split(/\n+/)
                .map((line) => line.replace(/\s+/g, ' ').trim()).filter(Boolean);
              const structuredReceipt = candidates.find((text) => strongReceipt.test(text)) || '';
              const receiptText = structuredReceipt || lines.find((text) => strongReceipt.test(text)) || '';
              const historicalDuplicateText = candidates.find(
                (text) => historicalDuplicate.test(text)
              ) || '';
              const providerSubmissionErrorText = lines.find(
                (text) => providerSubmissionError.test(text)
              ) || '';
              const badgeText = deepAll('button,a,span,div')
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
              const controls = deepAll('input:not([type=hidden]),textarea,select')
                .filter(visible);
              const validationErrors = [];
              const seenErrors = new Set();
              const seenMessages = new Set();
              for (const el of controls) {
                let described = '';
                const describedBy = (el.getAttribute('aria-describedby') || '').trim().split(/\s+/).filter(Boolean);
                if (describedBy.length) {
                  described = describedBy.map((id) => {
                    const node = byId(id);
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
                const repairEvidence = `${label} ${message}`;
                const resumeFileRepair = type === 'file' &&
                  /\b(?:resume|curriculum vitae|cv)\b/i.test(repairEvidence);
                const repairable = !unsafeRepairText.test(repairEvidence) &&
                  type !== 'password' && (type !== 'file' || resumeFileRepair);
                validationErrors.push({
                  label: label.slice(0, 240),
                  message: message.slice(0, 240),
                  field_type: type,
                  optional_claimed: optionalClaimed,
                  repairable
                });
              }
              for (const alert of deepAll('[role="alert"],[aria-live="assertive"]').filter(visible)) {
                const message = (alert.innerText || alert.textContent || '').replace(/\s+/g, ' ').trim();
                if (!message || !/required|invalid|error|please (?:enter|select|complete|provide|upload)|必填|无效|错误|请选择|请填写/i.test(message)) continue;
                if (seenMessages.has(message)) continue;
                const key = `alert|${message}`;
                if (seenErrors.has(key)) continue;
                seenErrors.add(key);
                const resumeUploadAlert =
                  /\b(?:resume|curriculum vitae|cv)\b/i.test(message) &&
                  /\b(?:upload|attach|file|required|missing|invalid)\b/i.test(message) &&
                  !unsafeRepairText.test(message);
                validationErrors.push({
                  label: 'page validation alert',
                  message: message.slice(0, 240),
                  field_type: resumeUploadAlert ? 'file' : 'unknown',
                  optional_claimed: /\boptional\b|可选|非必填/i.test(message),
                  repairable: resumeUploadAlert
                });
              }
              const submitControls = deepAll('button,input[type=submit],input[type=button],[role="button"]')
                .filter((el) => visible(el) && submitLabel.test(
                  (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
                ));
              const captchaVisible = deepAll(
                'iframe,[class*="turnstile" i],[id*="turnstile" i],[class*="hcaptcha" i],[class*="recaptcha" i],[data-sitekey]'
              ).filter(visible).some((el) => {
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
                deepAll('form,section,dialog,[role="dialog"]')
                  .filter(visible).some((el) => verificationText.test(el.innerText || ''));
              const repairableCount = validationErrors.filter((item) => item.repairable).length;
              const manualCount = validationErrors.length - repairableCount;
              return {
                current_url: location.href,
                page_title: document.title || '',
                receipt_visible: Boolean(receiptText),
                receipt_structured: Boolean(structuredReceipt),
                historical_duplicate: Boolean(historicalDuplicateText),
                historical_duplicate_text: historicalDuplicateText.slice(0, 500),
                applied_badge_visible: Boolean(badgeText),
                confirmation_text: receiptText || historicalDuplicateText || badgeText,
                provider_submission_error_visible: Boolean(providerSubmissionErrorText),
                provider_submission_error_text: providerSubmissionErrorText.slice(0, 500),
                form_visible: deepAll('form').some(visible),
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
        observed["document_url"] = observed.get("current_url", "")
        observed["current_url"] = page.url
        screenshot = (
            config.APPLY_WORKER_DIR
            / f"worker-{worker_id}"
            / (
                "post-submit-observer.png"
                if attempt == 1
                else f"post-submit-observer-attempt-{attempt}.png"
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
        observed["disposition"] = classify_post_submit_observation(observed)
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


def submission_evidence_consistent(model: dict | None, observer: dict) -> bool:
    """Require independent visible confirmation that agrees with the model claim."""
    if not model or observer.get("confirmed") is not True:
        return False
    if model.get("channel") == "direct_email":
        if observer.get("channel") != "direct_email":
            return False
        if model.get("send_accepted") is not True or model.get("sent_copy_verified") is not True:
            return False
        for key in ("recipient", "subject"):
            if str(model.get(key) or "").strip().casefold() != str(
                observer.get(key) or ""
            ).strip().casefold():
                return False
        model_attachments = {
            str(value).strip().casefold()
            for value in model.get("attachment_names", [])
            if str(value).strip()
        }
        observed_attachments = {
            str(value).strip().casefold()
            for value in observer.get("attachment_names", [])
            if str(value).strip()
        }
        return bool(model_attachments) and model_attachments == observed_attachments
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
    text_agrees = bool(model_text and observed_text) and (
        model_text in observed_text
        or observed_text in model_text
        or (
            receipt_agrees
            and looks_like_submission_receipt_text(model_text)
            and looks_like_submission_receipt_text(observed_text)
        )
    )
    if not text_agrees:
        return False

    claimed_url = str(model.get("confirmation_url") or "").strip().rstrip("/")
    current_url = str(observer.get("current_url") or "").strip().rstrip("/")
    return not claimed_url or claimed_url == current_url


def looks_like_submission_receipt_text(value: str) -> bool:
    """Recognize equivalent concise and verbose application receipt wording."""
    return bool(
        re.search(
            r"\b(?:your )?application (?:was |has been )?"
            r"(?:successfully )?(?:submitted|received)(?: successfully)?\b"
            r"|\bwe (?:have )?received your application\b"
            r"|\bthank you for (?:applying|submitting your application)\b"
            r"|申请已提交|投递成功|申请成功",
            value,
            flags=re.IGNORECASE,
        )
    )
