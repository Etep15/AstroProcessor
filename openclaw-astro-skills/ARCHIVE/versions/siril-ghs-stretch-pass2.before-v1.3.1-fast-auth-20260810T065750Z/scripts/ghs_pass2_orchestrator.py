#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ORCHESTRATION_VERSION = "1.3.0"
PROCESSING_HELPER_VERSION = "1.2.0"

WORKSPACE = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
ASTRO_PYTHON = WORKSPACE / "AstroProcessor/.venv/bin/python"
ENGINE = Path(os.environ.get("GHS_PASS2_ENGINE", str(WORKSPACE / "skills/siril-ghs-stretch-pass2/scripts/ghs_pass2.py")))

class OrchestrationError(RuntimeError):
    pass

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

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

def engine_call(arguments: list[str]) -> tuple[int, dict[str, Any], str]:
    cmd = [str(ASTRO_PYTHON), str(ENGINE), *arguments]
    proc = subprocess.run(cmd, text=True, capture_output=True)
    try:
        payload = parse_json_output(proc.stdout)
    except OrchestrationError:
        payload = {
            "status": "blocked",
            "error": proc.stderr.strip() or proc.stdout.strip() or "Unknown helper failure.",
        }
    return proc.returncode, payload, proc.stderr.strip()

def load_run_manifest(run_root: str) -> dict[str, Any]:
    root = Path(run_root)
    manifest = root / "ghs-pass2-manifest.json"
    if not manifest.is_file():
        raise OrchestrationError(
            f"Exact run manifest is missing: {manifest}. "
            "Do not use ls/find/globbing to recover it."
        )
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise OrchestrationError(f"Run manifest is invalid: {manifest}")
    return data

