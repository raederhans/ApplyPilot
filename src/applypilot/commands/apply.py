"""Execution body for the ApplyPilot apply command."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


class CommandConsole(Protocol):
    """Small output port used by command bodies."""

    def print(self, *objects: object, **kwargs: object) -> None: ...


class ApplyCommandRuntime(Protocol):
    """CLI-owned services needed to execute one apply command."""

    @property
    def console(self) -> CommandConsole: ...

    @property
    def environment(self) -> Mapping[str, str]: ...

    def bootstrap(self) -> None: ...

    def standing_auto_authorization_enabled(self, profile: dict) -> bool: ...

    def build_standing_authorization_manifest(
        self,
        connection: Any,
        *,
        profile: dict,
        target_url: str | None,
        requested_limit: int,
        min_score: int,
    ) -> dict: ...

    def exit_exception(self, code: int) -> BaseException: ...


@dataclass(frozen=True, slots=True)
class ApplyCommandOptions:
    """Immutable projection of Typer's ``apply`` options."""

    limit: int | None = None
    workers: int = 1
    min_score: int = 6
    model: str | None = None
    agent_backend: str | None = None
    browser_backend: str | None = None
    interaction_mode: str | None = None
    continuous: bool = False
    dry_run: bool = False
    manual_captcha_relay: bool = False
    headless: bool = False
    url: str | None = None
    authorization_file: Path | None = None
    final_authorization_file: Path | None = None
    gen: bool = False
    mark_applied: str | None = None
    mark_failed: str | None = None
    fail_reason: str | None = None
    reset_failed: bool = False
    reset_failed_url: str | None = None


def _select_runnable_browser_backend(
    requested: str,
    resolve_executable: Callable[[str], str],
) -> tuple[str, dict[str, str]]:
    """Select an installed runtime without making ``auto`` require both.

    ``auto`` keeps the Edge-to-Cloak fallback only when both runtimes are
    available.  When exactly one optional runtime is unavailable, execution
    continues on the installed runtime and returns the unavailable detail for
    an explicit operator warning.
    """
    candidates = ("edge", "cloak") if requested == "auto" else (requested,)
    available: list[str] = []
    unavailable: dict[str, str] = {}
    for backend in candidates:
        try:
            resolve_executable(backend)
        except (FileNotFoundError, RuntimeError) as exc:
            unavailable[backend] = str(exc)
        else:
            available.append(backend)

    if not available:
        details = "; ".join(
            f"{backend}: {reason}" for backend, reason in unavailable.items()
        )
        raise RuntimeError(details or f"{requested} browser is unavailable")
    if requested == "auto":
        if len(available) == len(candidates):
            return "auto", {}
        return available[0], unavailable
    return requested, {}


def _worker_summary_lines(
    allocation: dict[str, int],
    *,
    dry_run: bool,
    preview_selection: str | None = None,
    preview_candidates: int = 0,
) -> tuple[list[str], int]:
    """Describe the worker pool without mixing preview and submission counts."""
    if dry_run:
        return (
            [
                f"  Preview selection:       {preview_selection or 'queue'}",
                f"  Preview candidates cap:  {preview_candidates}",
                f"  Workers passed to launcher: {allocation['effective_workers']}",
            ],
            allocation["effective_workers"],
        )
    return (
        [
            f"  Manifest-bound:     {allocation['bound_candidates']}",
            f"  Executable:         {allocation['executable_candidates']}",
            f"  Blocked:            {allocation['blocked_candidates']}",
            f"  Workers effective:  {allocation['effective_workers']}",
        ],
        allocation["effective_workers"],
    )


def _manual_captcha_relay_enabled(
    cli_requested: bool,
    profile: Mapping[str, object],
    *,
    dry_run: bool = False,
    continuous: bool = False,
    headless: bool = False,
    workers: int = 1,
    limit: int | None = None,
) -> bool:
    """Apply the persisted relay preference only to bounded visible submissions.

    An explicit CLI request is deliberately preserved for the caller's strict
    mode validation.  A persisted preference is a convenience for real,
    attended submissions, not an instruction to relay CAPTCHAs during previews
    or unbounded/background work.
    """
    submission_policy = profile.get("submission_policy")
    profile_enabled = bool(
        isinstance(submission_policy, Mapping)
        and submission_policy.get("manual_captcha_relay") is True
    )
    profile_scope_is_valid = (
        not dry_run
        and not continuous
        and not headless
        and workers == 1
        and limit in (None, 1)
    )
    return bool(cli_requested or (profile_enabled and profile_scope_is_valid))


