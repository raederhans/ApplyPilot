"""ApplyPilot configuration: paths, platform detection, user data."""

import os
import platform
import shutil
from pathlib import Path
from urllib.parse import urlparse

# User data directory — all user-specific files live here
APP_DIR = Path(os.environ.get("APPLYPILOT_DIR", Path.home() / ".applypilot"))

# Core paths
DB_PATH = APP_DIR / "applypilot.db"
PROFILE_PATH = APP_DIR / "profile.json"
RESUME_PATH = APP_DIR / "resume.txt"
RESUME_PDF_PATH = APP_DIR / "resume.pdf"
SEARCH_CONFIG_PATH = APP_DIR / "searches.yaml"
RADAR_CONFIG_PATH = APP_DIR / "radar.yaml"
ENV_PATH = APP_DIR / ".env"
RADAR_IMPORT_DIR = APP_DIR / "radar-imports"
RADAR_REPORT_DIR = APP_DIR / "reports"

# Generated output
TAILORED_DIR = APP_DIR / "tailored_resumes"
COVER_LETTER_DIR = APP_DIR / "cover_letters"
LOG_DIR = APP_DIR / "logs"

# Chrome worker isolation
CHROME_WORKER_DIR = APP_DIR / "chrome-workers"
APPLY_WORKER_DIR = APP_DIR / "apply-workers"

# Package-shipped config (YAML registries)
PACKAGE_DIR = Path(__file__).parent
CONFIG_DIR = PACKAGE_DIR / "config"