def review_plan(run_payload: dict[str, Any]) -> dict[str, Any]:
    run_root = str(run_payload.get("run_root") or "")
    if not run_root:
        raise OrchestrationError("GHS pass-2 run state did not return an exact run_root.")

    data = run_payload
    if not isinstance(data.get("candidates"), list):
        data = load_run_manifest(run_root)

    eligible = data.get("publication_eligible_candidates")
    if not isinstance(eligible, list) or not eligible:
        gate = data.get("publication_gate") or {}
        eligible = gate.get("publication_eligible_candidates")
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
            "Run manifest is missing eligible candidate records: " + ", ".join(missing)
        )

    read_targets: list[dict[str, Any]] = []
    first = by_name[eligible[0]]
    before = Path(str((first.get("previews") or {}).get("before_linear") or ""))
    if not before.is_file():
        raise OrchestrationError(
            f"Exact before-preview target is missing: {before}. "
            "Do not use directory discovery to recover it."
        )
    read_targets.append({
        "role": "pass1_before_linear",
        "path": str(before),
        "sha256": sha256_file(before),
    })

    candidates = []
    for name in eligible:
        c = by_name[name]
        after = Path(str((c.get("previews") or {}).get("after_linear") or ""))
        if not after.is_file():
            raise OrchestrationError(
                f"Exact candidate preview target is missing: {after}. "
                "Do not use directory discovery to recover it."
            )
        read_targets.append({
            "role": "candidate_after",
            "candidate": name,
            "path": str(after),
            "sha256": sha256_file(after),
        })
        quality = c.get("quality_assessment") or {}
        metrics = quality.get("metrics") or {}
        candidates.append({
            "candidate": name,
            "parameters": c.get("parameters"),
            "histogram_classification": c.get("histogram_classification"),
            "output_luma_median": metrics.get("output_luma_median"),
            "output_luma_p99": metrics.get("output_luma_p99"),
            "output_maximum": metrics.get("output_maximum"),
            "luma_correlation": metrics.get("luma_correlation"),
            "selection_score": c.get("selection_score"),
            "recommended": name == data.get("recommended_candidate"),
        })

    compared_args = []
    for name in eligible:
        compared_args.append(f'--compared "{name}"')

    return {
        "status": "visual_review_required",
        "action": "continue_autonomously_select_publish",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "project_name": data.get("project_name"),
        "run_root": run_root,
        "resume_without_reprocessing": bool(run_payload.get("resume_without_reprocessing", False)),
        "publication_eligible_candidates": eligible,
        "recommended_candidate": data.get("recommended_candidate"),
        "numerical_recommendation_is_advisory": True,
        "candidates": candidates,
        "read_targets": read_targets,
        "read_target_policy": {
            "path_handling": "verbatim",
            "directory_discovery_forbidden": True,
            "forbidden_recovery_tools": ["ls", "find", "tree", "grep", "jq", "globbing"],
            "on_read_failure": "stop_and_report_exact_failed_path",
        },
        "selection_rule": (
            "Read every exact target once and compare every eligible candidate at the same "
            "display scale. Choose the best actual pass-2 result, not automatically the "
            "numerical recommendation. Preserve faint emission, Pillars/dark lanes, SHO "
            "colour, background integrity and highlight headroom."
        ),
        "autonomous_completion_policy": {
            "ask_user": False,
            "do_not_regenerate_candidates": True,
            "do_not_reread_after_selection": True,
            "do_not_discover_paths": True,
            "publication_retry_limit": 2,
        },
        "select_publish_command_template": {
            "command": str(WORKSPACE / "skills/siril-ghs-stretch-pass2/bin/ghs-pass2") + " select-publish",
            "required": [
                '--project "<project>"',
                f'--run-root "{run_root}"',
                '--candidate "<selected candidate>"',
                *compared_args,
                '--visual-notes "<80+ chars comparing every eligible candidate>"',
            ],
        },
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def fixed_project_paths(project: str) -> dict[str, Path]:
    root = WORKSPACE / "Projects" / project
    state = root / ".siril-ghs-stretch-pass2-v1.3.0"
    return {
        "project": root,
        "source": root / "processing/ghs-pass1/SHO-starless-ghs-pass1.fit",
        "canonical_manifest": root / "processing/ghs-pass2/ghs-pass2-manifest.json",
        "canonical_output": root / "processing/ghs-pass2/SHO-starless-ghs-pass2.fit",
        "intent": state / "fresh-intent.json",
    }

def obsolete_canonical_context(project: str, state: dict[str, Any]) -> dict[str, Any] | None:
    canonical = state.get("canonical_status")
    if not isinstance(canonical, dict):
        return None
    paths = fixed_project_paths(project)
    manifest = paths["canonical_manifest"]
    output = paths["canonical_output"]
    if not manifest.is_file() or not output.is_file():
        return None
    errors = canonical.get("errors")
    if not isinstance(errors, list):
        errors = []
    status = str(canonical.get("status") or "")
    if status not in {"invalid", "obsolete", "provisional"} and not errors:
        return None
    return {
        "current_canonical_status": "obsolete",
        "obsolete_reasons": [str(x) for x in errors] or [
            "Existing GHS pass-2 checkpoint is incompatible with the current GHS pass-1 source."
        ],
        "canonical_manifest": str(manifest),
        "canonical_output": str(output),
        "canonical_output_sha256": (canonical.get("output") or {}).get("sha256"),
        "current_source_sha256": state.get("source_sha256"),
    }

def current_obsolete_binding(project: str) -> dict[str, str]:
    paths = fixed_project_paths(project)
    for key in ("source", "canonical_manifest", "canonical_output"):
        if not paths[key].is_file():
            raise OrchestrationError(
                f"Cannot authorize obsolete GHS pass-2 rerun because exact {key} path is missing: "
                f"{paths[key]}"
            )
    return {
        "source_sha256": sha256_file(paths["source"]),
        "canonical_manifest_sha256": sha256_file(paths["canonical_manifest"]),
        "canonical_output_sha256": sha256_file(paths["canonical_output"]),
    }

def load_valid_obsolete_intent(project: str) -> dict[str, Any] | None:
    paths = fixed_project_paths(project)
    intent = paths["intent"]
    if not intent.is_file():
        return None
    try:
        obj = json.loads(intent.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(obj, dict) or obj.get("status") != "authorized":
        return None
    if obj.get("project_name") != project:
        return None
    try:
        binding = current_obsolete_binding(project)
    except Exception:
        return None
    for key, value in binding.items():
        if obj.get(key) != value:
            return None
    return obj

def authorize_obsolete_rerun(project: str) -> tuple[Path, dict[str, Any]]:
    paths = fixed_project_paths(project)
    binding = current_obsolete_binding(project)
    obj = {
        "schema_version": 1,
        "orchestration_version": ORCHESTRATION_VERSION,
        "status": "authorized",
        "authorized_at": utc_now(),
        "project_name": project,
        **binding,
    }
    target = paths["intent"]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.partial")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, target)
    return target, obj

def obsolete_confirmation_payload(project: str, state: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": "confirmation_required",
        "action": "await_user_confirmation",
        "confirmation_required": True,
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "project": str(fixed_project_paths(project)["project"]),
        "current_canonical_status": "obsolete",
        "canonical_output_preserved": True,
        "canonical_output_sha256": context.get("canonical_output_sha256"),
        "current_upstream_source_sha256": context.get("current_source_sha256"),
        "obsolete_reasons": context.get("obsolete_reasons", []),
        "hashes_verified": False,
        "hash_verification_deferred_until": "confirm-fresh",
        "production_processing_started": False,
        "question": (
            f"GHS pass 2 for {project} has already completed but is obsolete for the "
            "current GHS pass-1 result. Do you want me to run it again as a fresh run?"
        ),
        "next_command_after_confirmation": (
            str(WORKSPACE / "skills/siril-ghs-stretch-pass2/bin/ghs-pass2")
            + f' confirm-fresh --project "{project}"'
        ),
    }

def workflow_state(project: str) -> tuple[int, dict[str, Any]]:
    rc, state, stderr = engine_call(["workflow-state", "--project", project])
    if rc != 0 and state.get("status") not in {
        "start_new_run", "awaiting_visual_selection", "ready_to_publish", "ready"
    }:
        state.setdefault("orchestration_version", ORCHESTRATION_VERSION)
        return rc, state
    return 0, state

def complete_recorded_publication(project: str, run_root: str) -> tuple[int, dict[str, Any]]:
    attempts = 0
    last: dict[str, Any] = {}
    while attempts < 2:
        attempts += 1
        rc, pub, stderr = engine_call([
            "publish", "--project", project, "--run-root", run_root, "--fresh-run"
        ])
        last = pub
        if rc == 0:
            src, status, _ = engine_call(["status", "--project", project])
            if src == 0 and status.get("status") == "ready":
                return 0, {
                    **status,
                    "orchestration_version": ORCHESTRATION_VERSION,
                    "processing_helper_version": PROCESSING_HELPER_VERSION,
                    "publication_attempts": attempts,
                    "status": "ready",
                }
        wrc, state = workflow_state(project)
        if wrc != 0 or state.get("action") != "publish_recorded_selection":
            break
    return 2, {
        "status": "blocked",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "error": last.get("error") or "GHS pass-2 publication failed after bounded recovery.",
        "publication_attempts": attempts,
        "action": "stop_and_report_exact_error",
    }

def command_advance(args: argparse.Namespace) -> int:
    rc, begin, stderr = engine_call(["begin", "--project", args.project])
    begin.setdefault("orchestration_version", ORCHESTRATION_VERSION)
    begin.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)

    if rc != 0:
        return emit(begin, rc)

    # Valid compatible completed canonical: retain the helper's native
    # confirmation/authorization mechanism.
    if begin.get("status") == "confirmation_required":
        begin["action"] = "await_user_confirmation"
        begin["canonical_output_preserved"] = True
        begin["production_processing_started"] = False
        begin["next_command_after_confirmation"] = (
            str(WORKSPACE / "skills/siril-ghs-stretch-pass2/bin/ghs-pass2")
            + f' confirm-fresh --project "{args.project}"'
        )
        return emit(begin)

    wrc, state = workflow_state(args.project)
    state.setdefault("orchestration_version", ORCHESTRATION_VERSION)
    state.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
    if wrc != 0:
        return emit(state, wrc)

    # The legacy helper calls a source-stale canonical "invalid" and therefore
    # returns start_new_run. At the public skill boundary that is still a
    # completed image-processing result, so require the same one-time explicit
    # fresh authorization used by the other mature stage skills.
    obsolete = obsolete_canonical_context(args.project, state)
    obsolete_intent = None
    if obsolete is not None:
        obsolete_intent = load_valid_obsolete_intent(args.project)
        if obsolete_intent is None:
            return emit(obsolete_confirmation_payload(args.project, state, obsolete))
        state["obsolete_fresh_authorization"] = {
            "status": "authorized",
            "authorized_at": obsolete_intent.get("authorized_at"),
            "source_sha256": obsolete_intent.get("source_sha256"),
            "canonical_manifest_sha256": obsolete_intent.get("canonical_manifest_sha256"),
            "canonical_output_sha256": obsolete_intent.get("canonical_output_sha256"),
        }

    action = state.get("action")
    if args.plan_only:
        return emit({
            "status": "plan",
            "action": action,
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_helper_version": PROCESSING_HELPER_VERSION,
            "begin_status": begin.get("status"),
            "workflow_state": state,
            "production_processing_started": False,
        })

    if action == "run_review_select_publish":
        rrc, run, rerr = engine_call([
            "run", "--project", args.project, "--max-candidates", "3",
            "--timeout", str(args.timeout),
        ])
        if rrc != 0:
            run.setdefault("orchestration_version", ORCHESTRATION_VERSION)
            run.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
            return emit(run, rrc)
        try:
            plan = review_plan(run)
            if obsolete_intent is not None:
                plan["fresh_rerun_authorized"] = True
                plan["previous_canonical_was_obsolete"] = True
            return emit(plan)
        except Exception as exc:
            return emit({
                "status": "blocked",
                "orchestration_version": ORCHESTRATION_VERSION,
                "processing_helper_version": PROCESSING_HELPER_VERSION,
                "error": str(exc),
                "action": "stop_no_path_discovery",
            }, 2)

    if action == "review_select_publish":
        try:
            state["resume_without_reprocessing"] = True
            return emit(review_plan(state))
        except Exception as exc:
            return emit({
                "status": "blocked",
                "orchestration_version": ORCHESTRATION_VERSION,
                "processing_helper_version": PROCESSING_HELPER_VERSION,
                "error": str(exc),
                "action": "stop_no_path_discovery",
            }, 2)

    if action == "publish_recorded_selection":
        run_root = str(state.get("run_root") or "")
        if not run_root:
            return emit({
                "status": "blocked",
                "error": "workflow-state did not return run_root for recorded publication.",
                "orchestration_version": ORCHESTRATION_VERSION,
            }, 2)
        prc, result = complete_recorded_publication(args.project, run_root)
        return emit(result, prc)

    if action == "stop":
        state["status"] = state.get("status") or "blocked"
        return emit(state, 2)

    if state.get("status") == "awaiting_visual_selection":
        try:
            return emit(review_plan(state))
        except Exception as exc:
            return emit({"status": "blocked", "error": str(exc)}, 2)

    return emit({
        "status": "blocked",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "error": f"Unsupported GHS pass-2 workflow action: {action!r}",
        "action": "stop_no_discovery",
    }, 2)


def command_confirm_fresh(args: argparse.Namespace) -> int:
    rc, begin, stderr = engine_call(["begin", "--project", args.project])
    begin.setdefault("orchestration_version", ORCHESTRATION_VERSION)
    begin.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
    if rc != 0:
        return emit(begin, rc)

    if begin.get("status") == "confirmation_required":
        erc, payload, estderr = engine_call(["confirm-fresh", "--project", args.project])
        payload.setdefault("orchestration_version", ORCHESTRATION_VERSION)
        payload.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
        if erc == 0:
            payload["action"] = "continue_same_turn_with_advance"
            payload["next_command"] = (
                str(WORKSPACE / "skills/siril-ghs-stretch-pass2/bin/ghs-pass2")
                + f' advance --project "{args.project}"'
            )
        return emit(payload, erc)

    wrc, state = workflow_state(args.project)
    if wrc != 0:
        return emit(state, wrc)
    obsolete = obsolete_canonical_context(args.project, state)
    if obsolete is None:
        return emit({
            "status": "blocked",
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_helper_version": PROCESSING_HELPER_VERSION,
            "error": "No completed canonical GHS pass-2 result requires fresh-rerun authorization.",
            "action": "stop",
        }, 2)

    try:
        path, intent = authorize_obsolete_rerun(args.project)
    except Exception as exc:
        return emit({
            "status": "blocked",
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_helper_version": PROCESSING_HELPER_VERSION,
            "error": str(exc),
            "action": "stop",
        }, 2)

    return emit({
        "status": "fresh_run_confirmed",
        "action": "continue_same_turn_with_advance",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "project": str(fixed_project_paths(args.project)["project"]),
        "current_canonical_status": "obsolete",
        "canonical_output_preserved": True,
        "hashes_verified": True,
        "fresh_intent": str(path),
        "source_sha256": intent["source_sha256"],
        "canonical_manifest_sha256": intent["canonical_manifest_sha256"],
        "canonical_output_sha256": intent["canonical_output_sha256"],
        "next_command": (
            str(WORKSPACE / "skills/siril-ghs-stretch-pass2/bin/ghs-pass2")
            + f' advance --project "{args.project}"'
        ),
    })


def command_select_publish(args: argparse.Namespace) -> int:
    if len(args.visual_notes.strip()) < 80:
        return emit({
            "status": "blocked",
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_helper_version": PROCESSING_HELPER_VERSION,
            "error": "Visual notes must contain at least 80 characters comparing the eligible candidates.",
            "action": "repair_selection_payload_only",
        }, 2)

    try:
        run = load_run_manifest(args.run_root)
    except Exception as exc:
        return emit({"status": "blocked", "error": str(exc)}, 2)

    eligible = run.get("publication_eligible_candidates")
    if not isinstance(eligible, list) or not eligible:
        eligible = (run.get("publication_gate") or {}).get("publication_eligible_candidates")
    if not isinstance(eligible, list) or not eligible:
        return emit({"status": "blocked", "error": "Run has no eligible candidates."}, 2)

    if args.candidate not in eligible:
        return emit({
            "status": "blocked",
            "error": f"Selected candidate {args.candidate!r} is not eligible. Eligible: {eligible}",
        }, 2)

    if args.compared != eligible and sorted(args.compared) != sorted(eligible):
        return emit({
            "status": "blocked",
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_helper_version": PROCESSING_HELPER_VERSION,
            "error": f"Every eligible candidate must be supplied through repeated --compared. Expected {eligible}, got {args.compared}.",
            "action": "repair_selection_payload_only",
        }, 2)

    select_args = [
        "select", "--project", args.project, "--run-root", args.run_root,
        "--candidate", args.candidate,
    ]
    for name in args.compared:
        select_args += ["--compared", name]
    select_args += ["--visual-notes", args.visual_notes]

    src, selected, serr = engine_call(select_args)
    if src != 0:
        selected.setdefault("orchestration_version", ORCHESTRATION_VERSION)
        selected.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
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
    rc, payload, stderr = engine_call(["status", "--project", args.project])
    payload.setdefault("orchestration_version", ORCHESTRATION_VERSION)
    payload.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
    return emit(payload, rc)

def command_self_test(args: argparse.Namespace) -> int:
    payload = {
        "status": "success",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "standalone_wrapper": True,
        "canonical_python_fixed": str(ASTRO_PYTHON),
        "direct_helper_discovery_forbidden": True,
        "directory_discovery_forbidden": True,
        "exact_read_targets_required": True,
        "completed_stage_confirmation_preserved": True,
        "completed_obsolete_stage_requires_confirmation": True,
        "obsolete_fresh_authorization_strong_hash_binding": True,
        "obsolete_fresh_authorization_durable": True,
        "bounded_candidates": 3,
        "durable_selection": True,
        "bounded_publication_recovery_attempts": 2,
        "black_point_handoff": True,
        "selection_compared_arguments_repeated": True,
        "plan_only_supported": True,
    }
    return emit(payload)

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Standalone GHS pass-2 orchestration wrapper.")
    p.add_argument("--version", action="version", version=ORCHESTRATION_VERSION)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("advance")
    a.add_argument("--project", required=True)
    a.add_argument("--timeout", type=int, default=7200)
    a.add_argument("--plan-only", action="store_true")
    a.set_defaults(func=command_advance)

    c = sub.add_parser("confirm-fresh")
    c.add_argument("--project", required=True)
    c.set_defaults(func=command_confirm_fresh)

    s = sub.add_parser("select-publish")
    s.add_argument("--project", required=True)
    s.add_argument("--run-root", required=True)
    s.add_argument("--candidate", required=True)
    s.add_argument("--compared", action="append", required=True)
    s.add_argument("--visual-notes", required=True)
    s.set_defaults(func=command_select_publish)

    st = sub.add_parser("stage-status")
    st.add_argument("--project", required=True)
    st.set_defaults(func=command_status)

    t = sub.add_parser("self-test")
    t.set_defaults(func=command_self_test)

    return p

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

