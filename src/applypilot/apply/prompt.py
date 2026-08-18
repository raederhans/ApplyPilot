"""Prompt builder for the autonomous job application agent.

Constructs the full instruction prompt that tells Claude Code / the AI agent
how to fill out a job application form using Playwright MCP tools. All
personal data is loaded from the user's profile -- nothing is hardcoded.
"""

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

from applypilot import config

logger = logging.getLogger(__name__)


def _preferred_display_name(personal: dict) -> str:
    """Return the configured display name without duplicating the surname."""
    full_name = personal["full_name"]
    configured = personal.get("preferred_display_name", "").strip()
    if configured:
        return configured

    preferred = personal.get("preferred_name", "").strip()
    if not preferred:
        return full_name
    if " " in preferred:
        return preferred

    last_name = full_name.split()[-1] if " " in full_name else ""
    return f"{preferred} {last_name}".strip()


def _build_profile_summary(profile: dict) -> str:
    """Format the applicant profile section of the prompt.

    Reads all relevant fields from the profile dict and returns a
    human-readable multi-line summary for the agent.
    """
    p = profile
    personal = p["personal"]
    work_auth = p["work_authorization"]
    comp = p["compensation"]
    exp = p.get("experience", {})
    avail = p.get("availability", {})
    eeo = p.get("eeo_voluntary", {})

    lines = [
        f"Legal Name: {personal['full_name']}",
        f"Preferred/Display Name: {_preferred_display_name(personal)}",
        f"Email: {personal['email']}",
        f"Phone: {personal['phone']}",
    ]

    # Address -- handle optional fields gracefully
    addr_parts = [
        personal.get("address", ""),
        personal.get("address_line_2", ""),
        personal.get("city", ""),
        personal.get("province_state", ""),
        personal.get("country", ""),
        personal.get("postal_code", ""),
    ]
    lines.append(f"Address: {', '.join(p for p in addr_parts if p)}")

    if personal.get("linkedin_url"):
        lines.append(f"LinkedIn: {personal['linkedin_url']}")
    if personal.get("github_url"):
        lines.append(f"GitHub: {personal['github_url']}")
    if personal.get("portfolio_url"):
        lines.append(f"Portfolio: {personal['portfolio_url']}")
    if personal.get("website_url"):
        lines.append(f"Website: {personal['website_url']}")

    # Work authorization
    lines.append(f"Work Auth: {work_auth.get('legally_authorized_to_work', 'See profile')}")
    lines.append(f"Sponsorship Needed: {work_auth.get('require_sponsorship', 'See profile')}")
    if work_auth.get("work_permit_type"):
        lines.append(f"Work Permit: {work_auth['work_permit_type']}")

    # Compensation
    currency = comp.get("salary_currency", "USD")
    lines.append(f"Salary Strategy ({currency}): {comp['salary_expectation']}")

    # Experience
    if exp.get("years_of_experience_total"):
        lines.append(f"Years Experience: {exp['years_of_experience_total']}")
    if exp.get("education_level"):
        lines.append(f"Education: {exp['education_level']}")

    # Availability
    lines.append(f"Available: {avail.get('earliest_start_date', 'Immediately')}")

    # Screening facts -- never invent defaults for legal or employer-specific
    # questions. Missing facts remain manual-review items.
    screening = p.get("screening", {})
    screening_labels = (
        ("Age 18+", "age_18_or_older"),
        ("Background Check", "willing_to_complete_background_check"),
        ("Drug Test", "willing_to_complete_drug_test"),
        ("Criminal Convictions to Disclose", "criminal_convictions_to_disclose"),
        ("Driver's License", "drivers_license"),
        ("Has Transportation", "has_transportation"),
        ("NDA", "willing_to_sign_nda"),
        ("Employment Restrictions", "employment_or_non_compete_restrictions"),
        ("Previously Worked Here", "previously_worked_for_target_employer"),
    )
    for label, key in screening_labels:
        lines.append(f"{label}: {screening.get(key, 'Manual review')}")
    lines.append("How Heard: Use the actual discovery source from the job record")

    current = p.get("current_employment", {})
    if current:
        lines.append(f"Current Employment: {current.get('title', '')} at {current.get('company', '')}")
        lines.append(f"Notice Period: {current.get('notice_period', 'Manual review')}")
        lines.append(f"Contact Current Employer: {current.get('contact_current_employer', 'Manual review')}")

    languages = p.get("languages", [])
    if languages:
        language_text = "; ".join(
            f"{item.get('language')}: {item.get('proficiency')}" for item in languages
        )
        lines.append(f"Languages: {language_text}")

    education = p.get("education", [])
    for item in education:
        date = item.get("expected_graduation") or item.get("graduation", "")
        gpa = item.get("gpa", "")
        detail = f"{item.get('institution')}: {item.get('degree')} ({date})"
        if gpa and "leave blank" not in str(gpa).lower():
            detail += f", GPA {gpa}"
        lines.append(f"Education Record: {detail}")

    # EEO
    lines.append(f"Gender: {eeo.get('gender', 'Decline to self-identify')}")
    lines.append(f"Race: {eeo.get('race_ethnicity', 'Decline to self-identify')}")
    lines.append(f"Veteran: {eeo.get('veteran_status', 'I am not a protected veteran')}")
    lines.append(f"Disability: {eeo.get('disability_status', 'I do not wish to answer')}")

    return "\n".join(lines)


def _build_location_check(profile: dict, search_config: dict) -> str:
    """Build the location eligibility check section of the prompt.

    Uses the accept_patterns from search config to determine which cities
    are acceptable for hybrid/onsite roles.
    """
    personal = profile["personal"]
    location_cfg = search_config.get("location", {})
    accept_patterns = location_cfg.get("accept_patterns", [])
    primary_city = personal.get("city", location_cfg.get("primary", "your city"))

    # Build the list of acceptable cities for hybrid/onsite
    if accept_patterns:
        city_list = ", ".join(accept_patterns)
    else:
        city_list = primary_city

    return f"""== LOCATION CHECK (do this FIRST before any form) ==
Read the job page. Determine the work arrangement. Then decide:
- "Remote" or "work from anywhere" -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in {city_list} -> ELIGIBLE. Apply.
- "Hybrid" or "onsite" in another city BUT the posting also says "remote OK" or "remote option available" -> ELIGIBLE. Apply.
- "Onsite only" or "hybrid only" in any city outside the list above with NO remote option -> NOT ELIGIBLE. Stop immediately. Output RESULT:FAILED:not_eligible_location
- City is overseas (India, Philippines, Europe, etc.) with no remote option -> NOT ELIGIBLE. Output RESULT:FAILED:not_eligible_location
- Cannot determine location -> Continue applying. If a screening question reveals it's non-local onsite, answer honestly and let the system reject if needed.
Do NOT fill out forms for jobs that are clearly onsite in a non-acceptable location. Check EARLY, save time."""


