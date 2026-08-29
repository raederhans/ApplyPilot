"""Application worker orchestration over an injected runtime port.

The compatibility launcher supplies browser, storage, policy, and dashboard
operations at call time.  This keeps current monkeypatch and shutdown semantics
while removing the orchestration state machine from the launcher facade.
"""

from __future__ import annotations

import threading
from types import ModuleType

from applypilot.apply.email_routing import (
    normalize_prepared_email_application,
    normalize_sent_receipt,
    reserve_direct_email_send,
)
from applypilot.apply.failure_taxonomy import classify_failure


def _prepared_email_application(job: dict) -> dict | None:
    """Return a bounded, verified prepare plan reported by the application agent."""
    observations = job.get("_agent_observations")
    if not isinstance(observations, dict):
        return None
    return normalize_prepared_email_application(
        observations.get("email_application"),
        job,
    )

WORKER_RUNTIME_PORTS = (
    "POLL_INTERVAL", "_acquire_cloak_lane", "_archive_worker_evidence",
    "_snapshot_worker_evidence",
    "_attach_control_contract", "_audit_live_pre_submit_page",
    "_classify_post_submit_observation", "_cloak_lane", "_format_failure_error",
    "_is_permanent_failure", "_mark_runtime_cover_not_required",
    "_observe_post_submit_page", "_open_bound_application_target",
    "_resolve_ats_application_binding", "_run_read_only_preflight",
    "_prepare_runtime_cover_letter", "_reserve_manifest_submission",
    "_admit_direct_email_receipt",
    "_route_for_phase", "_stop_event", "_submission_evidence_consistent",
    "_submission_rate_status", "_update_submission_ledger",
    "_wait_for_manual_captcha", "acquire_job", "add_event", "allocate_cdp_port",
    "capture_browser_session", "cleanup_worker", "cloak_fallback_route",
    "computer_use_handoff_allowed", "config", "datetime", "get_connection",
    "initial_route", "launch_chrome", "load_runtime_settings", "logger",
    "mark_result", "release_cdp_port", "release_lock", "resolve_browser_backend",
    "resolve_interaction_mode", "restore_browser_session", "restore_preview_state",
    "run_job", "update_state",
)


def _validate_runtime_ports(runtime: ModuleType) -> None:
    missing = [name for name in WORKER_RUNTIME_PORTS if not hasattr(runtime, name)]
    if missing:
        raise TypeError(f"worker runtime is missing required ports: {', '.join(missing)}")