def get_chrome_path() -> str:
    """Auto-detect a supported Chromium browser executable.

    Override with CHROME_PATH environment variable.
    """
    env_path = os.environ.get("CHROME_PATH")
    if env_path and Path(env_path).exists():
        return env_path

    system = platform.system()

    if system == "Windows":
        candidates = [
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif system == "Darwin":
        candidates = [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ]
    else:  # Linux
        candidates = []
        for name in ("google-chrome", "google-chrome-stable", "chromium-browser", "chromium"):
            found = shutil.which(name)
            if found:
                candidates.append(Path(found))

    for c in candidates:
        if c and c.exists():
            return str(c)

    # Fall back to PATH search
    for name in ("msedge", "microsoft-edge", "google-chrome", "google-chrome-stable", "chromium-browser", "chromium", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    raise FileNotFoundError(
        "Edge/Chrome/Chromium not found. Install a Chromium browser or set CHROME_PATH."
    )


def get_chrome_user_data() -> Path:
    """Return the source browser profile directory when profile cloning is enabled."""
    env_path = os.environ.get("CHROME_USER_DATA_DIR")
    if env_path:
        return Path(env_path)

    system = platform.system()
    if system == "Windows":
        browser_path = Path(get_chrome_path()).name.lower()
        if browser_path == "msedge.exe":
            return Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Edge" / "User Data"
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return Path.home() / ".config" / "google-chrome"


def ensure_dirs():
    """Create all required directories."""
    for d in [APP_DIR, TAILORED_DIR, COVER_LETTER_DIR, LOG_DIR, CHROME_WORKER_DIR, APPLY_WORKER_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def ensure_radar_dirs() -> None:
    """Create only the storage used by discovery-only radar commands."""
    for directory in (APP_DIR, RADAR_IMPORT_DIR, RADAR_REPORT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def load_profile() -> dict:
    """Load user profile from ~/.applypilot/profile.json."""
    import json
    if not PROFILE_PATH.exists():
        raise FileNotFoundError(
            f"Profile not found at {PROFILE_PATH}. Run `applypilot init` first."
        )
    return json.loads(PROFILE_PATH.read_text(encoding="utf-8"))


def load_search_config() -> dict:
    """Load search configuration from ~/.applypilot/searches.yaml."""
    import yaml
    if not SEARCH_CONFIG_PATH.exists():
        # Fall back to package-shipped example
        example = CONFIG_DIR / "searches.example.yaml"
        if example.exists():
            return yaml.safe_load(example.read_text(encoding="utf-8"))
        return {}
    return yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8"))


def load_radar_config() -> dict:
    """Load the Singapore radar policy without inheriting the US example.

    A user-owned ``radar.yaml`` has highest priority.  Existing ApplyPilot
    users may keep location/title policy in ``searches.yaml``; that file is
    reused only when it actually exists.  A fresh radar installation falls
    back to the package's Singapore-specific example.
    """
    import yaml

    if RADAR_CONFIG_PATH.exists():
        return yaml.safe_load(RADAR_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if SEARCH_CONFIG_PATH.exists():
        return yaml.safe_load(SEARCH_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    example = CONFIG_DIR / "radar.example.yaml"
    if example.exists():
        return yaml.safe_load(example.read_text(encoding="utf-8")) or {}
    return {}


def get_location_filters(search_config: dict | None = None) -> tuple[list[str], list[str]]:
    """Return normalized accept/reject location patterns.

    Current search files use ``location.accept_patterns`` and
    ``location.reject_patterns``.  The legacy flat keys remain supported so
    existing user configurations continue to work during the migration.
    """
    search_config = search_config if search_config is not None else load_search_config()
    location = search_config.get("location", {})
    if not isinstance(location, dict):
        location = {}
    accept = location.get("accept_patterns")
    reject = location.get("reject_patterns")
    if accept is None:
        accept = search_config.get("location_accept", [])
    if reject is None:
        reject = search_config.get("location_reject_non_remote", [])
    return (
        [str(value) for value in (accept or []) if str(value).strip()],
        [str(value) for value in (reject or []) if str(value).strip()],
    )


def get_excluded_title_patterns(search_config: dict | None = None) -> list[str]:
    """Return configured case-insensitive title exclusions."""
    search_config = search_config if search_config is not None else load_search_config()
    values = search_config.get("exclude_titles", [])
    if not isinstance(values, list):
        return []
    return [str(value).casefold().strip() for value in values if str(value).strip()]


def title_is_excluded(
    title: str | None,
    patterns: list[str] | None = None,
    *,
    search_config: dict | None = None,
) -> bool:
    """Return whether a title contains one of the configured exclusions."""
    if not title:
        return False
    normalized = " ".join(str(title).casefold().split())
    selected = patterns if patterns is not None else get_excluded_title_patterns(search_config)
    return any(pattern and pattern in normalized for pattern in selected)


def location_is_accepted(
    location: str | None,
    accept_patterns: list[str],
    reject_patterns: list[str],
    *,
    keep_unknown: bool = True,
) -> bool:
    """Apply one shared, case-insensitive location policy."""
    if not location:
        return keep_unknown
    normalized = " ".join(str(location).casefold().split())
    if any(pattern.casefold() in normalized for pattern in reject_patterns):
        return False
    if not accept_patterns:
        return True
    return any(pattern.casefold() in normalized for pattern in accept_patterns)


def radar_location_is_accepted(
    location: str | None,
    accept_patterns: list[str],
    reject_patterns: list[str],
    *,
    allow_ambiguous_remote: bool = False,
) -> bool:
    """Apply a strict geographic policy to global official-career feeds.

    Generic ``Remote`` or ``Hybrid`` text does not prove that a role can be
    performed from Singapore. It is accepted only when the location also
    names one of the configured geographic scopes, unless a user explicitly
    opts into ambiguous remote listings.
    """
    if not location:
        return False
    normalized = " ".join(str(location).casefold().split())
    if any(pattern.casefold() in normalized for pattern in reject_patterns):
        return False
    non_geographic = {"remote", "hybrid", "anywhere"}
    geographic_patterns = [
        pattern
        for pattern in accept_patterns
        if pattern.casefold().strip() not in non_geographic
    ]
    if any(pattern.casefold() in normalized for pattern in geographic_patterns):
        return True
    return allow_ambiguous_remote and any(
        marker in normalized for marker in non_geographic
    )


def load_sites_config() -> dict:
    """Load sites.yaml configuration (sites list, manual_ats, blocked, etc.)."""
    import yaml
    path = CONFIG_DIR / "sites.yaml"
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _host_matches(host: str, candidate: str) -> bool:
    """Return whether ``host`` is exactly or is a subdomain of ``candidate``."""
    normalized_host = host.lower().strip(".")
    normalized_candidate = candidate.lower().strip(".")
    return bool(normalized_host and normalized_candidate) and (
        normalized_host == normalized_candidate
        or normalized_host.endswith(f".{normalized_candidate}")
    )


def _normalise_site_name(value: str | None) -> str:
    """Normalise a source-site label for a policy match without guessing it."""
    return " ".join((value or "").casefold().split())


def load_portal_policies() -> list[dict]:
    """Load opt-in portal controls from the package site registry.

    Portal policies are separate from searchable sites so an application rule
    cannot accidentally enable automated discovery for that portal.
    """
    policies = load_sites_config().get("portal_policies", [])
    if not isinstance(policies, list):
        return []
    return [policy for policy in policies if isinstance(policy, dict)]


def get_portal_policy(
    url: str | None = None,
    *,
    source_site: str | None = None,
    site: str | None = None,
) -> dict | None:
    """Return the configured portal policy for a URL or recorded source.

    The source-site fallback is intentional. A JobStreet listing that opens an
    employer's ATS is still governed by JobStreet's manual-only policy until a
    candidate explicitly takes over that external application themselves.
    """
    host = (urlparse(url or "").hostname or "").lower()
    source_names = {
        _normalise_site_name(value)
        for value in (source_site, site)
        if _normalise_site_name(value)
    }

    for policy in load_portal_policies():
        domains = policy.get("domains", [])
        if isinstance(domains, str):
            domains = [domains]
        if host and any(
            _host_matches(host, domain)
            for domain in domains
            if isinstance(domain, str)
        ):
            return policy

        policy_names = policy.get("site_names", [])
        if isinstance(policy_names, str):
            policy_names = [policy_names]
        names = {
            _normalise_site_name(value)
            for value in policy_names
            if isinstance(value, str) and _normalise_site_name(value)
        }
        if source_names.intersection(names):
            return policy
    return None


def portal_application_gate(
    url: str | None = None,
    *,
    source_site: str | None = None,
    site: str | None = None,
    preview_only: bool,
) -> str | None:
    """Return a human-action requirement, or ``None`` when browser use is allowed.

    Unknown or malformed modes fail closed for a matching portal. This helper
    intentionally decides only browser access; it never treats a portal record
    as evidence that an application was submitted.
    """
    policy = get_portal_policy(url, source_site=source_site, site=site)
    if policy is None:
        return None

    name = str(policy.get("name") or "configured portal")
    mode = str(policy.get("application_mode") or "").casefold()
    if mode == "manual_only":
        return f"{name} requires a candidate-operated manual application."

    if mode == "standing_authorized":
        return None

    domains = policy.get("domains", [])
    if isinstance(domains, str):
        domains = [domains]
    host = (urlparse(url or "").hostname or "").lower()
    if (
        policy.get("external_application_mode") == "manual_reconfirm"
        and host
        and domains
        and not any(
            _host_matches(host, domain)
            for domain in domains
            if isinstance(domain, str)
        )
    ):
        return (
            f"{name} handed off to an external ATS; the candidate must reconfirm and continue manually."
        )

    if mode == "review_only":
        if preview_only:
            return None
        return (
            f"{name} permits only a visible fill-only review; "
            "the candidate must submit manually."
        )
    return f"{name} has an unrecognised application policy and is manual-only."


def portal_discovery_gate(
    url: str | None = None,
    *,
    source_site: str | None = None,
    site: str | None = None,
) -> str | None:
    """Return why a listing must not be fetched by the automated detail crawler.

    Candidate-provided listings remain welcome in the local database. This
    guard prevents a later generic enrichment run from silently turning that
    local intake into scripted portal access.
    """
    policy = get_portal_policy(url, source_site=source_site, site=site)
    if policy is None:
        return None

    name = str(policy.get("name") or "configured portal")
    mode = str(policy.get("discovery_mode") or "").casefold()
    if mode == "authorized_import_only":
        return (
            f"{name} listings must come from an authorised export or "
            "candidate-provided CSV; automated portal enrichment is disabled."
        )
    if mode == "manual_only":
        return f"{name} requires candidate-operated browsing; automated portal enrichment is disabled."
    if mode == "visible_agent_browse":
        return (
            f"{name} permits bounded visible agent browsing in the authenticated user session; "
            "the unattended detail crawler remains disabled."
        )
    return f"{name} has an unrecognised discovery policy and is excluded from automated enrichment."


def is_manual_ats(url: str | None) -> bool:
    """Check if a URL routes through an ATS that requires manual application."""
    if not url:
        return False
    sites_cfg = load_sites_config()
    domains = sites_cfg.get("manual_ats", [])
    url_lower = url.lower()
    return any(domain in url_lower for domain in domains)


def load_blocked_sites() -> tuple[set[str], list[str]]:
    """Load blocked sites and URL patterns from sites.yaml.

    Returns:
        (blocked_site_names, blocked_url_patterns)
    """
    cfg = load_sites_config()
    blocked = cfg.get("blocked", {})
    sites = set(blocked.get("sites", []))
    patterns = blocked.get("url_patterns", [])
    return sites, patterns


def load_blocked_sso() -> list[str]:
    """Load blocked SSO domains from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("blocked_sso", [])


def load_base_urls() -> dict[str, str | None]:
    """Load site base URLs for URL resolution from sites.yaml."""
    cfg = load_sites_config()
    return cfg.get("base_urls", {})


# ---------------------------------------------------------------------------
# Default values — referenced across modules instead of magic numbers
# ---------------------------------------------------------------------------

DEFAULTS = {
    "min_score": 6,
    "max_apply_attempts": 3,
    "max_tailor_attempts": 5,
    "poll_interval": 60,
    "apply_timeout": 300,
    "viewport": "1280x900",
}


def load_env():
    """Load environment variables from ~/.applypilot/.env if it exists."""
    from dotenv import load_dotenv
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH)
    # Also try CWD .env as fallback
    load_dotenv()


def has_llm_provider() -> bool:
    """Return whether a usable configured LLM provider is available."""
    if any(os.environ.get(k) for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")):
        return True

    local_url = os.environ.get("LLM_URL", "")
    if not local_url:
        return False

    host = (urlparse(local_url).hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    return bool(os.environ.get("LLM_API_KEY"))


# ---------------------------------------------------------------------------
# Tier system — feature gating by installed dependencies
# ---------------------------------------------------------------------------

TIER_LABELS = {
    1: "Discovery",
    2: "AI Scoring & Tailoring",
    3: "Full Auto-Apply",
}

TIER_COMMANDS: dict[int, list[str]] = {
    1: ["init", "run discover", "run enrich", "status", "dashboard"],
    2: ["run score", "run tailor", "run cover", "run pdf", "run"],
    3: ["apply"],
}


def get_tier() -> int:
    """Detect the current tier based on available dependencies.

    Tier 1 (Discovery):            Python + pip
    Tier 2 (AI Scoring & Tailoring): + LLM API key
    Tier 3 (Full Auto-Apply):       + Claude Code CLI + Chrome
    """
    load_env()

    has_llm = has_llm_provider()
    if not has_llm:
        return 1

    has_claude = shutil.which("claude") is not None
    try:
        get_chrome_path()
        has_chrome = True
    except FileNotFoundError:
        has_chrome = False

    if has_claude and has_chrome:
        return 3

    return 2


def check_tier(required: int, feature: str) -> None:
    """Raise SystemExit with a clear message if the current tier is too low.

    Args:
        required: Minimum tier needed (1, 2, or 3).
        feature: Human-readable description of the feature being gated.
    """
    current = get_tier()
    if current >= required:
        return

    from rich.console import Console
    _console = Console(stderr=True)

    missing: list[str] = []
    if required >= 2 and not has_llm_provider():
        missing.append("LLM provider — set Gemini, OpenAI, DeepSeek, or a local OpenAI-compatible endpoint")
    if required >= 3:
        if not shutil.which("claude"):
            missing.append("Claude Code CLI — install from [bold]https://claude.ai/code[/bold]")
        try:
            get_chrome_path()
        except FileNotFoundError:
            missing.append("Edge/Chrome/Chromium — install or set CHROME_PATH")

    _console.print(
        f"\n[red]'{feature}' requires {TIER_LABELS.get(required, f'Tier {required}')} (Tier {required}).[/red]\n"
        f"Current tier: {TIER_LABELS.get(current, f'Tier {current}')} (Tier {current})."
    )
    if missing:
        _console.print("\n[yellow]Missing:[/yellow]")
        for m in missing:
            _console.print(f"  - {m}")
    _console.print()
    raise SystemExit(1)