def _build_salary_section(profile: dict) -> str:
    """Build the salary negotiation instructions.

    Adapts floor, range, and currency from the profile's compensation section.
    """
    comp = profile["compensation"]
    currency = comp.get("salary_currency", "SGD")
    internship_default = comp.get("internship_monthly_default", 1750)
    internship_min = comp.get("internship_monthly_min", 1500)
    internship_max = comp.get("internship_monthly_max", 2000)
    full_time_min = comp.get("full_time_annual_min", "")
    full_time_max = comp.get("full_time_annual_max", "")

    return f"""== COMPENSATION (no salary-based rejection) ==
Finding a suitable role takes priority. Never reject or stop an application because compensation is below a stored preference.

Decision tree:
1. Optional compensation field -> leave blank; if text is required, enter "Negotiable".
2. Internship field requiring one monthly number -> enter {currency} {internship_default} per month.
3. Internship field requesting a range -> enter {currency} {internship_min}-{internship_max} per month.
4. Full-time field shows an employer range -> do not invent a floor or convert it. Use the employer's range only if the form accepts a range.
5. Full-time field requires one salary number or asks current/expected salary -> STOP before submission and report RESULT:FAILED:manual_salary_review. Reference range is {currency} {full_time_min}-{full_time_max} per year, but it is not authorization to answer automatically.
6. Never add a dollar sign automatically, never assume annual versus monthly, and never convert annual salary to hourly pay without explicit user review."""


def _build_screening_section(profile: dict) -> str:
    """Build the screening questions guidance section."""
    personal = profile["personal"]
    exp = profile.get("experience", {})
    city = personal.get("city", "their city")
    years = exp.get("years_of_experience_total", "multiple")
    target_role = exp.get("target_role", personal.get("current_job_title", "software engineer"))
    work_auth = profile["work_authorization"]
    mobility = profile.get("mobility", {})
    screening = profile.get("screening", {})
    answer_policy = profile.get("screening_answer_policy", {})
    related_yes_policy = answer_policy.get(
        "required_experience_yes_policy",
        "Answer Yes only when direct or sufficiently adjacent same-domain evidence reasonably supports the category.",
    )
    exact_tool_policy = answer_policy.get(
        "exact_tool_policy",
        "Do not claim an absent exact tool, duration, certification, license, or regulated qualification.",
    )

    return f"""== SCREENING QUESTIONS (be strategic) ==
Hard facts -> answer truthfully from the profile. No guessing. This includes:
  - Location/relocation: lives in {city}; willing to relocate within Singapore: {mobility.get('willing_to_relocate_within_singapore', 'manual review')}; willing to relocate to another country: {mobility.get('willing_to_relocate_to_another_country', 'manual review')}
  - Travel: {mobility.get('willing_to_travel', 'manual review')}, maximum {mobility.get('maximum_travel_percentage', 'manual review')}%
  - Work authorization: {work_auth.get('legally_authorized_to_work', 'see profile')}
  - Citizenship, clearance, licenses, certifications: answer from profile only
  - Criminal/background: convictions to disclose = {screening.get('criminal_convictions_to_disclose', 'manual review')}; background check = {screening.get('willing_to_complete_background_check', 'manual review')}
  - Previous employment, relatives, or referrals at this employer: determine for this exact employer; never use a global default

Required experience and skills -> use the APPLICANT PROFILE, RESUME TEXT, and configured evidence policy. This candidate is a {target_role} with {years} years total experience. {related_yes_policy} Umbrella categories may be supported by explicit adjacent work: for example, documented LLM, generative-AI, hybrid-RAG, tool-calling, agent, or AI-workflow work can justify YES to a broadly phrased LLM/GenAI/AI-automation experience question. Do not require an exact keyword match when the underlying same-domain work is clear.

Precision boundary -> {exact_tool_policy} Do not convert general ML or AI familiarity into experience with a specifically named absent framework. Never invent exact years or months for a named technology. If a required answer has neither direct nor sufficiently adjacent same-domain support, output RESULT:FAILED:manual_review_required:unsupported_skill_answer. For open text, label transferable experience precisely rather than presenting it as identical experience.

Open-ended questions ("Why do you want this role?", "Tell us about yourself", "What interests you?") -> Write 2-3 sentences. Be specific to THIS job. Reference something from the job description. Connect it to a real achievement from the resume. No generic fluff. No "I am passionate about..." -- sound like a real person.

EEO/demographics -> "Decline to self-identify" or "Prefer not to say" for everything."""


def _build_hard_rules(profile: dict) -> str:
    """Build the hard rules section with work auth and name from profile."""
    personal = profile["personal"]
    work_auth = profile["work_authorization"]

    full_name = personal["full_name"]
    preferred_name = personal.get("preferred_name", full_name.split()[0])
    display_name = _preferred_display_name(personal)

    # Build work auth rule dynamically
    sponsorship = work_auth.get("require_sponsorship", "")
    permit_type = work_auth.get("work_permit_type", "")

    work_auth_rule = "Work auth: Answer truthfully from profile."
    if permit_type:
        work_auth_rule = f"Work auth: {permit_type}. Sponsorship needed: {sponsorship}."

    name_rule = (
        f'Name: Legal name = {full_name}. Treat "Full name", "First/Given name", '
        '"Last/Family name", and "Surname" as legal-name fields even when the word '
        '"legal" is omitted.'
    )
    if preferred_name and preferred_name != full_name.split()[0]:
        name_rule += (
            f' Preferred name = {preferred_name}; display name = "{display_name}". '
            'Use those only when the field explicitly asks for preferred, chosen, or display name.'
        )

    return f"""== HARD RULES (never break these) ==
1. Never lie about: citizenship, work authorization, criminal history, education credentials, security clearance, licenses.
2. {work_auth_rule}
3. {name_rule}"""


