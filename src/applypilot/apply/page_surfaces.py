"""Trusted application-page and same-origin surface selection helpers."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

APPLICATION_SURFACE_SIGNALS = r"""() => {
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
  const deepAll = (selector) => deepElements.filter((element) => element.matches(selector));
  const text = document.body ? document.body.innerText : '';
  const receipt = /your application has been submitted|application (?:was |has been )?(?:successfully )?submitted|thank you for (?:applying|submitting your application)|we (?:have )?received your application|申请已提交|投递成功|申请成功/i.test(text);
  const finalSubmit = deepAll(
    'button,input[type=submit],[role="button"]'
  ).some((el) => visible(el) && /^(submit|submit application|send application|finish|complete application|提交申请|投递)$/i.test(
    (el.innerText || el.value || el.getAttribute('aria-label') || '').trim()
  ));
  const assessmentVisible = /\b(complete|take|start) (an? )?(online |coding |video )?assessment\b|\bcoding assessment\b|\bonline assessment\b/i.test(text);
  const captchaVisible = deepAll(
    'iframe,[class*="turnstile" i],[id*="turnstile" i],[class*="hcaptcha" i],[class*="recaptcha" i],[data-sitekey]'
  ).filter(visible).some((el) => {
    const rect = el.getBoundingClientRect();
    const marker = `${el.title || ''} ${el.src || ''} ${el.id || ''} ${el.className || ''}`.toLowerCase();
    return rect.width >= 80 && rect.height >= 40 && /captcha|turnstile|challenge/.test(marker);
  });
  const verificationText = /security code|verification code|one[- ]time (?:code|password)|\botp\b|verify (?:your )?email|email verification|验证码|校验码|验证邮箱/i;
  const codeInputs = deepAll('input:not([type=hidden])').filter((el) => {
    if (!visible(el)) return false;
    const maxLength = Number(el.maxLength || 0);
    const descriptor = `${el.name || ''} ${el.id || ''} ${el.autocomplete || ''}`;
    return maxLength === 1 || /otp|verification|security.?code|one-time-code/i.test(descriptor);
  });
  const verificationVisible =
    (codeInputs.length > 0 && verificationText.test(text)) ||
    deepAll('form,section,dialog,[role="dialog"]')
      .filter(visible).some((el) => verificationText.test(el.innerText || ''));
  const review = /review your application/i.test(text);
  const dialog = deepAll('dialog,[role="dialog"]').some(visible);
  const formControls = deepAll(
    'input,select,textarea,button,[role="button"],[role="radio"],[role="checkbox"]'
  ).filter(visible).length;
  return {
    receipt,
    final_submit: finalSubmit,
    captcha_visible: captchaVisible,
    assessment_visible: assessmentVisible,
    verification_visible: verificationVisible,
    review,
    dialog,
    form_controls: formControls,
    text_length: text.trim().length
  };
}"""


def application_surface_score(signals: dict) -> int:
    return (
        100 * int(bool(signals.get("receipt")))
        + 50 * int(bool(signals.get("final_submit")))
        + 20 * int(bool(signals.get("review")))
        + 10 * int(bool(signals.get("dialog")))
        + min(int(signals.get("form_controls") or 0), 20)
        + min(int(signals.get("text_length") or 0) // 500, 9)
    )


def application_surface_selection_score(signals: dict) -> int:
    """Prefer the field-bearing surface while preserving receipt priority."""
    return (
        1000 * int(bool(signals.get("receipt")))
        + 100 * int(bool(signals.get("review")))
        + 50 * int(bool(signals.get("dialog")))
        + 10 * min(int(signals.get("form_controls") or 0), 200)
        + 5 * int(bool(signals.get("final_submit")))
        + min(int(signals.get("text_length") or 0) // 500, 9)
    )


def http_origin(url: object) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(str(url or ""))
        scheme = parsed.scheme.casefold()
        host = (parsed.hostname or "").casefold()
        if scheme not in {"http", "https"} or not host:
            return None
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    return scheme, host, port


def application_surface_is_allowed(page, surface) -> bool:
    """Allow main, same-origin, or inherited-origin application surfaces only."""
    main_frame = getattr(page, "main_frame", page)
    if surface is main_frame:
        return True

    # Compatibility for non-browser test doubles. Real Playwright pages always
    # expose both a top-level URL and main_frame, so missing lineage at runtime
    # remains fail closed.
    if not hasattr(page, "url") and not hasattr(page, "main_frame"):
        return True

    top_origin = http_origin(getattr(page, "url", None)) or http_origin(
        getattr(main_frame, "url", None)
    )
    if top_origin is None:
        return False
    surface_url = str(getattr(surface, "url", "") or "").strip()
    if http_origin(surface_url) == top_origin:
        return True
    if surface_url.casefold() not in {"about:blank", "about:srcdoc"}:
        return False

    seen: set[int] = set()
    parent = getattr(surface, "parent_frame", None)
    while parent is not None and id(parent) not in seen:
        if parent is main_frame or http_origin(getattr(parent, "url", None)) == top_origin:
            return True
        seen.add(id(parent))
        parent_url = str(getattr(parent, "url", "") or "").strip().casefold()
        if parent_url not in {"about:blank", "about:srcdoc"}:
            return False
        parent = getattr(parent, "parent_frame", None)
    return False


def select_application_frame(page):
    """Choose the populated application surface across the page and child frames."""
    selected, _score = score_application_page(page)
    return selected


def score_application_page(page):
    """Return one page's trusted field surface and aggregate page score."""
    candidates = [
        frame
        for frame in list(getattr(page, "frames", ()) or (page,))
        if application_surface_is_allowed(page, frame)
    ]
    if not candidates:
        candidates = [getattr(page, "main_frame", page)]
    selected = candidates[0]
    selected_surface_score = -1
    page_score = -1
    for frame in candidates:
        try:
            signals = frame.evaluate(APPLICATION_SURFACE_SIGNALS)
            page_score = max(page_score, application_surface_score(signals))
            surface_score = application_surface_selection_score(signals)
            if surface_score > selected_surface_score:
                selected = frame
                selected_surface_score = surface_score
        except Exception:
            logger.debug("Unable to score browser frame for application evidence", exc_info=True)
    return selected, page_score