def _worker_loop_with_port(
    runtime: ModuleType,
    port: int,
    worker_id: int = 0,
    limit: int = 1,
    target_url: str | None = None,
    min_score: int = 6,
    headless: bool = False,
    model: str = "sonnet",
    dry_run: bool = False,
    agent_backend: str = "codex",
    manual_captcha_relay: bool = False,
    browser_backend: str = "edge",
    interaction_mode: str = "auto",
    authorization_manifest: dict | None = None,
    attempted_urls: set[str] | None = None,
    attempted_urls_lock: threading.Lock | None = None,
) -> tuple[int, int]:
    """Run jobs until the confirmed-success target is reached or the queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Confirmed submissions to achieve for a real run; preview jobs for dry-run (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    POLL_INTERVAL = runtime.POLL_INTERVAL
    _acquire_cloak_lane = runtime._acquire_cloak_lane
    _archive_worker_evidence = runtime._archive_worker_evidence
    _snapshot_worker_evidence = runtime._snapshot_worker_evidence
    _attach_control_contract = runtime._attach_control_contract
    _audit_live_pre_submit_page = runtime._audit_live_pre_submit_page
    _classify_post_submit_observation = runtime._classify_post_submit_observation
    _cloak_lane = runtime._cloak_lane
    _format_failure_error = runtime._format_failure_error
    _is_permanent_failure = runtime._is_permanent_failure
    _mark_runtime_cover_not_required = runtime._mark_runtime_cover_not_required
    _observe_post_submit_page = runtime._observe_post_submit_page
    _open_bound_application_target = runtime._open_bound_application_target
    _resolve_ats_application_binding = runtime._resolve_ats_application_binding
    _run_read_only_preflight = runtime._run_read_only_preflight
    _prepare_runtime_cover_letter = runtime._prepare_runtime_cover_letter
    _admit_direct_email_receipt = runtime._admit_direct_email_receipt
    _reserve_manifest_submission = runtime._reserve_manifest_submission
    _route_for_phase = runtime._route_for_phase
    _stop_event = runtime._stop_event
    _submission_evidence_consistent = runtime._submission_evidence_consistent
    _submission_rate_status = runtime._submission_rate_status
    _update_submission_ledger = runtime._update_submission_ledger
    _wait_for_manual_captcha = runtime._wait_for_manual_captcha
    acquire_job = runtime.acquire_job
    add_event = runtime.add_event
    capture_browser_session = runtime.capture_browser_session
    cleanup_worker = runtime.cleanup_worker
    cloak_fallback_route = runtime.cloak_fallback_route
    computer_use_handoff_allowed = runtime.computer_use_handoff_allowed
    config = runtime.config
    datetime = runtime.datetime
    get_connection = runtime.get_connection
    initial_route = runtime.initial_route
    launch_chrome = runtime.launch_chrome
    logger = runtime.logger
    mark_result = runtime.mark_result
    release_lock = runtime.release_lock
    resolve_browser_backend = runtime.resolve_browser_backend
    resolve_interaction_mode = runtime.resolve_interaction_mode
    restore_browser_session = runtime.restore_browser_session
    restore_preview_state = runtime.restore_preview_state
    run_job = runtime.run_job
    update_state = runtime.update_state
    application_lease_minutes = runtime.load_runtime_settings().application_lease_minutes

    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    profile = config.load_profile()
    requested_browser_backend = resolve_browser_backend(browser_backend)
    requested_interaction_mode = resolve_interaction_mode(interaction_mode)
    run_attempted_urls = attempted_urls if attempted_urls is not None else set()

    while not _stop_event.is_set():
        target_progress = jobs_done if dry_run else applied
        if not continuous and target_progress >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        if not dry_run:
            allowed, cooldown, rate_reason = _submission_rate_status(
                get_connection(), profile
            )
            if not allowed:
                add_event(f"[W{worker_id}] Rate limit reached: {rate_reason}")
                update_state(worker_id, status="done", last_action=rate_reason)
                break
            if cooldown > 0:
                add_event(f"[W{worker_id}] Submission cooldown: {cooldown:.0f}s")
                update_state(worker_id, status="idle", last_action="submission cooldown")
                if _stop_event.wait(timeout=cooldown):
                    break

        if attempted_urls_lock is None:
            excluded_urls = set(run_attempted_urls)
        else:
            with attempted_urls_lock:
                excluded_urls = set(run_attempted_urls)
        job = acquire_job(
            target_url=target_url,
            min_score=min_score,
            worker_id=worker_id,
            preview_only=dry_run,
            authorization_manifest=authorization_manifest,
            exclude_urls=excluded_urls,
            application_lease_minutes=application_lease_minutes,
        )
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0
        job["_evidence_baseline"] = _snapshot_worker_evidence(worker_id)
        if attempted_urls_lock is None:
            run_attempted_urls.add(str(job["url"]))
        else:
            with attempted_urls_lock:
                run_attempted_urls.add(str(job["url"]))

        chrome_proc = None
        submission_started = False
        verification_relay_used = False
        cover_material_resolved = False
        pre_submit_repair_used = False
        ledger_reserved = False
        submission_evidence: dict | None = None
        cloak_lane_held = False
        cloak_fallback_used = False
        route_history: list[dict[str, object]] = []
        try:
            read_only_preflight = _run_read_only_preflight(job)
        except Exception as exc:  # noqa: BLE001 - final atomic gate remains authoritative
            logger.warning(
                "Read-only preflight failed for %s: %s",
                job.get("url"),
                exc,
            )
            read_only_preflight = {
                "task_statuses": {},
                "error_type": type(exc).__name__,
            }
        job["_read_only_preflight"] = read_only_preflight
        duplicate_snapshot = read_only_preflight.get("duplicate")
        if (
            isinstance(duplicate_snapshot, dict)
            and duplicate_snapshot.get("clear") is False
        ):
            reason = str(
                duplicate_snapshot.get("reason") or "duplicate_submission_identity"
            )
            mark_result(
                job["url"],
                "skipped",
                reason,
                permanent=True,
                task_id=job.get("_attempt_id"),
                evidence={"duplicate_snapshot": duplicate_snapshot},
            )
            jobs_done += 1
            add_event(f"[W{worker_id}] Exact duplicate skipped before browser launch")
            update_state(
                worker_id,
                status="skipped",
                last_action="exact duplicate receipt/status",
                jobs_done=jobs_done,
            )
            continue
        try:
            active_route = initial_route(requested_browser_backend, phase="prepare")
            active_browser_backend = active_route.browser_runtime
            if active_browser_backend == "cloak":
                cloak_lane_held = _acquire_cloak_lane(worker_id)
            _attach_control_contract(
                job,
                active_route,
                interaction_mode=requested_interaction_mode,
                resume_existing_page=False,
            )
            route_history.append({
                **active_route.as_dict(),
                "event": "route_selected",
                "submit_started": False,
            })
            add_event(f"[W{worker_id}] Launching {active_browser_backend} browser...")
            start_url = str(job.get("application_url") or job["url"])
            ats_binding = read_only_preflight.get("ats_binding")
            if (
                read_only_preflight.get("provider") == "smartrecruiters"
                and "ats_binding" not in read_only_preflight
            ):
                ats_binding = _resolve_ats_application_binding(job)
            if ats_binding is not None:
                job["_ats_application_binding"] = ats_binding
            chrome_proc = launch_chrome(
                worker_id,
                port=port,
                headless=headless,
                start_url=None,
                browser_backend=active_browser_backend,
            )
            root_ids = _open_bound_application_target(port, start_url)
            job["_browser_root_target_ids"] = sorted(root_ids)
            job["_browser_root_runtime"] = active_browser_backend

            submission_phase = "submit" if dry_run else "prepare"
            result, duration_ms = run_job(
                job,
                port=port,
                worker_id=worker_id,
                model=model,
                dry_run=dry_run,
                agent_backend=agent_backend,
                manual_captcha_relay=manual_captcha_relay,
                submission_phase=submission_phase,
            )

            fallback_route = cloak_fallback_route(
                result,
                requested_browser_backend=requested_browser_backend,
                phase="prepare",
                current_runtime=active_browser_backend,
                fallback_already_used=cloak_fallback_used,
            )
            if fallback_route is not None:
                edge_block_result = result
                cloak_fallback_used = True
                cloak_lane_held = _acquire_cloak_lane(worker_id)
                active_route = fallback_route
                active_browser_backend = active_route.browser_runtime
                _attach_control_contract(
                    job,
                    active_route,
                    interaction_mode=requested_interaction_mode,
                    resume_existing_page=False,
                )
                add_event(
                    f"[W{worker_id}] Explicit browser block detected; "
                    "retrying once with CloakBrowser"
                )
                update_state(
                    worker_id,
                    status="retrying",
                    last_action="switching to CloakBrowser",
                )
                try:
                    edge_session = capture_browser_session(
                        port,
                        [
                            str(job.get("url") or ""),
                            str(job.get("application_url") or ""),
                        ],
                    )
                    cleanup_worker(worker_id, chrome_proc)
                    chrome_proc = None
                    chrome_proc = launch_chrome(
                        worker_id,
                        port=port,
                        headless=headless,
                        start_url=None,
                        browser_backend=active_browser_backend,
                    )
                    restored_cookies = restore_browser_session(
                        port,
                        edge_session,
                        start_url,
                    )
                    root_ids = _open_bound_application_target(port, start_url)
                    job["_browser_root_target_ids"] = sorted(root_ids)
                    job["_browser_root_runtime"] = active_browser_backend
                    logger.info(
                        "[worker-%d] Bridged %d browser cookies into CloakBrowser",
                        worker_id,
                        restored_cookies,
                    )
                    route_history.append({
                        **active_route.as_dict(),
                        "event": "runtime_transition",
                        "from": "playwright/edge",
                        "to": "playwright/cloak",
                        "trigger_result": edge_block_result,
                        "session_cookies_restored": restored_cookies,
                        "submit_started": False,
                    })
                    result, stealth_duration = run_job(
                        job,
                        port=port,
                        worker_id=worker_id,
                        model=model,
                        dry_run=dry_run,
                        agent_backend=agent_backend,
                        manual_captcha_relay=manual_captcha_relay,
                        submission_phase=submission_phase,
                    )
                    duration_ms += stealth_duration
                except Exception as exc:
                    logger.exception("CloakBrowser fallback failed")
                    route_history.append({
                        **active_route.as_dict(),
                        "event": "runtime_transition_failed",
                        "from": "playwright/edge",
                        "to": "playwright/cloak",
                        "trigger_result": edge_block_result,
                        "error_type": type(exc).__name__,
                        "submit_started": False,
                    })
                    result = f"failed:cloak_backend_unavailable:{type(exc).__name__}"
                logger.info(
                    "[worker-%d] Browser fallback edge_result=%s cloak_result=%s",
                    worker_id,
                    edge_block_result,
                    result,
                )

            if computer_use_handoff_allowed(
                result,
                interaction_mode=requested_interaction_mode,
                phase="prepare",
                submit_started=submission_started,
            ):
                route_history.append({
                    "contract_version": 1,
                    "interaction_driver": "computer_use",
                    "browser_runtime": active_browser_backend,
                    "phase": "prepare",
                    "reason_code": "external_visual_handoff_requested",
                    "event": "handoff_requested",
                    "submit_started": False,
                    "requires_fresh_observation": True,
                })
                add_event(
                    f"[W{worker_id}] Computer Use handoff requested; "
                    "preserving fail-closed result"
                )

            while True:
                if result in {"cover_not_required", "cover_letter_required"}:
                    if cover_material_resolved:
                        result = "failed:cover_material_discovery_loop"
                        break
                    cover_material_resolved = True
                    try:
                        if result == "cover_not_required":
                            job = _mark_runtime_cover_not_required(job)
                            add_event(f"[W{worker_id}] ATS confirmed no cover letter is required")
                        else:
                            add_event(f"[W{worker_id}] ATS requires a cover letter; generating it")
                            update_state(
                                worker_id,
                                status="preparing_material",
                                last_action="generating validated cover letter",
                            )
                            job = _prepare_runtime_cover_letter(job)
                        # Runtime cover resolution reloads the database row.
                        # Re-attach the non-persisted browser policy before the
                        # agent resumes on the existing Cloak/Edge page.
                        _attach_control_contract(
                            job,
                            active_route,
                            interaction_mode=requested_interaction_mode,
                            resume_existing_page=True,
                        )
                        result, resumed_duration = run_job(
                            job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=False,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase="prepare",
                        )
                        duration_ms += resumed_duration
                        continue
                    except Exception as exc:
                        logger.exception("Runtime cover-letter resolution failed")
                        result = f"failed:manual_review_required:cover_letter_generation:{type(exc).__name__}"
                        break

                if result == "captcha":
                    if (
                        manual_captcha_relay
                        and not verification_relay_used
                        and _wait_for_manual_captcha(
                            port,
                            worker_id,
                            attempt_id=job.get("_attempt_id"),
                            submit_started=False,
                            root_target_ids=set(job.get("_browser_root_target_ids") or []),
                            application_lease_minutes=application_lease_minutes,
                        )
                    ):
                        verification_relay_used = True
                        resumed_job = dict(job)
                        resumed_job["_browser_observation"] = {
                            "verification_resume": True,
                            "signal": "manual_verification_cleared",
                            "submission_gate": True,
                        }
                        _attach_control_contract(
                            resumed_job,
                            active_route,
                            interaction_mode=requested_interaction_mode,
                            resume_existing_page=True,
                        )
                        result, resumed_duration = run_job(
                            resumed_job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=dry_run,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase=submission_phase,
                        )
                        duration_ms += resumed_duration
                        continue
                    break

                if result == "ready_to_submit" and not dry_run:
                    email_application = _prepared_email_application(job)
                    if email_application is not None:
                        audit_signal = None
                        audit_report = {
                            "status": "clear",
                            "disposition": "clear",
                            "page_url": str(job.get("application_url") or job.get("url") or ""),
                            "issues": [],
                            "blocking_issues": [],
                            "repairable_issues": [],
                            "advisory_issues": [],
                            "lossy_answer_mappings": [],
                            "advisory_only": False,
                            "submission_gate": True,
                            "email_application": email_application,
                        }
                    else:
                        audit_signal, audit_report = _audit_live_pre_submit_page(
                            port, worker_id, job
                        )
                    observation_label = audit_signal or "clear"
                    add_event(
                        f"[W{worker_id}] Browser observation: {observation_label[:45]}"
                    )
                    update_state(
                        worker_id,
                        status="observed",
                        last_action=f"browser signal: {observation_label[:25]}",
                    )
                    if audit_report.get("disposition") == "retry_prepare":
                        repairable = [
                            str(issue)
                            for issue in audit_report.get("repairable_issues", [])
                        ]
                        if pre_submit_repair_used:
                            reason = repairable[0] if repairable else "pre_submit_state"
                            result = f"failed:pre_submit_not_ready:{reason}"
                            break
                        pre_submit_repair_used = True
                        repair_job = dict(job)
                        repair_job["_browser_observation"] = {
                            **audit_report,
                            "signal": audit_signal,
                            "repair_prepare": True,
                            "advisory_only": False,
                            "submission_gate": False,
                        }
                        _attach_control_contract(
                            repair_job,
                            active_route,
                            interaction_mode=requested_interaction_mode,
                            resume_existing_page=True,
                        )
                        add_event(
                            f"[W{worker_id}] One prepare repair: "
                            f"{','.join(repairable[:2])[:45]}"
                        )
                        result, repair_duration = run_job(
                            repair_job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=False,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase="prepare",
                        )
                        duration_ms += repair_duration
                        continue
                    if audit_signal:
                        result = f"failed:manual_review_required:{audit_signal}"
                        break
                    from applypilot.database import update_application_attempt

                    attempt_id = job.get("_attempt_id")
                    lease_held = not attempt_id or update_application_attempt(
                        attempt_id,
                        phase="reservation",
                        submit_started=False,
                        evidence={
                            "pre_submit_audit": audit_report.get("disposition", "clear"),
                            "lossy_answer_mapping_count": len(
                                audit_report.get("lossy_answer_mappings", [])
                            ),
                        },
                        lease_minutes=application_lease_minutes,
                    )
                    if not lease_held:
                        result = "failed:stale_application_attempt"
                        break
                    reserved, reservation_reason = _reserve_manifest_submission(
                        authorization_manifest,
                        job,
                        audit_report,
                    )
                    if (
                        not reserved
                        and reservation_reason == "minimum_submission_gap"
                    ):
                        gate_state = job.get("_submission_gate", {})
                        retry_after = (
                            float(gate_state.get("retry_after_seconds") or 0)
                            if isinstance(gate_state, dict)
                            else 0.0
                        )
                        retry_after = max(0.0, min(retry_after, 120.0))
                        add_event(
                            f"[W{worker_id}] Atomic submit gate wait: "
                            f"{retry_after:.1f}s"
                        )
                        update_state(
                            worker_id,
                            status="idle",
                            last_action="atomic submission gap",
                        )
                        if _stop_event.wait(timeout=retry_after):
                            result = "deferred:operator_stop_before_submission_gate"
                            break
                        if email_application is None:
                            audit_signal, audit_report = _audit_live_pre_submit_page(
                                port,
                                worker_id,
                                job,
                            )
                            if audit_signal or audit_report.get("disposition") != "clear":
                                result = (
                                    "failed:manual_review_required:"
                                    f"{audit_signal or 'page_changed_during_submission_gap'}"
                                )
                                break
                        lease_held = not attempt_id or update_application_attempt(
                            attempt_id,
                            phase="reservation",
                            submit_started=False,
                            evidence={"submission_gate_wait_seconds": retry_after},
                            lease_minutes=application_lease_minutes,
                        )
                        if not lease_held:
                            result = "failed:stale_application_attempt"
                            break
                        reserved, reservation_reason = _reserve_manifest_submission(
                            authorization_manifest,
                            job,
                            audit_report,
                        )
                    if not reserved:
                        if reservation_reason in {
                            "minimum_submission_gap",
                            "rolling_hour_submission_cap",
                        }:
                            result = f"deferred:{reservation_reason}"
                        else:
                            result = f"failed:manual_review_required:{reservation_reason}"
                        break
                    ledger_reserved = authorization_manifest is not None
                    observed_job = dict(job)
                    observed_job["_browser_observation"] = {
                        **audit_report,
                        "signal": audit_signal,
                        "advisory_only": bool(audit_report.get("advisory_only")),
                        "submission_gate": True,
                    }
                    if email_application is not None:
                        observed_job["_direct_email_send_reservation"] = (
                            reserve_direct_email_send(job, email_application)
                        )
                    submission_phase = "submit"
                    lease_held = not attempt_id or update_application_attempt(
                        attempt_id,
                        phase="submit",
                        submit_started=True,
                        evidence={"pre_submit_audit": "clear"},
                        lease_minutes=application_lease_minutes,
                    )
                    if not lease_held:
                        result = "submission_uncertain"
                        submission_evidence = {
                            "submit_started": False,
                            "reason": "attempt_lease_lost_after_reservation",
                        }
                        break
                    submission_started = True
                    active_route = _route_for_phase(
                        active_route,
                        "submit",
                        (
                            "email_plan_verified"
                            if email_application is not None
                            else "pre_submit_audit_clear"
                        ),
                        interaction_driver=(
                            "mailbox" if email_application is not None else None
                        ),
                        submit_owner=(
                            "mailbox" if email_application is not None else None
                        ),
                    )
                    _attach_control_contract(
                        observed_job,
                        active_route,
                        interaction_mode=requested_interaction_mode,
                        resume_existing_page=True,
                    )
                    route_history.append({
                        **active_route.as_dict(),
                        "event": "phase_transition",
                        "submit_started": True,
                        "pre_submit_page_url": audit_report.get("page_url"),
                    })
                    result, submit_duration = run_job(
                        observed_job,
                        port=port,
                        worker_id=worker_id,
                        model=model,
                        dry_run=False,
                        agent_backend=agent_backend,
                        manual_captcha_relay=manual_captcha_relay,
                        resume_existing_page=True,
                        submission_phase="submit",
                    )
                    duration_ms += submit_duration
                    agent_evidence = observed_job.get("_agent_submission_evidence")
                    if email_application is not None:
                        runtime_mailbox = observed_job.get(
                            "_mailbox_runtime_evidence", {}
                        )
                        if not isinstance(runtime_mailbox, dict):
                            runtime_mailbox = {}
                        sent_receipt = normalize_sent_receipt(
                            runtime_mailbox.get("sent_receipt"),
                            email_application,
                        )
                        observer_evidence = {
                            "channel": "direct_email",
                            "confirmed": bool(
                                sent_receipt is not None
                                and runtime_mailbox.get("send_request_bound") is True
                                and runtime_mailbox.get("send_call_completed") is True
                                and runtime_mailbox.get("post_send_search_completed") is True
                                and runtime_mailbox.get("post_send_read_completed") is True
                            ),
                            "send_call_completed": bool(
                                runtime_mailbox.get("send_call_completed")
                            ),
                            "send_request_bound": bool(
                                runtime_mailbox.get("send_request_bound")
                            ),
                            "post_send_search_completed": bool(
                                runtime_mailbox.get("post_send_search_completed")
                            ),
                            "post_send_read_completed": bool(
                                runtime_mailbox.get("post_send_read_completed")
                            ),
                            "recipient": email_application.get("recipient"),
                            "subject": email_application.get("subject"),
                            "attachment_names": email_application.get(
                                "attachment_names", []
                            ),
                            "confirmation_text": "mailbox send and Sent-copy verification tools completed",
                            "sent_receipt": sent_receipt,
                        }
                        disposition = (
                            "confirmed"
                            if observer_evidence["confirmed"]
                            else "inconclusive"
                        )
                    else:
                        observer_evidence = _observe_post_submit_page(
                            port, worker_id, job, attempt=1
                        )
                        disposition = _classify_post_submit_observation(
                            observer_evidence
                        )
                        observer_reason = str(observer_evidence.get("reason") or "")
                        if (
                            disposition == "uncertain"
                            and observer_reason.startswith(
                                (
                                    "post_submit_observer_error:",
                                    "post_submit_no_bound_application_page",
                                )
                            )
                            and not _stop_event.wait(timeout=0.75)
                        ):
                            add_event(
                                f"[W{worker_id}] Reconnecting observer once; "
                                "never repeating Submit"
                            )
                            retry_observation = _observe_post_submit_page(
                                port,
                                worker_id,
                                job,
                                attempt=1,
                            )
                            retry_observation["observer_reconnect_attempts"] = 1
                            retry_observation["initial_observer_reason"] = observer_reason
                            observer_evidence = retry_observation
                            disposition = _classify_post_submit_observation(
                                observer_evidence
                            )
                    attempts = [{
                        "agent": agent_evidence,
                        "observer": observer_evidence,
                        "disposition": disposition,
                    }]

                    # One repair turn is allowed only when visible validation
                    # errors prove the first click was rejected. An absent
                    # receipt alone can never authorize another click.
                    if (
                        email_application is None
                        and disposition == "validation_blocked_repairable"
                    ):
                        repair_job = dict(observed_job)
                        repair_job.pop("_agent_submission_evidence", None)
                        repair_job["_browser_observation"] = {
                            "repair_mode": True,
                            "signal": disposition,
                            "validation_errors": observer_evidence.get(
                                "validation_errors", []
                            ),
                            "submission_gate": True,
                        }
                        add_event(f"[W{worker_id}] Repairing supported validation errors once")
                        update_state(
                            worker_id,
                            status="repairing",
                            last_action="one-time validation repair",
                        )
                        result, repair_duration = run_job(
                            repair_job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=False,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase="submit",
                        )
                        duration_ms += repair_duration
                        agent_evidence = repair_job.get("_agent_submission_evidence")
                        observer_evidence = _observe_post_submit_page(
                            port, worker_id, job, attempt=2
                        )
                        disposition = _classify_post_submit_observation(
                            observer_evidence
                        )
                        attempts.append({
                            "agent": agent_evidence,
                            "observer": observer_evidence,
                            "disposition": disposition,
                        })
                    elif (
                        email_application is None
                        and
                        disposition == "verification_required"
                        and manual_captcha_relay
                        and not verification_relay_used
                        and _wait_for_manual_captcha(
                            port,
                            worker_id,
                            attempt_id=job.get("_attempt_id"),
                            submit_started=True,
                            root_target_ids=set(job.get("_browser_root_target_ids") or []),
                            application_lease_minutes=application_lease_minutes,
                        )
                    ):
                        verification_relay_used = True
                        verification_job = dict(observed_job)
                        verification_job.pop("_agent_submission_evidence", None)
                        verification_job["_browser_observation"] = {
                            "verification_resume": True,
                            "signal": "manual_verification_cleared",
                            "submission_gate": True,
                        }
                        result, resumed_duration = run_job(
                            verification_job,
                            port=port,
                            worker_id=worker_id,
                            model=model,
                            dry_run=False,
                            agent_backend=agent_backend,
                            manual_captcha_relay=manual_captcha_relay,
                            resume_existing_page=True,
                            submission_phase="submit",
                        )
                        duration_ms += resumed_duration
                        agent_evidence = verification_job.get(
                            "_agent_submission_evidence"
                        )
                        observer_evidence = _observe_post_submit_page(
                            port, worker_id, job, attempt=2
                        )
                        disposition = _classify_post_submit_observation(
                            observer_evidence
                        )
                        attempts.append({
                            "agent": agent_evidence,
                            "observer": observer_evidence,
                            "disposition": disposition,
                        })

                    submission_evidence = {
                        "browser_backend": active_browser_backend,
                        "interaction_driver": active_route.interaction_driver,
                        "browser_runtime": active_route.browser_runtime,
                        "submit_owner": active_route.submit_owner,
                        "route_contract_version": active_route.contract_version,
                        "route_history": route_history,
                        "fallback_from_edge": (
                            requested_browser_backend == "auto"
                            and active_browser_backend == "cloak"
                        ),
                        "agent": agent_evidence,
                        "observer": observer_evidence,
                        "attempts": attempts,
                    }
                    if disposition == "provider_submission_error":
                        submission_evidence["technical_failure"] = classify_failure(
                            "failed:provider_submission_error"
                        ).as_dict()
                    elif (
                        disposition == "uncertain"
                        and str(observer_evidence.get("reason") or "").startswith(
                            (
                                "post_submit_observer_error:",
                                "post_submit_no_bound_application_page",
                            )
                        )
                    ):
                        submission_evidence["technical_failure"] = classify_failure(
                            "failed:post_submit_observer_unavailable"
                        ).as_dict()
                    archived = (
                        []
                        if email_application is not None
                        else _archive_worker_evidence(
                            config.APPLY_WORKER_DIR / f"worker-{worker_id}",
                            job,
                            worker_id,
                            datetime.now().astimezone().strftime("%Y%m%d_%H%M%S"),
                        )
                    )
                    archived_by_name = {path.name: path for path in archived}
                    for index, attempt_evidence in enumerate(attempts, start=1):
                        filename = (
                            "submission-confirmation-observer.png"
                            if index == 1
                            else f"submission-confirmation-observer-attempt-{index}.png"
                        )
                        archived_observer = archived_by_name.get(filename)
                        if archived_observer is not None:
                            attempt_evidence["observer"]["screenshot_path"] = str(
                                archived_observer
                            )
                    final_archive = archived_by_name.get(
                        "submission-confirmation-observer.png"
                        if len(attempts) == 1
                        else f"submission-confirmation-observer-attempt-{len(attempts)}.png"
                    )
                    if final_archive is not None:
                        observer_evidence["screenshot_path"] = str(final_archive)

                    if disposition == "confirmed":
                        sent_receipt = observer_evidence.get("sent_receipt")
                        direct_receipt_agrees = bool(
                            email_application is None
                            or (
                                isinstance(agent_evidence, dict)
                                and isinstance(sent_receipt, dict)
                                and str(agent_evidence.get("provider_message_id") or "")
                                == str(sent_receipt.get("provider_message_id") or "")
                            )
                        )
                        if (
                            result != "applied"
                            or not direct_receipt_agrees
                            or not _submission_evidence_consistent(
                                agent_evidence, observer_evidence
                            )
                        ):
                            result = "submission_uncertain"
                        elif email_application is not None:
                            admission = _admit_direct_email_receipt(
                                job,
                                observer_evidence.get("sent_receipt"),
                            )
                            if admission.get("status") not in {
                                "admitted",
                                "already_admitted",
                            }:
                                observer_evidence["receipt_admission"] = admission
                                result = "submission_uncertain"
                    elif disposition == "verification_required":
                        result = "captcha"
                    elif disposition == "validation_blocked_manual":
                        result = "failed:manual_review_required:submission_validation"
                    elif disposition == "validation_blocked_repairable":
                        result = "failed:submission_validation_blocked_after_repair"
                    elif disposition == "provider_submission_error":
                        result = "submission_uncertain"
                    else:
                        result = "submission_uncertain"
                    break
                break

            if (
                submission_started
                and result not in {"applied", "submission_uncertain"}
                and "submission_validation" not in result
            ):
                submission_evidence = submission_evidence or {
                    "submit_started": True,
                    "reason": f"post_submit_result_requires_review:{result}",
                }
                result = "submission_uncertain"
            if job.get("_bound_submission_materials"):
                submission_evidence = dict(submission_evidence or {})
                submission_evidence["material_binding"] = job[
                    "_bound_submission_materials"
                ]

            if dry_run and result != "previewed":
                restore_preview_state(job)
                if result == "skipped":
                    add_event(f"[W{worker_id}] Preview skipped: {job['title'][:30]}")
                    continue
                failed += 1
                update_state(worker_id, jobs_failed=failed, jobs_done=applied + failed)
            elif result == "skipped":
                release_lock(job["url"], job.get("_attempt_id"))
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif result == "applied":
                ledger_updated = _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "applied",
                    submission_evidence,
                )
                if not ledger_updated:
                    uncertainty_evidence = {
                        "submit_started": True,
                        "reason": "submission_ledger_update_failed",
                        "submission_evidence": submission_evidence,
                    }
                    mark_result(
                        job["url"],
                        "submission_uncertain",
                        "submission ledger could not record the confirmed browser outcome",
                        duration_ms=duration_ms,
                        task_id=job.get("_attempt_id"),
                        evidence=uncertainty_evidence,
                    )
                    add_event(
                        f"[W{worker_id}] Submission receipt found but ledger update failed"
                    )
                    update_state(
                        worker_id,
                        status="submission_uncertain",
                        last_action="ledger update failed",
                        jobs_done=applied + failed + 1,
                    )
                else:
                    mark_result(
                        job["url"],
                        "applied",
                        duration_ms=duration_ms,
                        task_id=job.get("_attempt_id"),
                        evidence=submission_evidence,
                    )
                    applied += 1
                    update_state(worker_id, jobs_applied=applied,
                                 jobs_done=applied + failed)
            elif result == "submission_uncertain":
                uncertainty_evidence = submission_evidence or {
                    "submit_started": submission_started,
                    "reason": "agent_or_observer_confirmation_inconclusive",
                }
                _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "submission_uncertain",
                    uncertainty_evidence,
                )
                mark_result(
                    job["url"],
                    "submission_uncertain",
                    "browser did not show a decisive receipt after the final action",
                    duration_ms=duration_ms,
                    task_id=job.get("_attempt_id"),
                    evidence=uncertainty_evidence,
                )
                add_event(f"[W{worker_id}] Submission state uncertain; status recorded")
                update_state(
                    worker_id,
                    status="submission_uncertain",
                    last_action="status recorded for agent review",
                    jobs_done=applied + failed + 1,
                )
            elif result == "previewed":
                mark_result(
                    job["url"],
                    "previewed",
                    duration_ms=duration_ms,
                    task_id=job.get("_attempt_id"),
                )
                update_state(worker_id, jobs_done=applied + failed + 1)
            elif result.startswith("deferred:"):
                release_lock(job["url"], job.get("_attempt_id"))
                reason = result.split(":", 1)[1]
                add_event(f"[W{worker_id}] Deferred without failure: {reason[:45]}")
                update_state(
                    worker_id,
                    status="idle",
                    last_action=f"deferred: {reason[:25]}",
                )
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                reason = _format_failure_error(reason, job.pop("_failure_context", None))
                if submission_started and ledger_reserved:
                    _update_submission_ledger(
                        authorization_manifest,
                        job,
                        "failed",
                        {
                            "reason": reason,
                            "submission_evidence": submission_evidence,
                        },
                    )
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms,
                            task_id=job.get("_attempt_id"))
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)

        except KeyboardInterrupt:
            if dry_run:
                restore_preview_state(job)
            elif submission_started:
                uncertainty_evidence = {
                    "submit_started": True,
                    "reason": "operator_interrupt_after_submit_phase_started",
                }
                _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "submission_uncertain",
                    uncertainty_evidence,
                )
                mark_result(
                    job["url"],
                    "submission_uncertain",
                    "operator interrupt after submit phase started",
                    task_id=job.get("_attempt_id"),
                    evidence=uncertainty_evidence,
                )
            else:
                release_lock(job["url"], job.get("_attempt_id"))
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            if dry_run:
                restore_preview_state(job)
                failed += 1
                update_state(worker_id, jobs_failed=failed)
            elif submission_started:
                uncertainty_evidence = {
                    "submit_started": True,
                    "reason": f"launcher_error:{type(e).__name__}",
                }
                _update_submission_ledger(
                    authorization_manifest if ledger_reserved else None,
                    job,
                    "submission_uncertain",
                    uncertainty_evidence,
                )
                mark_result(
                    job["url"],
                    "submission_uncertain",
                    "launcher error after submit phase started",
                    task_id=job.get("_attempt_id"),
                    evidence=uncertainty_evidence,
                )
                update_state(worker_id, status="submission_uncertain")
            else:
                release_lock(job["url"], job.get("_attempt_id"))
                failed += 1
                update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)
            if cloak_lane_held:
                _cloak_lane.release()

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


def worker_loop(
    runtime: ModuleType,
    worker_id: int = 0,
    limit: int = 1,
    target_url: str | None = None,
    min_score: int = 6,
    headless: bool = False,
    model: str = "sonnet",
    dry_run: bool = False,
    agent_backend: str = "codex",
    manual_captcha_relay: bool = False,
    browser_backend: str = "edge",
    interaction_mode: str = "auto",
    authorization_manifest: dict | None = None,
    attempted_urls: set[str] | None = None,
    attempted_urls_lock: threading.Lock | None = None,
) -> tuple[int, int]:
    """Validate injected ports and own the CDP claim for the full worker run."""
    _validate_runtime_ports(runtime)
    port = runtime.allocate_cdp_port(worker_id)
    try:
        return _worker_loop_with_port(
            runtime,
            port,
            worker_id=worker_id,
            limit=limit,
            target_url=target_url,
            min_score=min_score,
            headless=headless,
            model=model,
            dry_run=dry_run,
            agent_backend=agent_backend,
            manual_captcha_relay=manual_captcha_relay,
            browser_backend=browser_backend,
            interaction_mode=interaction_mode,
            authorization_manifest=authorization_manifest,
            attempted_urls=attempted_urls,
            attempted_urls_lock=attempted_urls_lock,
        )
    finally:
        runtime.release_cdp_port(worker_id)