def _build_login_steps(profile: dict) -> str:
    """Build a narrow, auditable authentication policy for the browser agent."""
    authentication = profile.get("authentication", {})
    google_reuse_authorized = bool(
        authentication.get("google_sso_existing_session_authorized", False)
    )
    account_creation_authorized = bool(
        authentication.get("ats_account_creation_authorized", False)
    )
    gmail_verification_authorized = bool(
        authentication.get("gmail_verification_authorized", False)
    )
    email = authentication.get(
        "ats_signup_email",
        profile.get("personal", {}).get("email", "the configured email"),
    )
    mailbox = authentication.get("gmail_verification_mailbox", email)

    if google_reuse_authorized or account_creation_authorized:
        google_rule = (
            f"You may use Continue with Google only by selecting the already signed-in account {email} and "
            "granting basic identity/email access. Stop if Google asks for credentials, account recovery, MFA "
            "enrollment, or broader OAuth scopes."
            if google_reuse_authorized
            else "Google SSO reuse is not authorized."
        )
        signup_rule = (
            f"For an ordinary employer ATS only, account creation with {email} is authorized. Never type, print, "
            "read aloud, copy into the prompt, or expose the password. Fill credentials only by running "
            ".\\fill-ats-credentials.ps1 -Field email, password, or both from the worker directory. The relay "
            "fills the browser directly and must not submit the form. If the relay is missing, unconfigured, "
            "rejects the current host, or fails, stop with RESULT:FAILED:credential_relay_required."
            if account_creation_authorized
            else "Do not create a new account."
        )
        verification_rule = (
            f"Email verification is authorized only through the read-only Gmail tools for mailbox {mailbox}. "
            "Search narrowly for a message received within the last 10 minutes, addressed to that exact mailbox, "
            "and confidently tied to the current employer/ATS domain. Read only the shortlisted verification "
            "message, enter the one-time code directly, and never repeat the code in chat, reasoning, reports, "
            "screenshots, or logs. If the mailbox differs, the message is stale/ambiguous, or the flow requests "
            "phone/SMS verification, password reset, account recovery, security questions, or MFA enrollment, "
            "stop with RESULT:LOGIN_ISSUE."
            if gmail_verification_authorized
            else "Do not open email or enter verification codes."
        )
        return (
            "5. Authentication policy: "
            + google_rule
            + " "
            + signup_rule
            + " "
            + verification_rule
            + " After authentication navigation, list tabs and return to the application tab if needed."
        )
    return (
        "5. If login, sign-up, email/SMS verification, SSO, OAuth, or account creation is required, do not "
        "authenticate or create an account. Output RESULT:LOGIN_ISSUE and stop."
    )


def _build_portal_handoff_rule(job: dict) -> str:
    """Describe the portal's external-ATS stop boundary for a browser prompt."""
    policy = config.get_portal_policy(
        job.get("application_url") or job.get("url"),
        source_site=job.get("source_site"),
        site=job.get("site"),
    )
    if not policy or policy.get("external_application_mode") != "manual_reconfirm":
        return ""
    name = str(policy.get("name") or "This portal")
    domains = ", ".join(str(domain) for domain in policy.get("domains", []) if domain)
    if not domains:
        return ""
    return (
        f" This listing originated from {name}. If navigation leaves {domains} for an employer or "
        "external ATS, stop immediately with RESULT:FAILED:manual_review_required:external_ats. "
        "Do not fill, upload, or submit after that hand-off."
    )