def select_application_page_and_frame(pages: list):
    """Choose a page and its best surface in one scoring pass."""
    selected_page = pages[-1]
    selected_surface = selected_page
    selected_score = -1
    for page in pages:
        try:
            surface, score = score_application_page(page)
            if score > selected_score:
                selected_page = page
                selected_surface = surface
                selected_score = score
        except Exception:
            logger.debug("Unable to score browser page for application evidence", exc_info=True)
    return selected_page, selected_surface


def select_application_page(pages: list):
    """Choose the tab carrying a review/receipt rather than relying on tab order."""
    selected, _surface = select_application_page_and_frame(pages)
    return selected


def allowed_application_surface_signals(page, application_surface) -> list[dict]:
    """Read only the selected application surface and its owning main frame."""
    surfaces = []
    for surface in (getattr(page, "main_frame", page), application_surface):
        if (
            surface is not None
            and application_surface_is_allowed(page, surface)
            and all(surface is not item for item in surfaces)
        ):
            surfaces.append(surface)
    signals = []
    for surface in surfaces:
        try:
            observed = surface.evaluate(APPLICATION_SURFACE_SIGNALS)
            if isinstance(observed, dict):
                signals.append(observed)
        except Exception:
            logger.debug(
                "Unable to inspect an allowed application surface",
                exc_info=True,
            )
    return signals


def merge_same_page_submit_evidence(
    snapshot: dict,
    page,
    application_surface,
) -> None:
    """Merge Submit and manual-gate evidence from the same allowed surfaces."""
    try:
        selected_surface_count = int(snapshot.get("submit_control_count") or 0)
    except (TypeError, ValueError):
        selected_surface_count = 0
    signals = allowed_application_surface_signals(page, application_surface)
    snapshot["submit_control_count"] = max(
        selected_surface_count,
        sum(int(bool(item.get("final_submit"))) for item in signals),
    )
    for key in ("captcha_visible", "assessment_visible", "verification_visible"):
        snapshot[key] = bool(snapshot.get(key)) or any(
            bool(item.get(key)) for item in signals
        )


def bound_application_pages(browser, pages: list, job: dict) -> list:
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