def run_apply(
    runtime: ApplyCommandRuntime,
    options: ApplyCommandOptions,
) -> None:
    """Prepare one application, or submit under workspace policy/one-off authorization."""
    limit = options.limit
    workers = options.workers
    min_score = options.min_score
    model = options.model
    agent_backend = options.agent_backend
    browser_backend = options.browser_backend
    interaction_mode = options.interaction_mode
    continuous = options.continuous
    dry_run = options.dry_run
    manual_captcha_relay = options.manual_captcha_relay
    headless = options.headless
    url = options.url
    authorization_file = options.authorization_file
    final_authorization_file = options.final_authorization_file
    gen = options.gen
    mark_applied = options.mark_applied
    mark_failed = options.mark_failed
    fail_reason = options.fail_reason
    reset_failed = options.reset_failed
    reset_failed_url = options.reset_failed_url
    console = runtime.console
    environment = runtime.environment

    runtime.bootstrap()

    from applypilot.apply.submission_admission import summarize_worker_allocation
    from applypilot.config import PROFILE_PATH as _profile_path
    from applypilot.database import get_connection
    from applypilot.services.application import (
        count_submission_ready_jobs,
        load_profile,
        resolve_apply_backend,
        resolve_apply_model,
    )

    # --- Utility modes (no Chrome/Claude needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        try:
            updated_url = mark_job(mark_applied, "applied")
        except (LookupError, ValueError) as exc:
            console.print(f"[red]Could not mark job as applied:[/red] {exc}")
            raise runtime.exit_exception(1) from None
        console.print(f"[green]Marked as applied:[/green] {updated_url}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        try:
            updated_url = mark_job(mark_failed, "failed", reason=fail_reason)
        except (LookupError, ValueError) as exc:
            console.print(f"[red]Could not mark job as failed:[/red] {exc}")
            raise runtime.exit_exception(1) from None
        console.print(
            f"[yellow]Marked as failed:[/yellow] {updated_url} "
            f"({fail_reason or 'manual'})"
        )
        return

    if reset_failed or reset_failed_url:
        from applypilot.apply.launcher import reset_failed as do_reset
        try:
            count = do_reset(reset_failed_url)
        except (LookupError, ValueError) as exc:
            console.print(f"[red]Could not reset failed job:[/red] {exc}")
            raise runtime.exit_exception(1) from None
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    if dry_run and continuous:
        console.print("[red]Continuous dry-run is not supported; use a finite --limit.[/red]")
        raise runtime.exit_exception(1)
    profile = load_profile(_profile_path)
    standing_auto_authorize = runtime.standing_auto_authorization_enabled(profile)
    submission_policy = profile.get("submission_policy", {})
    manual_captcha_relay = _manual_captcha_relay_enabled(
        manual_captcha_relay,
        profile,
        dry_run=dry_run,
        continuous=continuous,
        headless=headless,
        workers=workers,
        limit=limit,
    )
    if manual_captcha_relay and (
        dry_run
        or continuous
        or headless
        or workers != 1
        or limit not in (None, 1)
    ):
        console.print(
            "[red]Manual CAPTCHA relay requires bounded visible submission mode.[/red]"
        )
        raise runtime.exit_exception(1)
    final_batch_authorization_required = bool(
        isinstance(submission_policy, dict)
        and submission_policy.get("batch_final_authorization_required", False)
    )

    if final_authorization_file is not None and authorization_file is None:
        console.print(
            "[red]--final-authorization-file requires its bound --authorization-file.[/red]"
        )
        raise runtime.exit_exception(2)
    if (
        not dry_run
        and final_batch_authorization_required
        and authorization_file is not None
        and final_authorization_file is None
    ):
        console.print(
            "[red]This workspace requires one final batch authorization before any browser submission. "
            "Run finalize-batch after the user approves the complete prepared batch, then pass both files.[/red]"
        )
        raise runtime.exit_exception(2)

    if not dry_run and authorization_file is None and not url and not standing_auto_authorize:
        console.print(
            "[red]Submission without a manifest requires one exact --url.[/red]"
        )
        raise runtime.exit_exception(2)
    if not dry_run and authorization_file is None:
        submission_policy = profile.get("submission_policy", {})
        manifest_required = bool(
            profile.get("batch_authorization_required", False)
            or (
                isinstance(submission_policy, dict)
                and submission_policy.get("batch_authorization_required", False)
            )
        )
        if manifest_required and not standing_auto_authorize:
            console.print(
                "[red]Profile policy requires --authorization-file for every submission.[/red]"
            )
            raise runtime.exit_exception(2)
    requested_limit = limit if limit is not None else (0 if continuous else 1)
    if not dry_run and continuous and authorization_file is None:
        console.print(
            "[red]Continuous submission requires --authorization-file.[/red]"
        )
        raise runtime.exit_exception(2)
    if (
        not dry_run
        and requested_limit > 1
        and authorization_file is None
        and not standing_auto_authorize
    ):
        console.print(
            "[red]Batch submission requires --authorization-file or standing auto-authorization.[/red]"
        )
        raise runtime.exit_exception(2)
    if (
        not dry_run
        and url
        and authorization_file is None
        and environment.get("APPLYPILOT_AUTO_SUBMIT") != "1"
        and not standing_auto_authorize
    ):
        console.print(
            "[red]Single-URL submission requires APPLYPILOT_AUTO_SUBMIT=1 or "
            "--authorization-file.[/red]"
        )
        raise runtime.exit_exception(2)
    # --- Full apply mode ---

    backend = resolve_apply_backend(agent_backend, environment)
    if backend not in {"codex", "claude"}:
        console.print("[red]--agent-backend must be codex or claude.[/red]")
        raise runtime.exit_exception(1)
    from applypilot.apply.chrome import get_browser_executable, resolve_browser_backend
    from applypilot.apply.router import resolve_interaction_mode

    try:
        effective_browser_backend = resolve_browser_backend(browser_backend)
        effective_interaction_mode = resolve_interaction_mode(interaction_mode)
    except ValueError as exc:
        console.print(f"[red]{exc}.[/red]")
        raise runtime.exit_exception(1) from None
    effective_model = resolve_apply_model(backend, model, environment)

    # Check 1: the selected browser-agent CLI and a visible browser are required.
    import shutil

    backend_binary = shutil.which("codex.exe") or shutil.which("codex") if backend == "codex" else shutil.which("claude")
    try:
        effective_browser_backend, unavailable_browsers = (
            _select_runnable_browser_backend(
                effective_browser_backend,
                get_browser_executable,
            )
        )
        has_browser = True
        browser_error = ""
    except RuntimeError as exc:
        unavailable_browsers = {}
        has_browser = False
        browser_error = str(exc)
    if not backend_binary or not has_browser:
        missing = []
        if not backend_binary:
            missing.append(f"{backend} CLI")
        if not has_browser:
            missing.append(browser_error or effective_browser_backend)
        console.print(f"[red]Browser apply is missing: {', '.join(missing)}.[/red]")
        raise runtime.exit_exception(1)
    if unavailable_browsers:
        unavailable = "; ".join(
            f"{backend}: {reason}"
            for backend, reason in unavailable_browsers.items()
        )
        console.print(
            "[yellow]Automatic browser fallback is unavailable for "
            f"{unavailable}; continuing with {effective_browser_backend}.[/yellow]"
        )

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise runtime.exit_exception(1)

    # Check 3: Submission needs an approved cover letter. A fill-only preview
    # can proceed with a validated resume and leave an optional letter blank.
    ready = 0
    if not (gen and url):
        ready = count_submission_ready_jobs(
            get_connection(),
            dry_run=dry_run,
            profile=profile,
            minimum_fit_score=min_score,
        )
        if ready == 0:
            if dry_run:
                console.print("[red]No machine-validated tailored resume is ready for preview.[/red]")
            else:
                console.print(
                    "[red]No submission-ready materials.[/red]\n"
                    "Prepare a validated resume and resolve any material hard requirements first."
                )
            raise runtime.exit_exception(1)

    if gen:
        from applypilot.apply.launcher import gen_prompt
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise runtime.exit_exception(1)
        prompt_file = gen_prompt(target, min_score=min_score, model=effective_model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise runtime.exit_exception(1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        if backend == "claude":
            console.print("\n[bold]Run manually:[/bold]")
            console.print(
                f"  claude --model {effective_model} "
                f"--mcp-config {mcp_path} "
                f"--permission-mode bypassPermissions < {prompt_file}"
            )
        else:
            console.print(
                "\n[dim]Codex MCP isolation is assembled by CapyPilot at runtime; "
                "use apply --dry-run --url URL instead of copying a partial command.[/dim]"
            )
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)
    authorization_manifest = None
    if authorization_file is not None:
        from applypilot.apply.authorization import (
            load_final_authorization,
            load_manifest,
        )

        try:
            if final_authorization_file is not None:
                authorization_manifest = load_final_authorization(
                    final_authorization_file,
                    authorization_file,
                )
            else:
                authorization_manifest = load_manifest(authorization_file)
        except (OSError, TypeError, ValueError) as exc:
            console.print(f"[red]Invalid authorization manifest:[/red] {exc}")
            raise runtime.exit_exception(2) from None
    elif not dry_run and standing_auto_authorize:
        try:
            authorization_manifest = runtime.build_standing_authorization_manifest(
                get_connection(),
                profile=profile,
                target_url=url,
                requested_limit=requested_limit,
                min_score=min_score,
            )
        except (TypeError, ValueError) as exc:
            console.print(f"[red]Standing auto-authorization could not bind a ready job:[/red] {exc}")
            raise runtime.exit_exception(2) from None
        console.print(
            "[green]Standing authorization bound "
            f"{len(authorization_manifest['jobs'])} exact ready candidate(s) for up to "
            f"{authorization_manifest['max_submissions']} submissions.[/green]"
        )

    worker_allocation = {
        "requested_workers": workers,
        "bound_candidates": 0,
        "executable_candidates": 0,
        "blocked_candidates": 0,
        "effective_workers": workers,
    }
    preview_candidates = 0
    preview_selection = None
    if dry_run:
        preview_selection = "exact URL" if url else "queue"
        preview_candidates = (
            min(1, ready)
            if url
            else min(ready, max(0, effective_limit))
        )
        try:
            preview_worker_cap = int(
                submission_policy.get("maximum_workers", workers)
                if isinstance(submission_policy, dict)
                else workers
            )
        except (TypeError, ValueError):
            preview_worker_cap = workers
        worker_allocation["effective_workers"] = min(
            max(0, workers),
            max(0, preview_worker_cap),
            preview_candidates,
        )
        if worker_allocation["effective_workers"] < 1:
            console.print(
                "[red]No preview-admitted candidates remain; refusing to start browser workers.[/red]"
            )
            raise runtime.exit_exception(1)
    else:
        worker_allocation = summarize_worker_allocation(
            get_connection(),
            profile,
            authorization_manifest,
            requested_workers=workers,
            minimum_fit_score=min_score,
        )
        if worker_allocation["effective_workers"] < 1:
            console.print(
                "[red]No executable authorized candidates remain; refusing to start browser workers.[/red]"
            )
            raise runtime.exit_exception(2)

    worker_summary_lines, workers = _worker_summary_lines(
        worker_allocation,
        dry_run=dry_run,
        preview_selection=preview_selection,
        preview_candidates=preview_candidates,
    )

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Success target: {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers requested:  {worker_allocation['requested_workers']}")
    for summary_line in worker_summary_lines:
        console.print(summary_line)
    console.print(f"  Backend:  {backend}")
    console.print(f"  Control:  {effective_interaction_mode}")
    console.print(f"  Model:    {effective_model}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if dry_run:
        console.print(
            "  Preview boundary: form filling/uploads may occur; final submission is disabled"
        )
    console.print(f"  CAPTCHA:  {'manual relay' if manual_captcha_relay else 'stop on blocker'}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=effective_model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        agent_backend=backend,
        manual_captcha_relay=manual_captcha_relay,
        browser_backend=effective_browser_backend,
        interaction_mode=effective_interaction_mode,
        authorization_manifest=authorization_manifest,
    )