def _build_captcha_section() -> str:
    """Build the CAPTCHA detection and solving instructions.

    Reads the CapSolver API key from environment. The CAPTCHA section
    contains no personal data -- it's the same for every user.
    """
    config.load_env()
    capsolver_configured = bool(os.environ.get("CAPSOLVER_API_KEY", ""))
    # This literal is an instruction marker, never the secret. It keeps the
    # legacy browser snippets non-secret while directing the runtime agent to
    # obtain the credential from the process environment.
    capsolver_key = "READ_FROM_CAPSOLVER_API_KEY_ENV_WITHOUT_ECHOING"
    key_instruction = (
        "Read CAPSOLVER_API_KEY from the process environment at execution time. "
        "Never print, echo, persist, or include its value in tool output."
        if capsolver_configured
        else "CAPSOLVER_API_KEY is not configured. Use the manual fallback."
    )

    return f"""== CAPTCHA ==
You solve CAPTCHAs via the CapSolver REST API. No browser extension. You control the entire flow.
Credential handling: {key_instruction}
API base: https://api.capsolver.com

CRITICAL RULE: When ANY CAPTCHA appears (hCaptcha, reCAPTCHA, Turnstile -- regardless of what it looks like visually), you MUST:
1. Run CAPTCHA DETECT to get the type and sitekey
2. Run CAPTCHA SOLVE (createTask -> poll -> inject) with the CapSolver API
3. ONLY go to MANUAL FALLBACK if CapSolver returns errorId > 0
Do NOT skip the API call based on what the CAPTCHA looks like. CapSolver solves CAPTCHAs server-side -- it does NOT need to see or interact with images, puzzles, or games. Even "drag the pipe" or "click all traffic lights" hCaptchas are solved via API token, not visually. ALWAYS try the API first.

--- CAPTCHA DETECT ---
Run this browser_evaluate after every navigation, Apply/Submit/Login click, or when a page feels stuck.
IMPORTANT: Detection order matters. hCaptcha elements also have data-sitekey, so check hCaptcha BEFORE reCAPTCHA.

browser_evaluate function: () => {{{{
  const r = {{}};
  const url = window.location.href;
  // 1. hCaptcha (check FIRST -- hCaptcha uses data-sitekey too)
  const hc = document.querySelector('.h-captcha, [data-hcaptcha-sitekey]');
  if (hc) {{{{
    r.type = 'hcaptcha'; r.sitekey = hc.dataset.sitekey || hc.dataset.hcaptchaSitekey;
  }}}}
  if (!r.type && document.querySelector('script[src*="hcaptcha.com"], iframe[src*="hcaptcha.com"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'hcaptcha'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // 2. Cloudflare Turnstile
  if (!r.type) {{{{
    const cf = document.querySelector('.cf-turnstile, [data-turnstile-sitekey]');
    if (cf) {{{{
      r.type = 'turnstile'; r.sitekey = cf.dataset.sitekey || cf.dataset.turnstileSitekey;
      if (cf.dataset.action) r.action = cf.dataset.action;
      if (cf.dataset.cdata) r.cdata = cf.dataset.cdata;
    }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="challenges.cloudflare.com"]')) {{{{
    r.type = 'turnstile_script_only'; r.note = 'Wait 3s and re-detect.';
  }}}}
  // 3. reCAPTCHA v3 (invisible, loaded via render= param)
  if (!r.type) {{{{
    const s = document.querySelector('script[src*="recaptcha"][src*="render="]');
    if (s) {{{{
      const m = s.src.match(/render=([^&]+)/);
      if (m && m[1] !== 'explicit') {{{{ r.type = 'recaptchav3'; r.sitekey = m[1]; }}}}
    }}}}
  }}}}
  // 4. reCAPTCHA v2 (checkbox or invisible)
  if (!r.type) {{{{
    const rc = document.querySelector('.g-recaptcha');
    if (rc) {{{{ r.type = 'recaptchav2'; r.sitekey = rc.dataset.sitekey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="recaptcha"]')) {{{{
    const el = document.querySelector('[data-sitekey]');
    if (el) {{{{ r.type = 'recaptchav2'; r.sitekey = el.dataset.sitekey; }}}}
  }}}}
  // 5. FunCaptcha (Arkose Labs)
  if (!r.type) {{{{
    const fc = document.querySelector('#FunCaptcha, [data-pkey], .funcaptcha');
    if (fc) {{{{ r.type = 'funcaptcha'; r.sitekey = fc.dataset.pkey; }}}}
  }}}}
  if (!r.type && document.querySelector('script[src*="arkoselabs"], script[src*="funcaptcha"]')) {{{{
    const el = document.querySelector('[data-pkey]');
    if (el) {{{{ r.type = 'funcaptcha'; r.sitekey = el.dataset.pkey; }}}}
  }}}}
  if (r.type) {{{{ r.url = url; return r; }}}}
  return null;
}}}}

Result actions:
- null -> no CAPTCHA. Continue normally.
- "turnstile_script_only" -> browser_wait_for time: 3, re-run detect.
- Any other type -> proceed to CAPTCHA SOLVE below.

--- CAPTCHA SOLVE ---
Three steps: createTask -> poll -> inject. Do each as a separate browser_evaluate call.

STEP 1 -- CREATE TASK (copy this exactly, fill in the 3 placeholders):
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/createTask', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      task: {{{{
        type: 'TASK_TYPE',
        websiteURL: 'PAGE_URL',
        websiteKey: 'SITE_KEY'
      }}}}
    }}}})
  }}}});
  return await r.json();
}}}}

TASK_TYPE values (use EXACTLY these strings):
  hcaptcha     -> HCaptchaTaskProxyLess
  recaptchav2  -> ReCaptchaV2TaskProxyLess
  recaptchav3  -> ReCaptchaV3TaskProxyLess
  turnstile    -> AntiTurnstileTaskProxyLess
  funcaptcha   -> FunCaptchaTaskProxyLess

PAGE_URL = the url from detect result. SITE_KEY = the sitekey from detect result.
For recaptchav3: add "pageAction": "submit" to the task object (or the actual action found in page scripts).
For turnstile: add "metadata": {{"action": "...", "cdata": "..."}} if those were in detect result.

Response: {{"errorId": 0, "taskId": "abc123"}} on success.
If errorId > 0 -> CAPTCHA SOLVE failed. Go to MANUAL FALLBACK.

STEP 2 -- POLL (replace TASK_ID with the taskId from step 1):
Loop: browser_wait_for time: 3, then run:
browser_evaluate function: async () => {{{{
  const r = await fetch('https://api.capsolver.com/getTaskResult', {{{{
    method: 'POST',
    headers: {{{{'Content-Type': 'application/json'}}}},
    body: JSON.stringify({{{{
      clientKey: '{capsolver_key}',
      taskId: 'TASK_ID'
    }}}})
  }}}});
  return await r.json();
}}}}

- status "processing" -> wait 3s, poll again. Max 10 polls (30s).
- status "ready" -> extract token:
    reCAPTCHA: solution.gRecaptchaResponse
    hCaptcha:  solution.gRecaptchaResponse
    Turnstile: solution.token
- errorId > 0 or 30s timeout -> MANUAL FALLBACK.

STEP 3 -- INJECT TOKEN (replace THE_TOKEN with actual token string):

For reCAPTCHA v2/v3:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  document.querySelectorAll('[name="g-recaptcha-response"]').forEach(el => {{{{ el.value = token; el.style.display = 'block'; }}}});
  if (window.___grecaptcha_cfg) {{{{
    const clients = window.___grecaptcha_cfg.clients;
    for (const key in clients) {{{{
      const walk = (obj, d) => {{{{
        if (d > 4 || !obj) return;
        for (const k in obj) {{{{
          if (typeof obj[k] === 'function' && k.length < 3) try {{{{ obj[k](token); }}}} catch(e) {{{{}}}}
          else if (typeof obj[k] === 'object') walk(obj[k], d+1);
        }}}}
      }}}};
      walk(clients[key], 0);
    }}}}
  }}}}
  return 'injected';
}}}}

For hCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const ta = document.querySelector('[name="h-captcha-response"], textarea[name*="hcaptcha"]');
  if (ta) ta.value = token;
  document.querySelectorAll('iframe[data-hcaptcha-response]').forEach(f => f.setAttribute('data-hcaptcha-response', token));
  const cb = document.querySelector('[data-hcaptcha-widget-id]');
  if (cb && window.hcaptcha) try {{{{ window.hcaptcha.getResponse(cb.dataset.hcaptchaWidgetId); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For Turnstile:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('[name="cf-turnstile-response"], input[name*="turnstile"]');
  if (inp) inp.value = token;
  if (window.turnstile) try {{{{ const w = document.querySelector('.cf-turnstile'); if (w) window.turnstile.getResponse(w); }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

For FunCaptcha:
browser_evaluate function: () => {{{{
  const token = 'THE_TOKEN';
  const inp = document.querySelector('#FunCaptcha-Token, input[name="fc-token"]');
  if (inp) inp.value = token;
  if (window.ArkoseEnforcement) try {{{{ window.ArkoseEnforcement.setConfig({{{{data: {{{{blob: token}}}}}}}}) }}}} catch(e) {{{{}}}}
  return 'injected';
}}}}

After injecting: browser_wait_for time: 2, then snapshot.
- Widget gone or green check -> success. Click Submit if needed.
- No change -> click Submit/Verify/Continue button (some sites need it).
- Still stuck -> token may have expired (~2 min lifetime). Re-run from STEP 1.

--- MANUAL FALLBACK ---
You should ONLY be here if CapSolver createTask returned errorId > 0. If you haven't tried CapSolver yet, GO BACK and try it first.
If CapSolver genuinely failed (errorId > 0):
1. Audio challenge: Look for "audio" or "accessibility" button -> click it for an easier challenge.
2. Text/logic puzzles: Solve them yourself. Think step by step. Common tricks: "All but 9 die" = 9 left. "3 sisters and 4 brothers, how many siblings?" = 7.
3. Simple text captchas ("What is 3+7?", "Type the word") -> solve them.
4. All else fails -> Output RESULT:CAPTCHA."""


