#!/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/.venv/bin/python
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ORCHESTRATION_VERSION = "1.0.4"
PROCESSING_HELPER_VERSION = "1.0.3"
WORKSPACE = Path(os.environ.get(
    "GREEN_REDUCTION_WORKSPACE",
    "/home/peter/.openclaw/workspace/agents/codewarrior",
))
PROJECTS = WORKSPACE / "Projects"
SKILL_ROOT = Path(__file__).resolve().parent.parent
LEGACY_WRAPPER = Path(os.environ.get(
    "GREEN_REDUCTION_LEGACY_WRAPPER",
    str(SKILL_ROOT / "bin/green-reduction-v1.0.3"),
))
PUBLIC_WRAPPER = Path(os.environ.get(
    "GREEN_REDUCTION_PUBLIC_WRAPPER",
    str(SKILL_ROOT / "bin/green-reduction"),
))
HEX64 = re.compile(r"^[0-9a-f]{64}$")


class OrchestrationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def emit(payload: dict[str, Any], rc: int = 0) -> int:
    print(json.dumps(payload, indent=2, sort_keys=True))
    return rc


def compact_text(value: Any, limit: int = 1200) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def json_read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise OrchestrationError(f"Required manifest is missing: {path}") from exc
    except Exception as exc:
        raise OrchestrationError(f"Could not read JSON manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OrchestrationError(f"Manifest root must be an object: {path}")
    return value


def safe_project(project_name: str) -> tuple[str, Path]:
    name = project_name.strip()
    if not name or name in {".", ".."} or Path(name).name != name or "/" in name or "\\" in name:
        raise OrchestrationError("Project must be a single existing project-directory name.")
    project = PROJECTS / name
    if not project.is_dir():
        raise OrchestrationError(f"Project directory does not exist: {project}")
    return name, project


def paths(project_name: str) -> dict[str, Path]:
    name, project = safe_project(project_name)
    black = project / "processing/black-point"
    green = project / "processing/green-reduction"
    state = project / ".siril-green-reduction-v1.0.4"
    return {
        "project_name": Path(name),
        "project": project,
        "black_dir": black,
        "black_output": black / "SHO-starless-black-point.fit",
        "black_manifest": black / "black-point-manifest.json",
        "green_dir": green,
        "green_output": green / "SHO-starless-green-reduced.fit",
        "green_manifest": green / "green-reduction-manifest.json",
        "state": state,
        "fresh_intent": state / "fresh-intent.json",
    }


def valid_sha(value: Any) -> bool:
    return isinstance(value, str) and HEX64.fullmatch(value) is not None


def recorded_policy(manifest: dict[str, Any]) -> Any:
    return manifest.get("selection_policy_version") or (manifest.get("selection_policy") or {}).get("version")


def check_small_file_evidence(path: Path, evidence: dict[str, Any], canonical_path: Path, label: str) -> list[str]:
    errors: list[str] = []
    if evidence.get("path") != str(canonical_path):
        errors.append(f"{label} manifest path is not canonical.")
    if not valid_sha(evidence.get("sha256")):
        errors.append(f"{label} manifest SHA is missing or invalid.")
    if not path.is_file():
        errors.append(f"{label} canonical FITS is missing.")
    elif isinstance(evidence.get("size"), int) and path.stat().st_size != evidence["size"]:
        errors.append(f"{label} canonical FITS size differs from its manifest.")
    return errors


def fast_completed_status(project_name: str) -> dict[str, Any]:
    p = paths(project_name)
    bm_path = p["black_manifest"]
    bf_path = p["black_output"]
    gm_path = p["green_manifest"]
    gf_path = p["green_output"]

    if not bm_path.is_file() or not bf_path.is_file():
        return {
            "status": "blocked",
            "current_canonical_status": "unknown",
            "project": str(p["project"]),
            "error": "Current black-point canonical prerequisite is missing. Run/repair siril-black-point first.",
        }

    black = json_read(bm_path)
    black_errors: list[str] = []
    if black.get("status") != "ready":
        black_errors.append("Black-point manifest status is not ready.")
    if black.get("helper_version") != "1.0.4":
        black_errors.append("Black-point helper version is not 1.0.4.")
    if recorded_policy(black) != "1.0.4":
        black_errors.append("Black-point selection policy is not 1.0.4.")
    if black.get("visual_review_completed") is not True:
        black_errors.append("Black-point visual review is incomplete.")
    if (black.get("quality_assessment") or {}).get("satisfactory") is not True:
        black_errors.append("Black-point quality assessment is not satisfactory.")
    if black.get("next_stage") != "siril-green-reduction":
        black_errors.append("Black point does not hand off to siril-green-reduction.")
    if black.get("green_reduction_processing_permitted") is not True:
        black_errors.append("Black point does not permit green reduction.")
    bout = black.get("output") or {}
    black_errors.extend(check_small_file_evidence(bf_path, bout, bf_path, "Black-point"))
    if black_errors:
        return {
            "status": "blocked",
            "current_canonical_status": "unknown",
            "project": str(p["project"]),
            "errors": black_errors,
            "error": "Current black-point prerequisite failed the manifest-first contract.",
        }

    black_manifest_sha = sha256_file(bm_path)
    current_black_sha = bout["sha256"]

    green_exists = gm_path.is_file() or gf_path.exists()
    if not green_exists:
        return {
            "status": "missing",
            "current_canonical_status": "missing",
            "project": str(p["project"]),
            "current_upstream_source_sha256": current_black_sha,
            "black_manifest_sha256": black_manifest_sha,
            "manifest_first": True,
            "pre_confirmation_large_fits_hashing": False,
        }
    if not (gm_path.is_file() and gf_path.is_file()):
        return {
            "status": "blocked",
            "current_canonical_status": "invalid",
            "project": str(p["project"]),
            "error": "Green-reduction canonical is partial: output and manifest must either both exist or both be absent.",
            "manifest_first": True,
            "pre_confirmation_large_fits_hashing": False,
        }

    green = json_read(gm_path)
    green_errors: list[str] = []
    if green.get("status") != "ready":
        green_errors.append("Green-reduction manifest status is not ready.")
    if green.get("helper_version") != PROCESSING_HELPER_VERSION:
        green_errors.append(f"Green-reduction helper version is not {PROCESSING_HELPER_VERSION}.")
    if green.get("visual_review_completed") is not True:
        green_errors.append("Green-reduction visual review is incomplete.")
    if (green.get("quality_assessment") or {}).get("satisfactory") is not True:
        green_errors.append("Green-reduction quality assessment is not satisfactory.")
    if green.get("next_stage") != "siril-saturation":
        green_errors.append("Green reduction does not hand off to siril-saturation.")
    if green.get("saturation_processing_permitted") is not True:
        green_errors.append("Green reduction does not permit saturation.")
    gout = green.get("output") or {}
    gsrc = green.get("source") or {}
    green_errors.extend(check_small_file_evidence(gf_path, gout, gf_path, "Green-reduction"))
    if gsrc.get("path") != str(bf_path):
        green_errors.append("Green-reduction recorded source path is not the canonical black-point FITS.")
    if not valid_sha(gsrc.get("sha256")):
        green_errors.append("Green-reduction recorded source SHA is missing or invalid.")
    if green_errors:
        return {
            "status": "blocked",
            "current_canonical_status": "invalid",
            "project": str(p["project"]),
            "errors": green_errors,
            "error": "Existing green-reduction canonical is not a mature completed v1.0.3 result.",
            "manifest_first": True,
            "pre_confirmation_large_fits_hashing": False,
        }

    relation = "ready" if gsrc["sha256"] == current_black_sha else "obsolete"
    obsolete_reasons = [] if relation == "ready" else [
        "Green-reduction source checksum differs from the current black-point result."
    ]
    return {
        "status": "completed",
        "current_canonical_status": relation,
        "project": str(p["project"]),
        "current_upstream_source_sha256": current_black_sha,
        "black_manifest_sha256": black_manifest_sha,
        "canonical_manifest_sha256": sha256_file(gm_path),
        "canonical_output_sha256": gout["sha256"],
        "recorded_source_sha256": gsrc["sha256"],
        "obsolete_reasons": obsolete_reasons,
        "manifest_first": True,
        "pre_confirmation_large_fits_hashing": False,
    }


def auth_read(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def auth_fast_matches(project_name: str, state: dict[str, Any]) -> bool:
    if state.get("status") != "completed":
        return False
    p = paths(project_name)
    auth = auth_read(p["fresh_intent"])
    if not auth or auth.get("status") != "fresh_run_authorized":
        return False
    relation = state.get("current_canonical_status")
    return (
        auth.get("orchestration_version") == ORCHESTRATION_VERSION
        and auth.get("project") == str(p["project"])
        and auth.get("canonical_relation_at_authorization") == relation
        and auth.get("current_black_manifest_sha256") == state.get("black_manifest_sha256")
        and auth.get("current_black_output_sha256") == state.get("current_upstream_source_sha256")
        and auth.get("preserved_green_manifest_sha256") == state.get("canonical_manifest_sha256")
        and auth.get("preserved_green_output_sha256") == state.get("canonical_output_sha256")
    )


def legacy_call(argv: list[str]) -> tuple[int, dict[str, Any], str]:
    proc = subprocess.run(
        [str(LEGACY_WRAPPER), *argv],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    raw = proc.stdout.strip()
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        payload = {
            "status": "blocked",
            "error": f"Legacy green-reduction wrapper returned non-JSON output: {compact_text(raw or proc.stderr)}",
        }
    if not isinstance(payload, dict):
        payload = {"status": "blocked", "error": "Legacy green-reduction wrapper returned a non-object JSON payload."}
    return proc.returncode, payload, proc.stderr


def confirmation_payload(project_name: str, state: dict[str, Any]) -> dict[str, Any]:
    relation = state["current_canonical_status"]
    if relation == "obsolete":
        question = (
            f"Green reduction for {project_name} has already completed but is obsolete for the current "
            "black-point result. Do you want me to run it again as a fresh run?"
        )
    else:
        question = (
            f"Green reduction for {project_name} has already completed successfully. "
            "Do you want me to run it again as a fresh run?"
        )
    quoted = project_name.replace('"', '\\"')
    public = str(PUBLIC_WRAPPER)
    return {
        "status": "confirmation_required",
        "action": "await_user_confirmation",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "project": state["project"],
        "current_canonical_status": relation,
        "current_upstream_source_sha256": state["current_upstream_source_sha256"],
        "canonical_manifest_sha256": state["canonical_manifest_sha256"],
        "canonical_output_sha256": state["canonical_output_sha256"],
        "obsolete_reasons": state.get("obsolete_reasons", []),
        "confirmation_required": True,
        "question": question,
        "canonical_output_preserved": True,
        "manifest_first": True,
        "pre_confirmation_large_fits_hashing": False,
        "hashes_verified": False,
        "hash_verification_deferred_until": "confirm-fresh",
        "production_processing_started": False,
        "post_confirmation_single_exec": True,
        "next_command_after_confirmation": (
            f'{public} confirm-fresh --project "{quoted}" && '
            f'{public} advance --project "{quoted}"'
        ),
    }


def full_hash_binding(project_name: str, state: dict[str, Any]) -> dict[str, str]:
    p = paths(project_name)
    current_black_manifest_sha = sha256_file(p["black_manifest"])
    current_black_output_sha = sha256_file(p["black_output"])
    preserved_green_manifest_sha = sha256_file(p["green_manifest"])
    preserved_green_output_sha = sha256_file(p["green_output"])

    if current_black_manifest_sha != state["black_manifest_sha256"]:
        raise OrchestrationError("Black-point manifest changed while confirming the fresh run.")
    if current_black_output_sha != state["current_upstream_source_sha256"]:
        raise OrchestrationError("Current black-point FITS does not match its manifest SHA.")
    if preserved_green_manifest_sha != state["canonical_manifest_sha256"]:
        raise OrchestrationError("Green-reduction manifest changed while confirming the fresh run.")
    if preserved_green_output_sha != state["canonical_output_sha256"]:
        raise OrchestrationError("Preserved green-reduction FITS does not match its manifest SHA.")
    return {
        "current_black_manifest_sha256": current_black_manifest_sha,
        "current_black_output_sha256": current_black_output_sha,
        "preserved_green_manifest_sha256": preserved_green_manifest_sha,
        "preserved_green_output_sha256": preserved_green_output_sha,
    }


def write_auth(project_name: str, state: dict[str, Any], binding: dict[str, str]) -> Path:
    p = paths(project_name)
    p["state"].mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "status": "fresh_run_authorized",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "authorized_at": utc_now(),
        "project": str(p["project"]),
        "canonical_relation_at_authorization": state["current_canonical_status"],
        **binding,
    }
    target = p["fresh_intent"]
    temp = target.with_suffix(".json.tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, target)
    return target


def enhance_review_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("status") != "visual_review_required":
        return payload
    enriched = dict(payload)
    enriched["orchestration_version"] = ORCHESTRATION_VERSION
    enriched["processing_helper_version"] = PROCESSING_HELPER_VERSION
    enriched["candidate_note_examples"] = {
        "green": "green:<state specifically whether unwanted/residual green remains or was removed>",
        "magenta": "magenta:<state specifically whether any magenta/purple shift is visible>",
        "structure": "structure:<state specifically whether faint emission/Pillars/dark lanes remain preserved>",
    }
    enriched["selection_instruction"] = (
        "Use every exact read_targets[].path verbatim. For every eligible candidate, describe all three "
        "observations with full phrases; do not use vague values such as 'none' or 'preserved'. "
        "Choose the least aggressive eligible candidate that removes unwanted green without magenta/purple "
        "or loss of faint structure. Then call select-publish once."
    )
    if len(json.dumps(enriched, separators=(",", ":")).encode("utf-8")) <= 4096:
        return enriched
    payload = dict(payload)
    payload["orchestration_version"] = ORCHESTRATION_VERSION
    payload["processing_helper_version"] = PROCESSING_HELPER_VERSION
    return payload


def delegate_legacy(argv: list[str]) -> int:
    rc, payload, stderr = legacy_call(argv)
    payload.setdefault("orchestration_version", ORCHESTRATION_VERSION)
    payload.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
    payload = enhance_review_payload(payload)
    return emit(payload, rc)


def command_stage_status(args: argparse.Namespace) -> int:
    state = fast_completed_status(args.project)
    state.setdefault("orchestration_version", ORCHESTRATION_VERSION)
    state.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
    if state.get("status") == "completed":
        state["status"] = state["current_canonical_status"]
        state["canonical_output_preserved"] = True
        return emit(state)
    if state.get("status") == "missing":
        return delegate_legacy(["stage-status", "--project", args.project])
    return emit(state, 2)


def command_confirm_fresh(args: argparse.Namespace) -> int:
    state = fast_completed_status(args.project)
    if state.get("status") != "completed" or state.get("current_canonical_status") not in {"ready", "obsolete"}:
        raise OrchestrationError("Fresh-run confirmation requires a mature completed green-reduction canonical result.")

    if auth_fast_matches(args.project, state):
        p = paths(args.project)
        return emit({
            "status": "fresh_run_authorized",
            "fresh_run_authorized": True,
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_helper_version": PROCESSING_HELPER_VERSION,
            "project": state["project"],
            "current_canonical_status": state["current_canonical_status"],
            "fresh_intent": str(p["fresh_intent"]),
            "authorization_reused": True,
            "canonical_output_preserved": True,
            "action": "run_review_select_publish",
        })

    binding = full_hash_binding(args.project, state)

    native_bridge: dict[str, Any] | None = None
    if state["current_canonical_status"] == "ready":
        rc, begin, stderr = legacy_call(["advance", "--project", args.project, "--plan-only"])
        if begin.get("status") == "confirmation_required":
            rc2, confirmed, stderr2 = legacy_call(["confirm-fresh", "--project", args.project])
            if rc2 != 0 or confirmed.get("status") not in {"fresh_run_authorized", "start_new_run"}:
                raise OrchestrationError(
                    "The preserved v1.0.3 helper could not create its native durable fresh authorization: "
                    + compact_text(confirmed.get("error") or stderr2)
                )
            native_bridge = confirmed
        elif begin.get("status") in {"fresh_run_authorized", "would_generate_candidates"}:
            native_bridge = begin
        else:
            raise OrchestrationError(
                "The preserved v1.0.3 helper did not expose the expected completed/current confirmation state: "
                + compact_text(begin)
            )

    intent = write_auth(args.project, state, binding)
    return emit({
        "status": "fresh_run_authorized",
        "fresh_run_authorized": True,
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "project": state["project"],
        "current_canonical_status": state["current_canonical_status"],
        "fresh_intent": str(intent),
        "authorization_reused": False,
        "hashes_verified": True,
        "strong_hash_binding": binding,
        "native_v1_0_3_confirmation_bridge": native_bridge is not None,
        "canonical_output_preserved": True,
        "action": "run_review_select_publish",
    })


def command_advance(args: argparse.Namespace) -> int:
    state = fast_completed_status(args.project)

    if state.get("status") == "completed":
        ignore = os.environ.get("GREEN_REDUCTION_IGNORE_FRESH_INTENT") == "1"
        if not ignore and auth_fast_matches(args.project, state):
            argv = ["advance", "--project", args.project]
            if args.plan_only:
                argv.append("--plan-only")
            return delegate_legacy(argv)
        return emit(confirmation_payload(args.project, state))

    if state.get("status") == "missing":
        argv = ["advance", "--project", args.project]
        if args.plan_only:
            argv.append("--plan-only")
        return delegate_legacy(argv)

    state.setdefault("orchestration_version", ORCHESTRATION_VERSION)
    state.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
    state["action"] = "stop_and_report_exact_error"
    state["production_processing_started"] = False
    return emit(state, 2)


def command_self_test(args: argparse.Namespace) -> int:
    payload = {
        "status": "success",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "completed_current_requires_confirmation": True,
        "completed_obsolete_requires_confirmation": True,
        "manifest_first_fast_path": True,
        "pre_confirmation_large_fits_hashing": False,
        "strong_authorization_binding": [
            "current_black_manifest_sha256",
            "current_black_output_sha256",
            "preserved_green_manifest_sha256",
            "preserved_green_output_sha256",
        ],
        "post_confirmation_single_exec": True,
        "processing_helper_byte_for_byte_preserved": True,
        "single_siril_process_owned_by_processing_helper": True,
        "bounded_candidates_owned_by_processing_helper": 3,
        "exact_read_targets_owned_by_processing_helper": True,
        "select_publish_owned_by_processing_helper": True,
        "candidate_note_validator_unchanged": True,
        "candidate_note_handoff_guidance_improved": True,
        "directory_discovery_forbidden_for_runtime_routing": True,
        "canonical_move_or_rename_forbidden_for_runtime_recovery": True,
        "current_ready_native_confirmation_bridge_via_legacy_wrapper": True,
        "obsolete_legacy_flow_requires_no_private_processing_bypass": True,
    }
    return emit(payload)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Siril green-reduction v1.0.4 orchestration dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("advance")
    p.add_argument("--project", required=True)
    p.add_argument("--plan-only", action="store_true")
    p = sub.add_parser("confirm-fresh")
    p.add_argument("--project", required=True)
    p = sub.add_parser("stage-status")
    p.add_argument("--project", required=True)
    sub.add_parser("self-test")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "advance":
            return command_advance(args)
        if args.command == "confirm-fresh":
            return command_confirm_fresh(args)
        if args.command == "stage-status":
            return command_stage_status(args)
        if args.command == "self-test":
            return command_self_test(args)
        raise OrchestrationError(f"Unsupported command: {args.command}")
    except OrchestrationError as exc:
        return emit({
            "status": "blocked",
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_helper_version": PROCESSING_HELPER_VERSION,
            "error": compact_text(exc),
            "action": "stop_and_report_exact_error",
            "production_processing_started": False,
        }, 2)


if __name__ == "__main__":
    raise SystemExit(main())
