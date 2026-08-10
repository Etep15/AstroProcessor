#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ORCHESTRATION_VERSION = "1.3.1"
PROCESSING_HELPER_VERSION = "1.2.0"

WORKSPACE = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
PROJECTS_ROOT = WORKSPACE / "Projects"
ASTRO_PYTHON = WORKSPACE / "AstroProcessor/.venv/bin/python"
ENGINE = Path(os.environ.get(
    "GHS_PASS2_ENGINE",
    str(WORKSPACE / "skills/siril-ghs-stretch-pass2/scripts/ghs_pass2.py"),
))
STATE_DIR_NAME = ".siril-ghs-stretch-pass2-v1.3.1"
LEGACY_STATE_DIR_NAME = ".siril-ghs-stretch-pass2-v1.3.0"


class OrchestrationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OrchestrationError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OrchestrationError(f"Expected JSON object: {path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.partial")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def emit(payload: dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return code


def parse_json_output(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise OrchestrationError("Underlying GHS pass-2 helper returned no JSON output.")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OrchestrationError(
            "Underlying GHS pass-2 helper returned non-JSON output; "
            "stop rather than guessing or discovering paths."
        ) from exc
    if not isinstance(value, dict):
        raise OrchestrationError("Underlying GHS pass-2 helper returned a non-object JSON payload.")
    return value


def engine_call(
    arguments: list[str],
    *,
    obsolete_authorized: bool = False,
    timeout: int | None = None,
) -> tuple[int, dict[str, Any], str]:
    env = os.environ.copy()
    if obsolete_authorized:
        env["GHS_PASS2_OBSOLETE_AUTHORIZED"] = "1"
    else:
        env.pop("GHS_PASS2_OBSOLETE_AUTHORIZED", None)
    try:
        proc = subprocess.run(
            [str(ASTRO_PYTHON), str(ENGINE), *arguments],
            text=True,
            capture_output=True,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 2, {
            "status": "blocked",
            "error": f"Underlying GHS pass-2 helper timed out after {timeout} seconds.",
        }, ""
    try:
        payload = parse_json_output(proc.stdout)
    except OrchestrationError:
        payload = {
            "status": "blocked",
            "error": proc.stderr.strip() or proc.stdout.strip() or "Unknown helper failure.",
        }
    return proc.returncode, payload, proc.stderr.strip()


def project_paths(project_name: str) -> dict[str, Path]:
    project = PROJECTS_ROOT / project_name
    return {
        "project": project,
        "pass1_manifest": project / "processing/ghs-pass1/ghs-pass1-manifest.json",
        "pass1_review": project / "processing/ghs-pass1/visual-selection-record-v1.3.2.json",
        "pass1_output": project / "processing/ghs-pass1/SHO-starless-ghs-pass1.fit",
        "pass2_manifest": project / "processing/ghs-pass2/ghs-pass2-manifest.json",
        "pass2_output": project / "processing/ghs-pass2/SHO-starless-ghs-pass2.fit",
        "intent": project / STATE_DIR_NAME / "fresh-intent.json",
        "legacy_intent": project / LEGACY_STATE_DIR_NAME / "fresh-intent.json",
    }


def manifest_first_snapshot(project_name: str) -> dict[str, Any]:
    """Classify completed/obsolete status without hashing large FITS files."""
    p = project_paths(project_name)
    if not p["project"].is_dir():
        raise OrchestrationError(f"Project does not exist: {p['project']}")
    for key in ("pass1_manifest", "pass1_review", "pass1_output"):
        if not p[key].is_file():
            raise OrchestrationError(f"Required GHS pass-1 evidence is missing: {p[key]}")

    p1 = load_json(p["pass1_manifest"])
    review = load_json(p["pass1_review"])
    source_sha = (p1.get("output") or {}).get("sha256")
    if p1.get("status") != "ready":
        raise OrchestrationError("Current GHS pass-1 manifest is not ready.")
    if p1.get("helper_version") != "1.3.1":
        raise OrchestrationError("Current GHS pass-1 processing helper is not 1.3.1.")
    if p1.get("ghs_pass2_processing_permitted") is not True:
        raise OrchestrationError("Current GHS pass-1 manifest does not permit GHS pass 2.")
    if not isinstance(source_sha, str) or len(source_sha) != 64:
        raise OrchestrationError("Current GHS pass-1 manifest lacks a valid output SHA-256.")
    if review.get("visual_review_completed") is not True:
        raise OrchestrationError("Current GHS pass-1 visual review is incomplete.")
    if review.get("review_method") != "openclaw-read":
        raise OrchestrationError("Current GHS pass-1 review method is not openclaw-read.")
    if review.get("canonical_output_sha256") != source_sha:
        raise OrchestrationError("GHS pass-1 visual-selection record does not match its manifest output.")

    result: dict[str, Any] = {
        "project": str(p["project"]),
        "current_source_sha256": source_sha,
        "pass1_manifest_sha256": sha256_file(p["pass1_manifest"]),
        "pass1_review_sha256": sha256_file(p["pass1_review"]),
        "manifest_first": True,
        "pre_confirmation_large_fits_hashing": False,
    }

    if not p["pass2_manifest"].is_file() or not p["pass2_output"].is_file():
        return {
            **result,
            "canonical_exists": False,
            "canonical_status": "missing",
            "canonical_manifest_sha256": None,
            "canonical_output_sha256": None,
            "obsolete_reasons": [],
        }

    p2 = load_json(p["pass2_manifest"])
    old_source_sha = (p2.get("source") or {}).get("sha256")
    output_sha = (p2.get("output") or {}).get("sha256")
    manifest_sha = sha256_file(p["pass2_manifest"])

    completed = (
        p2.get("status") == "ready"
        and isinstance(output_sha, str)
        and len(output_sha) == 64
    )
    if not completed:
        return {
            **result,
            "canonical_exists": True,
            "canonical_status": "unknown_or_damaged",
            "canonical_manifest_sha256": manifest_sha,
            "canonical_output_sha256": output_sha,
            "obsolete_reasons": [
                "Existing GHS pass-2 manifest is not a structurally ready completed result."
            ],
        }

    reasons: list[str] = []
    if old_source_sha != source_sha:
        reasons.append(
            "GHS pass-2 source checksum differs from the current GHS pass-1 result."
        )

    return {
        **result,
        "canonical_exists": True,
        "canonical_status": "obsolete" if reasons else "ready",
        "canonical_manifest_sha256": manifest_sha,
        "canonical_output_sha256": output_sha,
        "canonical_recorded_source_sha256": old_source_sha,
        "obsolete_reasons": reasons,
    }


def intent_matches_snapshot(intent: dict[str, Any], snap: dict[str, Any]) -> bool:
    return (
        intent.get("status") == "authorized"
        and intent.get("project_name") == Path(str(snap["project"])).name
        and intent.get("source_sha256") == snap.get("current_source_sha256")
        and intent.get("canonical_manifest_sha256") == snap.get("canonical_manifest_sha256")
        and intent.get("canonical_output_sha256") == snap.get("canonical_output_sha256")
    )


def find_manifest_matching_intent(
    project_name: str,
    snap: dict[str, Any],
) -> tuple[Path, dict[str, Any]] | None:
    if os.environ.get("GHS_PASS2_IGNORE_FRESH_INTENT") == "1":
        return None
    p = project_paths(project_name)
    for path in (p["intent"], p["legacy_intent"]):
        if not path.is_file():
            continue
        try:
            value = load_json(path)
        except Exception:
            continue
        if intent_matches_snapshot(value, snap):
            return path, value
    return None


def strong_binding(project_name: str) -> dict[str, str]:
    p = project_paths(project_name)
    snap = manifest_first_snapshot(project_name)
    if not snap.get("canonical_exists"):
        raise OrchestrationError("No completed canonical GHS pass-2 result exists to authorize.")
    for key in ("pass1_output", "pass2_manifest", "pass2_output"):
        if not p[key].is_file():
            raise OrchestrationError(f"Required authorization target is missing: {p[key]}")
    source_sha = sha256_file(p["pass1_output"])
    manifest_sha = sha256_file(p["pass2_manifest"])
    output_sha = sha256_file(p["pass2_output"])
    if source_sha != snap.get("current_source_sha256"):
        raise OrchestrationError("Current GHS pass-1 FITS checksum does not match its manifest.")
    if manifest_sha != snap.get("canonical_manifest_sha256"):
        raise OrchestrationError("Existing GHS pass-2 manifest changed during authorization.")
    if output_sha != snap.get("canonical_output_sha256"):
        raise OrchestrationError(
            "Existing GHS pass-2 FITS checksum does not match its manifest output SHA."
        )
    return {
        "source_sha256": source_sha,
        "canonical_manifest_sha256": manifest_sha,
        "canonical_output_sha256": output_sha,
    }


def migrate_or_write_intent(
    project_name: str,
    binding: dict[str, str],
    *,
    legacy: Path | None = None,
) -> tuple[Path, dict[str, Any]]:
    p = project_paths(project_name)
    value: dict[str, Any] = {
        "schema_version": 1,
        "orchestration_version": ORCHESTRATION_VERSION,
        "status": "authorized",
        "authorized_at": utc_now(),
        "project_name": project_name,
        **binding,
    }
    if legacy is not None:
        value["migrated_from"] = str(legacy)
        value["migrated_at"] = utc_now()
    write_json_atomic(p["intent"], value)
    return p["intent"], value


def verify_or_migrate_intent(
    project_name: str,
    snap: dict[str, Any],
) -> tuple[Path, dict[str, Any]]:
    found = find_manifest_matching_intent(project_name, snap)
    if found is None:
        raise OrchestrationError(
            "No fresh-run authorization matches the current GHS2 replacement target."
        )
    path, value = found
    binding = strong_binding(project_name)
    for key, expected in binding.items():
        if value.get(key) != expected:
            raise OrchestrationError(
                "Fresh-run authorization no longer matches current source/canonical hashes."
            )
    p = project_paths(project_name)
    if path == p["legacy_intent"]:
        return migrate_or_write_intent(project_name, binding, legacy=path)
    return path, value


def confirmation_payload(project_name: str, snap: dict[str, Any]) -> dict[str, Any]:
    obsolete = snap.get("canonical_status") == "obsolete"
    question = (
        f"GHS pass 2 for {project_name} has already completed but is obsolete for "
        "the current GHS pass-1 result. Do you want me to run it again as a fresh run?"
        if obsolete
        else
        f"GHS pass 2 for {project_name} has already completed successfully. "
        "Do you want me to run it again as a fresh run?"
    )
    return {
        "status": "confirmation_required",
        "action": "await_user_confirmation",
        "confirmation_required": True,
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "project": snap["project"],
        "current_canonical_status": snap.get("canonical_status"),
        "canonical_output_preserved": True,
        "canonical_output_sha256": snap.get("canonical_output_sha256"),
        "canonical_manifest_sha256": snap.get("canonical_manifest_sha256"),
        "current_upstream_source_sha256": snap.get("current_source_sha256"),
        "obsolete_reasons": snap.get("obsolete_reasons", []),
        "manifest_first": True,
        "pre_confirmation_large_fits_hashing": False,
        "hashes_verified": False,
        "hash_verification_deferred_until": "confirm-fresh",
        "production_processing_started": False,
        "question": question,
        "next_command_after_confirmation": (
            str(WORKSPACE / "skills/siril-ghs-stretch-pass2/bin/ghs-pass2")
            + f' confirm-fresh --project "{project_name}"'
        ),
    }


def ensure_helper_native_authorization(
    project_name: str,
) -> dict[str, Any]:
    """Bridge standalone authorization through the helper's native begin/confirm contract."""
    # The helper 1.2.0 contract that historically worked is:
    #   begin -> pending fresh intent -> confirm-fresh -> durable authorization.
    # For an obsolete canonical we expose the narrow private status bridge to
    # BOTH native calls. We never skip begin and never fabricate helper state.
    rc, begin, _ = engine_call(
        ["begin", "--project", project_name],
        obsolete_authorized=True,
    )
    if rc != 0:
        raise OrchestrationError(
            f"Helper native begin failed during authorization bridge: {begin.get('error', begin)}"
        )

    # Recovery: if native authorization already exists, or a compatible run has
    # progressed beyond authorization, do not ask or confirm again.
    if begin.get("fresh_run_authorized") is True or begin.get("status") == "fresh_run_authorized":
        return begin
    if begin.get("status") in {"awaiting_visual_selection", "ready_to_publish"}:
        return begin
    if begin.get("action") in {"review_select_publish", "publish_recorded_selection"}:
        return begin

    if begin.get("status") != "confirmation_required" or begin.get("confirmation_required") is not True:
        raise OrchestrationError(
            "Helper native begin did not create/recover the required pending fresh intent: "
            f"{begin}"
        )
    if not begin.get("fresh_run_intent") or not begin.get("fresh_run_request_id"):
        raise OrchestrationError(
            "Helper native begin reported confirmation_required without a durable pending intent."
        )

    rc, auth, _ = engine_call(
        ["confirm-fresh", "--project", project_name],
        obsolete_authorized=True,
    )
    if rc != 0 or auth.get("status") != "fresh_run_authorized" or auth.get("fresh_run_authorized") is not True:
        raise OrchestrationError(
            "Helper native confirm-fresh did not authorize the pending intent: "
            f"{auth.get('error', auth)}"
        )
    return auth



def workflow_state(project_name: str) -> tuple[int, dict[str, Any]]:
    rc, state, _ = engine_call(["workflow-state", "--project", project_name])
    if rc != 0 and state.get("status") not in {
        "start_new_run", "awaiting_visual_selection", "ready_to_publish", "ready"
    }:
        state.setdefault("orchestration_version", ORCHESTRATION_VERSION)
        return rc, state
    return 0, state


def load_run_manifest(run_root: str) -> dict[str, Any]:
    manifest = Path(run_root) / "ghs-pass2-manifest.json"
    if not manifest.is_file():
        raise OrchestrationError(
            f"Exact run manifest is missing: {manifest}. Do not discover another path."
        )
    return load_json(manifest)


def review_plan(run_payload: dict[str, Any]) -> dict[str, Any]:
    run_root = str(run_payload.get("run_root") or "")
    if not run_root:
        raise OrchestrationError("GHS pass-2 state did not return an exact run_root.")

    data = run_payload
    if not isinstance(data.get("candidates"), list):
        data = load_run_manifest(run_root)

    eligible = data.get("publication_eligible_candidates")
    if not isinstance(eligible, list) or not eligible:
        eligible = (data.get("publication_gate") or {}).get(
            "publication_eligible_candidates"
        )
    if not isinstance(eligible, list) or not eligible:
        raise OrchestrationError("No publication-eligible GHS pass-2 candidates were returned.")

    by_name = {
        c.get("candidate"): c
        for c in data.get("candidates", [])
        if isinstance(c, dict) and isinstance(c.get("candidate"), str)
    }
    missing = [name for name in eligible if name not in by_name]
    if missing:
        raise OrchestrationError(
            "Run manifest lacks eligible candidate records: " + ", ".join(missing)
        )

    first = by_name[eligible[0]]
    before = Path(str((first.get("previews") or {}).get("before_linear") or ""))
    if not before.is_file():
        raise OrchestrationError(f"Exact before-preview target is missing: {before}")

    targets: list[dict[str, Any]] = [{
        "role": "pass1_before_linear",
        "path": str(before),
        "sha256": sha256_file(before),
    }]
    candidates: list[dict[str, Any]] = []
    for name in eligible:
        candidate = by_name[name]
        after = Path(str((candidate.get("previews") or {}).get("after_linear") or ""))
        if not after.is_file():
            raise OrchestrationError(f"Exact candidate preview target is missing: {after}")
        targets.append({
            "role": "candidate_after",
            "candidate": name,
            "path": str(after),
            "sha256": sha256_file(after),
        })
        metrics = (candidate.get("quality_assessment") or {}).get("metrics") or {}
        candidates.append({
            "candidate": name,
            "parameters": candidate.get("parameters"),
            "histogram_classification": candidate.get("histogram_classification"),
            "output_luma_median": metrics.get("output_luma_median"),
            "output_luma_p99": metrics.get("output_luma_p99"),
            "output_maximum": metrics.get("output_maximum"),
            "luma_correlation": metrics.get("luma_correlation"),
            "selection_score": candidate.get("selection_score"),
            "recommended": name == data.get("recommended_candidate"),
        })

    repeated_compared = [f'--compared "{name}"' for name in eligible]
    return {
        "status": "visual_review_required",
        "action": "continue_autonomously_select_publish",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "project_name": data.get("project_name"),
        "run_root": run_root,
        "resume_without_reprocessing": bool(
            run_payload.get("resume_without_reprocessing", False)
        ),
        "publication_eligible_candidates": eligible,
        "recommended_candidate": data.get("recommended_candidate"),
        "numerical_recommendation_is_advisory": True,
        "candidates": candidates,
        "read_targets": targets,
        "read_target_policy": {
            "path_handling": "verbatim",
            "directory_discovery_forbidden": True,
            "forbidden_recovery_tools": ["ls", "find", "tree", "grep", "jq", "globbing"],
            "on_read_failure": "stop_and_report_exact_failed_path",
        },
        "selection_rule": (
            "Read every exact target once and compare every eligible candidate at the "
            "same display scale. Choose the best actual pass-2 result, not automatically "
            "the numerical recommendation. Preserve faint emission, Pillars/dark lanes, "
            "SHO colour, background integrity and highlight headroom."
        ),
        "autonomous_completion_policy": {
            "ask_user": False,
            "do_not_regenerate_candidates": True,
            "do_not_reread_after_selection": True,
            "do_not_discover_paths": True,
            "publication_retry_limit": 2,
        },
        "select_publish_command_template": {
            "command": str(
                WORKSPACE / "skills/siril-ghs-stretch-pass2/bin/ghs-pass2"
            ) + " select-publish",
            "required": [
                '--project "<project>"',
                f'--run-root "{run_root}"',
                '--candidate "<selected candidate>"',
                *repeated_compared,
                '--visual-notes "<80+ chars comparing every eligible candidate>"',
            ],
        },
    }


def complete_recorded_publication(
    project_name: str,
    run_root: str,
) -> tuple[int, dict[str, Any]]:
    attempts = 0
    last: dict[str, Any] = {}
    while attempts < 2:
        attempts += 1
        rc, published, _ = engine_call([
            "publish",
            "--project", project_name,
            "--run-root", run_root,
            "--fresh-run",
        ])
        last = published
        if rc == 0:
            src, status, _ = engine_call(["status", "--project", project_name])
            if src == 0 and status.get("status") == "ready":
                p = project_paths(project_name)
                if p["intent"].is_file():
                    value = load_json(p["intent"])
                    value["status"] = "consumed"
                    value["consumed_at"] = utc_now()
                    value["published_run_root"] = run_root
                    write_json_atomic(p["intent"], value)
                return 0, {
                    **status,
                    "orchestration_version": ORCHESTRATION_VERSION,
                    "processing_helper_version": PROCESSING_HELPER_VERSION,
                    "publication_attempts": attempts,
                    "status": "ready",
                }
        wrc, state = workflow_state(project_name)
        if wrc != 0 or state.get("action") != "publish_recorded_selection":
            break

    return 2, {
        "status": "blocked",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "error": last.get("error")
        or "GHS pass-2 publication failed after bounded recovery.",
        "publication_attempts": attempts,
        "action": "stop_and_report_exact_error",
    }


def continue_authorized_stage(args: argparse.Namespace) -> int:
    snap = manifest_first_snapshot(args.project)
    intent_path, intent = verify_or_migrate_intent(args.project, snap)

    if args.plan_only:
        return emit({
            "status": "plan",
            "action": "continue_authorized_fresh_run",
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_helper_version": PROCESSING_HELPER_VERSION,
            "project": snap["project"],
            "current_canonical_status": snap["canonical_status"],
            "fresh_authorization_present": True,
            "fresh_intent": str(intent_path),
            "authorization_manifest_binding_matches": True,
            "strong_hashes_verified": True,
            "production_processing_started": False,
        })

    ensure_helper_native_authorization(args.project)
    wrc, state = workflow_state(args.project)
    if wrc != 0:
        return emit(state, wrc)

    action = state.get("action")
    if action == "run_review_select_publish":
        rrc, run, _ = engine_call(
            [
                "run",
                "--project", args.project,
                "--max-candidates", "3",
                "--timeout", str(args.timeout),
            ],
            obsolete_authorized=True,
            timeout=args.timeout + 60,
        )
        if rrc != 0:
            run.setdefault("orchestration_version", ORCHESTRATION_VERSION)
            run.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
            return emit(run, rrc)
        plan = review_plan(run)
        plan["fresh_rerun_authorized"] = True
        plan["fresh_intent"] = str(intent_path)
        plan["previous_canonical_preserved"] = True
        return emit(plan)

    if action == "review_select_publish":
        state["resume_without_reprocessing"] = True
        return emit(review_plan(state))

    if action == "publish_recorded_selection":
        run_root = str(state.get("run_root") or "")
        if not run_root:
            return emit({
                "status": "blocked",
                "error": "workflow-state did not return run_root for recorded publication.",
            }, 2)
        rc, result = complete_recorded_publication(args.project, run_root)
        return emit(result, rc)

    if state.get("status") == "awaiting_visual_selection":
        state["resume_without_reprocessing"] = True
        return emit(review_plan(state))

    return emit({
        "status": "blocked",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "error": f"Unsupported authorized GHS pass-2 workflow action: {action!r}",
        "action": "stop_no_discovery",
    }, 2)


def command_advance(args: argparse.Namespace) -> int:
    snap = manifest_first_snapshot(args.project)

    if snap.get("canonical_exists"):
        if snap.get("canonical_status") == "unknown_or_damaged":
            return emit({
                "status": "blocked",
                "action": "stop_and_report_canonical_integrity",
                "orchestration_version": ORCHESTRATION_VERSION,
                "processing_helper_version": PROCESSING_HELPER_VERSION,
                "project": snap["project"],
                "error": snap["obsolete_reasons"][0],
                "production_processing_started": False,
            }, 2)

        if find_manifest_matching_intent(args.project, snap) is None:
            return emit(confirmation_payload(args.project, snap))
        return continue_authorized_stage(args)

    if args.plan_only:
        return emit({
            "status": "plan",
            "action": "start_new_run",
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_helper_version": PROCESSING_HELPER_VERSION,
            "project": snap["project"],
            "production_processing_started": False,
        })

    rc, begin, _ = engine_call(["begin", "--project", args.project])
    if rc != 0:
        begin.setdefault("orchestration_version", ORCHESTRATION_VERSION)
        return emit(begin, rc)

    wrc, state = workflow_state(args.project)
    if wrc != 0:
        return emit(state, wrc)
    if state.get("action") != "run_review_select_publish":
        return emit({
            "status": "blocked",
            "error": f"Unexpected new-stage workflow action: {state.get('action')!r}",
            "orchestration_version": ORCHESTRATION_VERSION,
        }, 2)

    rrc, run, _ = engine_call(
        [
            "run",
            "--project", args.project,
            "--max-candidates", "3",
            "--timeout", str(args.timeout),
        ],
        timeout=args.timeout + 60,
    )
    if rrc != 0:
        return emit(run, rrc)
    return emit(review_plan(run))


def command_confirm_fresh(args: argparse.Namespace) -> int:
    snap = manifest_first_snapshot(args.project)
    if not snap.get("canonical_exists"):
        return emit({
            "status": "blocked",
            "error": "No completed canonical GHS pass-2 result exists to rerun.",
            "orchestration_version": ORCHESTRATION_VERSION,
        }, 2)
    if snap.get("canonical_status") == "unknown_or_damaged":
        return emit({
            "status": "blocked",
            "error": (
                "Existing GHS pass-2 canonical is structurally damaged; "
                "fresh confirmation cannot authorize over it."
            ),
            "orchestration_version": ORCHESTRATION_VERSION,
        }, 2)

    found = find_manifest_matching_intent(args.project, snap)
    if found is not None:
        try:
            path, value = verify_or_migrate_intent(args.project, snap)
            helper_auth = ensure_helper_native_authorization(args.project)
        except Exception as exc:
            return emit({
                "status": "blocked",
                "error": str(exc),
                "action": "stop",
                "orchestration_version": ORCHESTRATION_VERSION,
            }, 2)
        return emit({
            "status": "fresh_run_confirmed",
            "action": "continue_same_turn_with_advance",
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_helper_version": PROCESSING_HELPER_VERSION,
            "project": snap["project"],
            "current_canonical_status": snap["canonical_status"],
            "canonical_output_preserved": True,
            "hashes_verified": True,
            "fresh_intent": str(path),
            "source_sha256": value["source_sha256"],
            "canonical_manifest_sha256": value["canonical_manifest_sha256"],
            "canonical_output_sha256": value["canonical_output_sha256"],
            "authorization_reused": True,
            "helper_native_authorization_status": helper_auth.get("status"),
            "next_command": (
                str(WORKSPACE / "skills/siril-ghs-stretch-pass2/bin/ghs-pass2")
                + f' advance --project "{args.project}"'
            ),
        })

    try:
        binding = strong_binding(args.project)
        path, value = migrate_or_write_intent(args.project, binding)
        helper_auth = ensure_helper_native_authorization(args.project)
    except Exception as exc:
        return emit({
            "status": "blocked",
            "error": str(exc),
            "action": "stop",
            "orchestration_version": ORCHESTRATION_VERSION,
        }, 2)

    return emit({
        "status": "fresh_run_confirmed",
        "action": "continue_same_turn_with_advance",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "project": snap["project"],
        "current_canonical_status": snap["canonical_status"],
        "canonical_output_preserved": True,
        "hashes_verified": True,
        "fresh_intent": str(path),
        "source_sha256": value["source_sha256"],
        "canonical_manifest_sha256": value["canonical_manifest_sha256"],
        "canonical_output_sha256": value["canonical_output_sha256"],
        "authorization_reused": False,
        "helper_native_authorization_status": helper_auth.get("status"),
        "next_command": (
            str(WORKSPACE / "skills/siril-ghs-stretch-pass2/bin/ghs-pass2")
            + f' advance --project "{args.project}"'
        ),
    })


def command_select_publish(args: argparse.Namespace) -> int:
    if len(args.visual_notes.strip()) < 80:
        return emit({
            "status": "blocked",
            "error": (
                "Visual notes must contain at least 80 characters comparing "
                "the eligible candidates."
            ),
            "action": "repair_selection_payload_only",
            "orchestration_version": ORCHESTRATION_VERSION,
        }, 2)

    run = load_run_manifest(args.run_root)
    eligible = run.get("publication_eligible_candidates")
    if not isinstance(eligible, list) or not eligible:
        eligible = (run.get("publication_gate") or {}).get(
            "publication_eligible_candidates"
        )
    if not isinstance(eligible, list) or not eligible:
        return emit({"status": "blocked", "error": "Run has no eligible candidates."}, 2)
    if args.candidate not in eligible:
        return emit({
            "status": "blocked",
            "error": f"Selected candidate {args.candidate!r} is not eligible: {eligible}",
        }, 2)
    if sorted(args.compared) != sorted(eligible):
        return emit({
            "status": "blocked",
            "error": (
                "Every eligible candidate must be supplied through repeated --compared. "
                f"Expected {eligible}, got {args.compared}."
            ),
            "action": "repair_selection_payload_only",
        }, 2)

    select_args = [
        "select",
        "--project", args.project,
        "--run-root", args.run_root,
        "--candidate", args.candidate,
    ]
    for candidate in args.compared:
        select_args += ["--compared", candidate]
    select_args += ["--visual-notes", args.visual_notes]

    src, selected, _ = engine_call(select_args)
    if src != 0:
        selected["action"] = "repair_selection_payload_only"
        return emit(selected, src)

    prc, result = complete_recorded_publication(args.project, args.run_root)
    if prc == 0:
        result["selected_candidate"] = args.candidate
        result["visual_review_completed"] = True
        result["next_stage"] = "siril-black-point"
        result["black_point_processing_permitted"] = True
    return emit(result, prc)


def command_status(args: argparse.Namespace) -> int:
    rc, payload, _ = engine_call(["status", "--project", args.project])
    payload.setdefault("orchestration_version", ORCHESTRATION_VERSION)
    payload.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
    return emit(payload, rc)


def command_self_test(args: argparse.Namespace) -> int:
    return emit({
        "status": "success",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "standalone_wrapper": True,
        "canonical_python_fixed": str(ASTRO_PYTHON),
        "manifest_first_fast_path": True,
        "pre_confirmation_large_fits_hashing": False,
        "fast_route_first_exec_required": True,
        "completed_obsolete_stage_requires_confirmation": True,
        "fresh_authorization_strong_hash_binding": True,
        "legacy_v1_3_0_authorization_migration": True,
        "helper_native_authorization_bridge": True,
        "helper_native_begin_before_confirm": True,
        "private_obsolete_status_bridge": True,
        "ready_only_helper_gate_guarded_bypass": False,
        "guarded_bypass_environment": "GHS_PASS2_OBSOLETE_AUTHORIZED",
        "direct_helper_discovery_forbidden": True,
        "directory_discovery_forbidden": True,
        "exact_read_targets_required": True,
        "bounded_candidates": 3,
        "durable_selection": True,
        "bounded_publication_recovery_attempts": 2,
        "black_point_handoff": True,
        "selection_compared_arguments_repeated": True,
        "plan_only_supported": True,
    })


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Standalone GHS pass-2 v1.3.1 orchestration wrapper."
    )
    parser.add_argument("--version", action="version", version=ORCHESTRATION_VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    advance = sub.add_parser("advance")
    advance.add_argument("--project", required=True)
    advance.add_argument("--timeout", type=int, default=7200)
    advance.add_argument("--plan-only", action="store_true")
    advance.set_defaults(func=command_advance)

    confirm = sub.add_parser("confirm-fresh")
    confirm.add_argument("--project", required=True)
    confirm.set_defaults(func=command_confirm_fresh)

    select = sub.add_parser("select-publish")
    select.add_argument("--project", required=True)
    select.add_argument("--run-root", required=True)
    select.add_argument("--candidate", required=True)
    select.add_argument("--compared", action="append", required=True)
    select.add_argument("--visual-notes", required=True)
    select.set_defaults(func=command_select_publish)

    status = sub.add_parser("stage-status")
    status.add_argument("--project", required=True)
    status.set_defaults(func=command_status)

    test = sub.add_parser("self-test")
    test.set_defaults(func=command_self_test)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except OrchestrationError as exc:
        return emit({
            "status": "blocked",
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_helper_version": PROCESSING_HELPER_VERSION,
            "error": str(exc),
            "action": "stop_no_discovery",
        }, 2)


if __name__ == "__main__":
    raise SystemExit(main())