def build_prompt(job: dict, tailored_resume: str,
                 cover_letter: str | None = None,
                 dry_run: bool = False,
                 worker_id: int = 0,
                 worker_dir: Path | None = None,
                 manual_captcha_relay: bool = False,
                 resume_existing_page: bool = False,
                 submission_phase: str = "submit") -> str:
    """Build the full instruction prompt for the apply agent.

    Loads the user profile and search config internally. All personal data
    comes from the profile -- nothing is hardcoded.

    Args:
        job: Job dict from the database (must have url, title, site,
             application_url, fit_score, tailored_resume_path).
        tailored_resume: Plain-text content of the tailored resume.
        cover_letter: Optional plain-text cover letter content.
        dry_run: If True, tell the agent not to click Submit.
        worker_id: Worker identifier used to isolate upload artifacts.
        worker_dir: Optional already-reset worker directory.

    Returns:
        Complete prompt string for the AI agent.
    """
    profile = config.load_profile()
    search_config = config.load_search_config()
    personal = profile["personal"]
    if submission_phase not in {"prepare", "submit"}:
        raise ValueError(f"Unknown submission phase: {submission_phase}")
    if job.get("tailor_status") != "machine_validated":
        raise ValueError(
            "Tailored resume must be machine_validated before application preparation."
        )
    cover_not_required = job.get("cover_letter_status") == "not_required"
    if (
        not dry_run
        and job.get("cover_letter_status") != "human_approved"
        and not cover_not_required
    ):
        raise ValueError(
            "Application prompt requires a human-approved cover letter; "
            f"current state is {job.get('cover_letter_status') or 'unset'}."
        )

    # --- Resolve resume PDF path ---
    resume_path = job.get("tailored_resume_path")
    if not resume_path:
        raise ValueError(f"No tailored resume for job: {job.get('title', 'unknown')}")

    src_pdf = Path(resume_path).with_suffix(".pdf").resolve()
    if not src_pdf.exists():
        raise ValueError(f"Resume PDF not found: {src_pdf}")

    # Copy to a clean filename for upload (recruiters see the filename)
    full_name = personal["full_name"]
    name_slug = full_name.replace(" ", "_")
    base_worker_dir = worker_dir or (config.APPLY_WORKER_DIR / f"worker-{worker_id}")
    dest_dir = base_worker_dir / "attachments"
    dest_dir.mkdir(parents=True, exist_ok=True)
    upload_pdf = dest_dir / f"{name_slug}_Resume.pdf"
    shutil.copy(str(src_pdf), str(upload_pdf))
    pdf_path = str(upload_pdf)

    # --- Cover letter handling ---
    cover_letter_text = cover_letter or ""
    cl_upload_path = ""
    cl_path = job.get("cover_letter_path")
    cover_is_approved = job.get("cover_letter_status") == "human_approved"
    if cover_is_approved and cl_path and Path(cl_path).exists():
        cl_src = Path(cl_path)
        # Read text from .txt sibling (PDF is binary)
        cl_txt = cl_src.with_suffix(".txt")
        if cl_txt.exists():
            cover_letter_text = cl_txt.read_text(encoding="utf-8")
        elif cl_src.suffix == ".txt":
            cover_letter_text = cl_src.read_text(encoding="utf-8")
        # Upload must be PDF
        cl_pdf_src = cl_src.with_suffix(".pdf")
        if cl_pdf_src.exists():
            cl_upload = dest_dir / f"{name_slug}_Cover_Letter.pdf"
            shutil.copy(str(cl_pdf_src), str(cl_upload))
            cl_upload_path = str(cl_upload)

    # --- Build all prompt sections ---
    profile_summary = _build_profile_summary(profile)
    location_check = _build_location_check(profile, search_config)
    salary_section = _build_salary_section(profile)
    screening_section = _build_screening_section(profile)
    hard_rules = _build_hard_rules(profile)
    captcha_section = _build_captcha_section()
    portal_handoff_rule = _build_portal_handoff_rule(job)

    if not dry_run and not cover_letter_text and not cover_not_required:
        raise ValueError("Approved cover-letter artifact is empty or unreadable; manual review required.")
    if cover_not_required:
        cl_display = "N/A -- this exact application form was manually verified to have no cover-letter field."
    else:
        cl_display = cover_letter_text or "N/A -- no human-approved cover letter is supplied for this preview."

    # Phone digits only (for fields with country prefix)
    phone_digits = "".join(c for c in personal.get("phone", "") if c.isdigit())

    # Preferred display name
    display_name = _preferred_display_name(personal)
    authorized_login_steps = _build_login_steps(profile)

    # Preview mode is a separate workflow, not a weakened submission prompt.
    resume_step = "6. Upload resume. If an old resume is visibly attached, remove it first; if the field is empty, do not look for a delete control. Click the upload control once, call browser_file_upload with the PDF path above, wait for parsing, then snapshot and verify that an uploaded filename or replacement/remove control is visible. Once verified, never click the upload control again. This is the tailored resume for THIS job. Non-negotiable."
    field_review_steps = """8. Check ALL pre-filled fields. ATS systems parse your resume and auto-fill -- it's often WRONG.
   - \"Current Job Title\" or \"Most Recent Title\" -> use the Current Employment title from APPLICANT PROFILE, NOT the target job title or a resume-parser guess.
   - Compare every other field to the APPLICANT PROFILE. Fix mismatches. Fill empty fields.
9. Answer screening questions using the rules above."""

    if dry_run:
        mission_instruction = "Fill and verify this application for human review without submitting it or causing any external communication."
        mission_body = (
            "Populate the real application form accurately from the supplied profile and validated resume, "
            "then stop with the completed form visible for review."
        )
        unexpected_instruction = (
            "Except for the narrowly authorized existing-session Google SSO described below, if the flow requires "
            "account creation, email/SMS verification, an assessment, a CAPTCHA, or any action that sends data "
            "beyond ordinary field entry and file upload, stop and report it for manual review."
            + portal_handoff_rule
        )
        apply_navigation = (
            "Open the application form. You may click an initial Apply link only when it navigates to the form. "
            "If the role accepts applications only by email, do not send email; output "
            "RESULT:FAILED:manual_review_required:email_application."
        )
        login_steps = authorized_login_steps
        cover_steps = (
            "7. Use a cover letter only when the FILES section provides a human-approved PDF/text. "
            "Otherwise leave an optional cover-letter field blank. If it is required, output "
            "RESULT:FAILED:manual_review_required:cover_letter and stop."
        )
        final_steps = """10. Review every populated field against the APPLICANT PROFILE and TAILORED RESUME.
11. STOP before clicking any final Submit, Send, Finish, Complete application, or equivalent control. Do not press Enter while a final submission control is focused. Do not solve a CAPTCHA that gates submission.
12. Take a final screenshot named final-preview.png and leave the completed form at the final review point. Output exactly `RESULT:PREVIEWED` on one line, then `PREVIEW_AUDIT: {json}` on the next line without a Markdown code fence. The JSON object must contain filled_fields, skipped_optional_fields, manual_review_fields, resume_uploaded, cover_letter_used, final_control_label, and submission_attempted. submission_attempted must be false."""
        result_codes = """RESULT:PREVIEWED -- form populated and reviewed without submission
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- a CAPTCHA blocks reaching the review point
RESULT:LOGIN_ISSUE -- authentication or account creation is required
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:manual_review_required:reason -- a human decision or side effect is required
RESULT:FAILED:reason -- any other failure (brief reason)"""
        captcha_section = """== CAPTCHA IN PREVIEW MODE ==
An invisible or background hCaptcha/reCAPTCHA iframe is normal on many ATS pages and is not, by itself, a blocked CAPTCHA. Do not interact with any CAPTCHA iframe, checkbox, image, audio, refresh, accessibility, or language control. Continue filling while the ordinary form controls remain usable. If a visible challenge actually prevents reaching the human-review point, take a screenshot named captcha-blocked.png, output RESULT:CAPTCHA, and stop immediately. Never solve, test, refresh, or bypass a CAPTCHA in preview mode."""
        captcha_navigation_instruction = (
            "browser_snapshot to read the page. Ignore background CAPTCHA iframes. "
            "Do not click or test CAPTCHA controls. If a visible challenge blocks ordinary form controls, "
            "take captcha-blocked.png, output RESULT:CAPTCHA, and stop."
        )
        captcha_efficiency_instruction = (
            "CAPTCHA SAFETY: never interact with a CAPTCHA in preview mode. A hidden iframe is not a blocker; "
            "a visible challenge is an immediate screenshot-and-stop condition."
        )
        form_validation_tip = "If the page shows validation warnings before submission, capture a snapshot and screenshot, fix only fields supported by the profile, and never use the final submit control to probe for errors."
    else:
        mission_instruction = "Complete and submit this one application after all required checks pass."
        mission_body = "Submit a complete, accurate application. Use the profile and resume as source data -- adapt to fit each form's format."
        unexpected_instruction = "If something unexpected happens and these instructions don't cover it, figure it out yourself while staying within the hard safety rules."
        apply_navigation = f"""Find and click the Apply button. If email-only (page says \"email resume to X\"):
   - send_email with subject \"Application for {job['title']} -- {display_name}\", body = 2-3 sentence pitch + contact info, attach resume PDF: [\"{pdf_path}\"]
   - Output RESULT:APPLIED. Done.
   After clicking Apply: browser_snapshot. Run CAPTCHA DETECT -- many sites trigger CAPTCHAs right after the Apply click. If found, solve before continuing."""
        login_steps = authorized_login_steps
        if cover_not_required:
            cover_steps = (
                "7. This exact form was previously verified to have no cover-letter field. "
                "If one is now required, output RESULT:FAILED:manual_review_required:cover_letter and stop."
            )
        else:
            cover_steps = "7. Upload cover letter if there's a field for it. Text field -> paste the cover letter text. File upload -> use the cover letter PDF path."
        final_steps = """10. BEFORE clicking Submit/Apply, take a snapshot and review EVERY field on the page. Verify all data matches the APPLICANT PROFILE and TAILORED RESUME -- name, email, phone, location, work auth, resume uploaded, cover letter if applicable. If anything is wrong or missing, fix it FIRST. Only click Submit after confirming everything is correct.
11. After submit: browser_snapshot. Run CAPTCHA DETECT -- submit buttons often trigger invisible CAPTCHAs. If found, solve it (the form will auto-submit once the token clears, or you may need to click Submit again). Then check for new tabs (browser_tabs action: \"list\"). Switch to newest, close old. Snapshot to confirm submission. Look for \"thank you\" or \"application received\".
12. Output your result."""
        result_codes = """RESULT:APPLIED -- submitted successfully
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- blocked by unsolvable captcha
RESULT:LOGIN_ISSUE -- could not sign in or create account
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:reason -- any other failure (brief reason)"""
        captcha_navigation_instruction = (
            "browser_snapshot to read the page. Then run CAPTCHA DETECT (see CAPTCHA section). "
            "If a CAPTCHA is found, solve it before continuing."
        )
        captcha_efficiency_instruction = (
            "CAPTCHA AWARENESS: After any navigation, Apply/Submit/Login click, or when a page feels stuck -- "
            "run CAPTCHA DETECT (see CAPTCHA section). Invisible CAPTCHAs (Turnstile, reCAPTCHA v3) show NO "
            "visual widget but block form submissions silently. The detect script finds them even when invisible."
        )
        form_validation_tip = "Validation errors after submit? Take BOTH snapshot AND screenshot. Snapshot shows text errors, screenshot shows red-highlighted fields. Fix all, retry."

    if not dry_run and submission_phase == "prepare":
        mission_instruction = "Prepare and review this one application, but do not submit it."
        mission_body = (
            "Populate the real application form accurately from the supplied profile and validated resume, "
            "verify every required field, and stop before the final submission control."
        )
        unexpected_instruction = (
            "Do not cause external submission or communication. If a CAPTCHA blocks ordinary form interaction, "
            "use the configured CAPTCHA result path and stop."
        )
        apply_navigation = (
            "Open the ordinary application form without submitting it. If the role accepts applications only by "
            "email, do not send email; output RESULT:FAILED:manual_review_required:email_application."
        )
        final_steps = """10. BEFORE any submission action, snapshot and review EVERY field. Verify legal name, email, phone, Singapore location, current company, work authorization, availability answers, required screening responses, and the uploaded resume. Fix supported errors, then save a screenshot named pre-submit-review.png.
11. STOP before clicking Submit/Apply/Send/Finish/Complete application or any equivalent final control. Do not press Enter while that control is focused.
12. Output RESULT:READY_TO_SUBMIT only when the completed form is visible at the final review point. The launcher will perform an independent browser-side audit before a separate submission phase."""
        result_codes = """RESULT:READY_TO_SUBMIT -- form completed and waiting for deterministic audit
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- a visible CAPTCHA blocks ordinary form interaction
RESULT:LOGIN_ISSUE -- could not sign in or create account
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:reason -- any other failure (brief reason)"""

    if not dry_run and submission_phase == "submit":
        mission_instruction = "Submit the already prepared and independently audited application exactly once."
        mission_body = (
            "The visible application form has already been populated and audited by the launcher. Preserve its "
            "current values and perform only the final confirmation/submission sequence."
        )
        resume_step = "6. Do not remove, replace, or re-upload the audited resume. If it is visibly missing, output RESULT:FAILED:pre_submit_state_changed and stop."
        field_review_steps = """8. Do not rewrite or clear audited profile fields. Snapshot the current page and stop if any required value is visibly missing.
9. Do not change audited screening answers. The only authorized side effect in this phase is one final submission click."""

    if manual_captcha_relay:
        captcha_section = """== MANUAL CAPTCHA RELAY ==
Never solve, test, refresh, bypass, or interact with a CAPTCHA. Hidden/background CAPTCHA iframes are not blockers. If a visible challenge prevents ordinary form interaction, take captcha-blocked.png, output RESULT:CAPTCHA, and stop immediately. The launcher will keep Edge open for the applicant to solve it manually, then start a fresh supervised agent turn."""
        captcha_navigation_instruction = (
            "browser_snapshot to read the page. Do not interact with CAPTCHA controls. "
            "If a visible challenge blocks the form, take captcha-blocked.png, output RESULT:CAPTCHA, and stop."
        )
        captcha_efficiency_instruction = (
            "CAPTCHA RELAY: hidden iframes are ignored; a visible challenge is screenshot-and-stop. "
            "The applicant, not the agent, completes it."
        )
        if not dry_run:
            apply_navigation = (
                "Find and click the Apply button only when it navigates to the ordinary application form. "
                "After navigation, snapshot the form. If a visible CAPTCHA blocks it, take captcha-blocked.png, "
                "output RESULT:CAPTCHA, and stop for the applicant's manual relay. Email-only applications "
                "require manual review; do not send email."
            )
            login_steps = authorized_login_steps
            if submission_phase == "prepare":
                final_steps = """10. Snapshot and review EVERY field. Verify legal name, email, phone, Singapore location, current company, availability answers, required screening responses, and the uploaded resume. Fix supported errors, then save pre-submit-review.png.
11. STOP before clicking the final submission control. Do not press Enter while it is focused.
12. Output RESULT:READY_TO_SUBMIT only when the form is complete and ready for the launcher's independent audit."""
                result_codes = """RESULT:READY_TO_SUBMIT -- form completed and waiting for deterministic audit
RESULT:EXPIRED -- job closed or no longer accepting applications
RESULT:CAPTCHA -- a visible CAPTCHA blocks ordinary form interaction
RESULT:LOGIN_ISSUE -- authentication or account creation is required
RESULT:FAILED:not_eligible_location -- onsite outside acceptable area, no remote option
RESULT:FAILED:not_eligible_work_auth -- requires unauthorized work location
RESULT:FAILED:reason -- any other failure (brief reason)"""
            else:
                final_steps = """10. Snapshot the current audited form without changing its fields. If any required value or the resume is visibly missing, output RESULT:FAILED:pre_submit_state_changed and stop.
11. Click the final submission control exactly once. Snapshot immediately afterward. If a visible CAPTCHA appears, take captcha-blocked.png, output RESULT:CAPTCHA, and stop without clicking Submit again. If a confirmation page is already visible, do not submit again; save submission-confirmation.png and output RESULT:APPLIED.
12. Output RESULT:APPLIED only after the page visibly confirms that the application was received and submission-confirmation.png has been saved. Otherwise output the applicable failure result."""

    if resume_existing_page:
        opening_steps = (
            "1. Do not navigate or reload. The visible Edge session is already on this exact application after "
            "a manual CAPTCHA handoff. Snapshot the current page first. If an application confirmation is already "
            "visible, output RESULT:APPLIED immediately without clicking Submit again.\n"
            "2. If the application form is visible, continue from its current state without clearing existing fields. "
            "In prepare phase, finish and verify the form; in submit phase, preserve the independently audited state."
        )
    else:
        opening_steps = f"1. browser_navigate to the job URL.\n2. {captcha_navigation_instruction}"

    prompt = f"""You are a job application assistant. {mission_instruction}

== REQUIRED BROWSER CONTROL ==
The `playwright` MCP server is already attached to the visible isolated Edge session. Use only its browser_* MCP tools for all browser interaction. Do not invoke shell commands, Skills, agent-browser, npx, Playwright CLI, browser-use, computer-use, or any other browser automation route. If the attached browser MCP is unavailable, output RESULT:FAILED:browser_mcp_unavailable and stop.

== FIELD IDENTITY RULES ==
- Full name and all first/given/last/family/surname fields use the legal identity from APPLICANT PROFILE. Preferred/display name is used only when the label explicitly asks for it.
- Current location/city/country fields use Singapore. Use the full street address only when the form actually asks for address fields.
- Current company and current title use the Current Employment record, not a resume-parser guess.
- For a full-time internship tied to a stated start month, answer Yes only if the exact full-time availability in the profile meets that month. The generic application date is 2026-10-15; full-time credit-bearing availability begins January 2027. Therefore a question that specifically requires full-time starting September must be answered No.

== JOB ==
URL: {job.get('application_url') or job['url']}
Title: {job['title']}
Company: {job.get('company_name') or 'Unknown employer'}
Discovery source: {job.get('source_site') or job.get('site') or 'Unknown'}
Fit Score: {job.get('fit_score', 'N/A')}/10

== FILES ==
Resume PDF (upload this): {pdf_path}
Cover Letter PDF (upload if asked): {cl_upload_path or "N/A"}

== RESUME TEXT (use when filling text fields) ==
{tailored_resume}

== COVER LETTER TEXT (paste if text field, upload PDF if file field) ==
{cl_display}

== APPLICANT PROFILE ==
{profile_summary}

== YOUR MISSION ==
{mission_body}

{unexpected_instruction}

{hard_rules}

== NEVER DO THESE (immediate RESULT:FAILED if encountered) ==
- NEVER grant camera, microphone, screen sharing, or location permissions. If a site requests them -> RESULT:FAILED:unsafe_permissions
- NEVER do video/audio verification, selfie capture, ID photo upload, or biometric anything -> RESULT:FAILED:unsafe_verification
- NEVER set up a freelancing profile (Mercor, Toptal, Upwork, Fiverr, Turing, etc.). These are contractor marketplaces, not job applications -> RESULT:FAILED:not_a_job_application
- NEVER set up a contractor/freelancer rate or availability-calendar profile. This workflow may apply to internships or full-time employment, but not long-term contractor marketplaces. A short-term practice contract requires manual review.
- NEVER install browser extensions, download executables, or run assessment software.
- NEVER enter payment info, bank details, or SSN/SIN.
- NEVER click "Allow" on any browser permission popup. Always deny/block.
- If the site is NOT a job application form (it's a profile builder, skills marketplace, talent network signup, coding assessment platform) -> RESULT:FAILED:not_a_job_application

{location_check}

{salary_section}

{screening_section}

== STEP-BY-STEP ==
{opening_steps}
3. LOCATION CHECK. Read the page for location info. If not eligible, output RESULT and stop.
4. {apply_navigation}
{login_steps}
{resume_step}
{cover_steps}
{field_review_steps}
{final_steps}

== RESULT CODES (output EXACTLY one) ==
{result_codes}

== BROWSER EFFICIENCY ==
- browser_snapshot ONCE per page to understand it. Then use browser_take_screenshot to check results (10x less memory).
- Only snapshot again when you need element refs to click/fill.
- Multi-page forms (Workday, Taleo, iCIMS): snapshot each new page, fill all fields, click Next/Continue. Repeat until final review page.
- Fill ALL fields in ONE browser_fill_form call. Not one at a time.
- Keep your thinking SHORT. Don't repeat page structure back.
- {captcha_efficiency_instruction}

== FORM TRICKS ==
- LinkedIn job detail: the primary Easy Apply control may be an `a` link, not a button. Select only the control whose accessible name/aria-label is exactly or starts with "Easy Apply to this job" for the current JOB URL. Do not click an "Easy Apply" job-type chip or a similar-job card. Click the exact control once and snapshot. If no application dialog opens, read that control's href and browser_navigate to it only when its path is the current exact job URL plus `/apply/` and its query contains `openSDUIApplyFlow=true`; otherwise stop with RESULT:FAILED:linkedin_apply_entry_mismatch.
- Popup/new window opened? browser_tabs action "list" to see all tabs. browser_tabs action "select" with the tab index to switch. ALWAYS check for new tabs after clicking login/apply/sign-in buttons.
- "Upload your resume" pre-fill page (Workday, Lever, etc.): This is NOT the application form yet. Click "Select file" or the upload area, then browser_file_upload with the resume PDF path. Wait for parsing to finish. Then click Next/Continue to reach the actual form.
- File upload verification: after browser_file_upload, wait and snapshot. Continue only when the filename or a remove/replace control proves the file is attached. Do not click the upload area again after success. If no proof appears, retry the click-plus-upload sequence once, snapshot again, then fail with RESULT:FAILED:resume_upload if still empty.
- Native dropdown/combobox: use browser_select_option directly with the exact visible option text. Snapshot afterward and verify the selected option. Use click-the-option only for a custom non-native dropdown.
- Lever ordinary application form: select native comboboxes first, upload and verify the resume second, then populate all visible text fields in one browser_fill_form call. Select radio answers with browser_click, fill long-answer textboxes, and snapshot once to verify that required fields are no longer blank. If fields remain blank, retry the failed operation once using the new refs from that snapshot; do not repeat unrelated clicks or declare progress without visible state change.
- Checkbox won't check via fill_form? Use browser_click on it instead. Snapshot to verify.
- Phone field with country prefix: just type digits {phone_digits}
- Date fields: {datetime.now().astimezone().strftime('%m/%d/%Y')}
- {form_validation_tip}
- Honeypot fields (hidden, "leave blank"): skip them.
- Format-sensitive fields: read the placeholder text, match it exactly.

{captcha_section}

== WHEN TO GIVE UP ==
- Same page after 3 attempts with no progress -> RESULT:FAILED:stuck
- Job is closed/expired/page says "no longer accepting" -> RESULT:EXPIRED
- Page is broken/500 error/blank -> RESULT:FAILED:page_error
Stop immediately. Output your RESULT code. Do not loop."""

    return prompt
