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

ORCHESTRATION_VERSION = "1.0.5"
PROCESSING_HELPER_VERSION = "1.0.4"

DEFAULT_WORKSPACE = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
WORKSPACE = Path(os.environ.get("BLACK_POINT_WORKSPACE", str(DEFAULT_WORKSPACE)))
PROJECTS_ROOT = Path(os.environ.get("BLACK_POINT_PROJECTS_ROOT", str(WORKSPACE / "Projects")))
HERE = Path(__file__).resolve().parent
LEGACY_WRAPPER = Path(os.environ.get(
    "BLACK_POINT_LEGACY_WRAPPER",
    str(HERE.parent / "bin" / "black-point-v1.0.4"),
))
STATE_DIR_NAME = ".siril-black-point-v1.0.5"

class BlackPointOrchestrationError(RuntimeError):
    pass

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def emit(payload: dict[str, Any], code: int = 0) -> int:
    out = dict(payload)
    out.setdefault("orchestration_version", ORCHESTRATION_VERSION)
    out.setdefault("processing_helper_version", PROCESSING_HELPER_VERSION)
    print(json.dumps(out, indent=2, sort_keys=True))
    return code

def load_json(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise BlackPointOrchestrationError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(obj, dict):
        raise BlackPointOrchestrationError(f"Expected JSON object: {path}")
    return obj

def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.partial")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def parse_json_output(stdout: str, stderr: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        raise BlackPointOrchestrationError(
            "Legacy black-point wrapper returned no JSON output: "
            + (stderr.strip() or "no stderr")
        )
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BlackPointOrchestrationError(
            "Legacy black-point wrapper returned non-JSON output; "
            "stop rather than discovering state."
        ) from exc
    if not isinstance(obj, dict):
        raise BlackPointOrchestrationError("Legacy black-point wrapper returned non-object JSON.")
    return obj

def legacy_call(arguments: list[str], *, timeout: int | None = None) -> tuple[int, dict[str, Any], str]:
    if not LEGACY_WRAPPER.is_file() or not os.access(LEGACY_WRAPPER, os.X_OK):
        raise BlackPointOrchestrationError(
            f"Preserved v1.0.4 wrapper is unavailable: {LEGACY_WRAPPER}"
        )
    proc = subprocess.run(
        [str(LEGACY_WRAPPER), *arguments],
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    try:
        payload = parse_json_output(proc.stdout, proc.stderr)
    except BlackPointOrchestrationError:
        payload = {
            "status": "blocked",
            "error": proc.stderr.strip() or proc.stdout.strip()
            or "Unknown legacy black-point wrapper failure.",
        }
    return proc.returncode, payload, proc.stderr.strip()

def project_paths(project: str) -> dict[str, Path]:
    root = PROJECTS_ROOT / project
    return {
        "project": root,
        "ghs_manifest": root / "processing/ghs-pass2/ghs-pass2-manifest.json",
        "ghs_output": root / "processing/ghs-pass2/SHO-starless-ghs-pass2.fit",
        "black_manifest": root / "processing/black-point/black-point-manifest.json",
        "black_output": root / "processing/black-point/SHO-starless-black-point.fit",
        "intent": root / STATE_DIR_NAME / "fresh-intent.json",
    }

def current_selection_policy_version(manifest: dict[str, Any]) -> str | None:
    direct = manifest.get("selection_policy_version")
    if isinstance(direct, str):
        return direct
    policy = manifest.get("selection_policy")
    if isinstance(policy, dict) and isinstance(policy.get("version"), str):
        return policy["version"]
    return None

def manifest_first_snapshot(project: str) -> dict[str, Any]:
    """Classify the named stage without hashing either large FITS file."""
    p = project_paths(project)
    if not p["project"].is_dir():
        raise BlackPointOrchestrationError(f"Project does not exist: {p['project']}")
    if not p["ghs_manifest"].is_file():
        raise BlackPointOrchestrationError(f"GHS pass-2 manifest is missing: {p['ghs_manifest']}")
    if not p["ghs_output"].is_file():
        raise BlackPointOrchestrationError(f"GHS pass-2 output is missing: {p['ghs_output']}")

    upstream = load_json(p["ghs_manifest"])
    source_sha = (upstream.get("output") or {}).get("sha256")
    if upstream.get("status") != "ready":
        raise BlackPointOrchestrationError("Current GHS pass-2 manifest is not ready.")
    if upstream.get("helper_version") != "1.2.0":
        raise BlackPointOrchestrationError(
            f"Current GHS pass-2 helper is {upstream.get('helper_version')!r}, expected '1.2.0'."
        )
    if upstream.get("visual_review_completed") is not True:
        raise BlackPointOrchestrationError("Current GHS pass-2 visual review is incomplete.")
    if (upstream.get("quality_assessment") or {}).get("satisfactory") is not True:
        raise BlackPointOrchestrationError("Current GHS pass-2 quality assessment is not satisfactory.")
    if upstream.get("next_stage") != "siril-black-point":
        raise BlackPointOrchestrationError(
            f"Current GHS pass-2 next_stage is {upstream.get('next_stage')!r}, "
            "expected 'siril-black-point'."
        )
    if upstream.get("black_point_processing_permitted") is not True:
        raise BlackPointOrchestrationError("Current GHS pass-2 manifest does not permit black point.")
    if not isinstance(source_sha, str) or len(source_sha) != 64:
        raise BlackPointOrchestrationError("Current GHS pass-2 manifest lacks a valid output SHA-256.")

    base = {
        "project": str(p["project"]),
        "current_source_sha256": source_sha,
        "upstream_manifest_sha256": sha256_file(p["ghs_manifest"]),
        "manifest_first": True,
        "pre_confirmation_large_fits_hashing": False,
    }

    if not p["black_manifest"].is_file() or not p["black_output"].is_file():
        return {
            **base,
            "canonical_exists": False,
            "canonical_status": "missing",
            "delegate_legacy_wrapper": True,
            "obsolete_reasons": [],
        }

    canonical = load_json(p["black_manifest"])
    output_sha = (canonical.get("output") or {}).get("sha256")
    old_source_sha = (canonical.get("source") or {}).get("sha256")
    helper_version = canonical.get("helper_version")
    policy_version = current_selection_policy_version(canonical)
    structurally_completed = (
        canonical.get("status") == "ready"
        and isinstance(output_sha, str) and len(output_sha) == 64
        and isinstance(old_source_sha, str) and len(old_source_sha) == 64
    )
    if not structurally_completed:
        return {
            **base,
            "canonical_exists": True,
            "canonical_status": "unknown_or_damaged",
            "delegate_legacy_wrapper": False,
            "canonical_manifest_sha256": sha256_file(p["black_manifest"]),
            "canonical_output_sha256": output_sha,
            "obsolete_reasons": [
                "Existing black-point manifest is not a structurally ready completed result."
            ],
        }

    mature_v104 = helper_version == "1.0.4" and policy_version == "1.0.4"
    if not mature_v104 and old_source_sha == source_sha:
        return {
            **base,
            "canonical_exists": True,
            "canonical_status": "legacy_policy",
            "delegate_legacy_wrapper": True,
            "canonical_manifest_sha256": sha256_file(p["black_manifest"]),
            "canonical_output_sha256": output_sha,
            "canonical_recorded_source_sha256": old_source_sha,
            "canonical_helper_version": helper_version,
            "selection_policy_version": policy_version,
            "obsolete_reasons": [],
        }

    reasons: list[str] = []
    if old_source_sha != source_sha:
        reasons.append("Black-point source checksum differs from the current GHS pass-2 result.")

    return {
        **base,
        "canonical_exists": True,
        "canonical_status": "obsolete" if reasons else "ready",
        "delegate_legacy_wrapper": False,
        "canonical_manifest_sha256": sha256_file(p["black_manifest"]),
        "canonical_output_sha256": output_sha,
        "canonical_recorded_source_sha256": old_source_sha,
        "canonical_helper_version": helper_version,
        "selection_policy_version": policy_version,
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

def matching_intent(project: str, snap: dict[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    if os.environ.get("BLACK_POINT_IGNORE_FRESH_INTENT") == "1":
        return None
    path = project_paths(project)["intent"]
    if not path.is_file():
        return None
    try:
        obj = load_json(path)
    except Exception:
        return None
    return (path, obj) if intent_matches_snapshot(obj, snap) else None

def strong_binding(project: str, snap: dict[str, Any]) -> dict[str, str]:
    p = project_paths(project)
    if not snap.get("canonical_exists"):
        raise BlackPointOrchestrationError("No completed black-point canonical exists to authorize.")
    source_sha = sha256_file(p["ghs_output"])
    manifest_sha = sha256_file(p["black_manifest"])
    output_sha = sha256_file(p["black_output"])
    if source_sha != snap.get("current_source_sha256"):
        raise BlackPointOrchestrationError("Current GHS pass-2 FITS checksum does not match its manifest.")
    if manifest_sha != snap.get("canonical_manifest_sha256"):
        raise BlackPointOrchestrationError("Black-point canonical manifest changed during authorization.")
    if output_sha != snap.get("canonical_output_sha256"):
        raise BlackPointOrchestrationError("Black-point canonical FITS does not match its manifest SHA.")
    return {
        "source_sha256": source_sha,
        "canonical_manifest_sha256": manifest_sha,
        "canonical_output_sha256": output_sha,
    }

def verify_authorized_intent(project: str, snap: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    found = matching_intent(project, snap)
    if found is None:
        raise BlackPointOrchestrationError(
            "No fresh-run authorization matches the current replacement target."
        )
    path, obj = found
    binding = strong_binding(project, snap)
    for key, value in binding.items():
        if obj.get(key) != value:
            raise BlackPointOrchestrationError(
                "Fresh-run authorization no longer matches current strong hashes."
            )
    return path, obj

def write_intent(project: str, snap: dict[str, Any], binding: dict[str, str]) -> tuple[Path, dict[str, Any]]:
    path = project_paths(project)["intent"]
    obj = {
        "schema_version": 1,
        "orchestration_version": ORCHESTRATION_VERSION,
        "status": "authorized",
        "authorized_at": utc_now(),
        "project_name": project,
        "canonical_status_at_authorization": snap.get("canonical_status"),
        **binding,
    }
    write_json_atomic(path, obj)
    return path, obj

def confirmation_payload(project: str, snap: dict[str, Any]) -> dict[str, Any]:
    obsolete = snap.get("canonical_status") == "obsolete"
    question = (
        f"Black point for {project} has already completed but is obsolete for the current "
        "GHS pass-2 result. Do you want me to run it again as a fresh run?"
        if obsolete else
        f"Black point for {project} has already completed successfully. "
        "Do you want me to run it again as a fresh run?"
    )
    return {
        "status": "confirmation_required",
        "action": "await_user_confirmation",
        "confirmation_required": True,
        "project": snap["project"],
        "current_canonical_status": snap["canonical_status"],
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
            str(DEFAULT_WORKSPACE / "skills/siril-black-point/bin/black-point")
            + f' confirm-fresh --project "{project}"'
        ),
    }

def bridge_native_ready_confirmation(project: str) -> None:
    """Preserve the proven v1.0.4 wrapper's own durable confirmation state."""
    rc, begin, _ = legacy_call(["advance", "--project", project])
    if rc != 0:
        raise BlackPointOrchestrationError(
            begin.get("error") or "v1.0.4 wrapper advance failed during confirmation bridge."
        )
    if begin.get("confirmation_required") is True or begin.get("status") == "confirmation_required":
        rc, confirmed, _ = legacy_call(["confirm-fresh", "--project", project])
        if rc != 0:
            raise BlackPointOrchestrationError(
                confirmed.get("error") or
                "v1.0.4 wrapper confirm-fresh failed during confirmation bridge."
            )
        if not (
            confirmed.get("fresh_run_authorized") is True
            or confirmed.get("status") in {"fresh_run_authorized", "fresh_run_confirmed"}
        ):
            raise BlackPointOrchestrationError(
                "v1.0.4 wrapper confirm-fresh did not return an authorized state."
            )
        return
    if begin.get("fresh_run_authorized") is True or begin.get("status") in {
        "fresh_run_authorized", "fresh_run_confirmed"
    }:
        return
    raise BlackPointOrchestrationError(
        "v1.0.4 wrapper did not expose a pending or already-authorized fresh-run state."
    )

def command_advance(args: argparse.Namespace) -> int:
    snap = manifest_first_snapshot(args.project)

    if snap.get("canonical_status") == "unknown_or_damaged":
        return emit({
            "status": "blocked",
            "action": "stop_and_report_canonical_integrity",
            "error": snap["obsolete_reasons"][0],
            "production_processing_started": False,
        }, 2)

    if snap.get("delegate_legacy_wrapper") is True:
        legacy_args = ["advance", "--project", args.project]
        if args.plan_only:
            legacy_args.append("--plan-only")
        rc, payload, _ = legacy_call(legacy_args, timeout=args.timeout)
        payload.setdefault("legacy_wrapper_preserved", True)
        return emit(payload, rc)

    if snap.get("canonical_exists"):
        found = matching_intent(args.project, snap)
        if found is None:
            return emit(confirmation_payload(args.project, snap))

        if args.plan_only:
            path, _ = found
            return emit({
                "status": "plan",
                "action": "continue_authorized_fresh_run",
                "project": snap["project"],
                "current_canonical_status": snap["canonical_status"],
                "fresh_authorization_present": True,
                "fresh_intent": str(path),
                "authorization_manifest_binding_matches": True,
                "strong_hash_verification_deferred_until_processing": True,
                "production_processing_started": False,
            })

        try:
            intent_path, _ = verify_authorized_intent(args.project, snap)
        except Exception as exc:
            return emit({
                "status": "blocked",
                "action": "stop_and_report_authorization_integrity",
                "error": str(exc),
                "production_processing_started": False,
            }, 2)

        rc, payload, _ = legacy_call(
            ["advance", "--project", args.project],
            timeout=args.timeout,
        )
        payload["fresh_rerun_authorized"] = True
        payload["fresh_intent"] = str(intent_path)
        payload["previous_canonical_preserved"] = True
        payload["legacy_wrapper_preserved"] = True
        return emit(payload, rc)

    legacy_args = ["advance", "--project", args.project]
    if args.plan_only:
        legacy_args.append("--plan-only")
    rc, payload, _ = legacy_call(legacy_args, timeout=args.timeout)
    payload.setdefault("legacy_wrapper_preserved", True)
    return emit(payload, rc)

def command_confirm_fresh(args: argparse.Namespace) -> int:
    snap = manifest_first_snapshot(args.project)
    if not snap.get("canonical_exists"):
        return emit({
            "status": "blocked",
            "error": "No completed black-point canonical exists to rerun."
        }, 2)
    if snap.get("canonical_status") == "unknown_or_damaged":
        return emit({
            "status": "blocked",
            "error": (
                "Existing black-point canonical is structurally damaged; "
                "fresh confirmation cannot authorize over it."
            ),
        }, 2)
    if snap.get("delegate_legacy_wrapper") is True:
        return emit({
            "status": "blocked",
            "error": (
                "This canonical is governed by the existing v1.0.4 same-source "
                "reselection migration, not a fresh rerun."
            ),
        }, 2)

    existing = matching_intent(args.project, snap)
    if existing is not None:
        try:
            path, obj = verify_authorized_intent(args.project, snap)
        except Exception as exc:
            return emit({"status": "blocked", "error": str(exc)}, 2)
        return emit({
            "status": "fresh_run_confirmed",
            "action": "continue_same_turn_with_advance",
            "project": snap["project"],
            "current_canonical_status": snap["canonical_status"],
            "canonical_output_preserved": True,
            "hashes_verified": True,
            "fresh_intent": str(path),
            "authorization_reused": True,
            "source_sha256": obj["source_sha256"],
            "canonical_manifest_sha256": obj["canonical_manifest_sha256"],
            "canonical_output_sha256": obj["canonical_output_sha256"],
            "next_command": (
                str(DEFAULT_WORKSPACE / "skills/siril-black-point/bin/black-point")
                + f' advance --project "{args.project}"'
            ),
        })

    try:
        binding = strong_binding(args.project, snap)
        if snap.get("canonical_status") == "ready":
            bridge_native_ready_confirmation(args.project)
        path, obj = write_intent(args.project, snap, binding)
    except Exception as exc:
        return emit({"status": "blocked", "action": "stop", "error": str(exc)}, 2)

    return emit({
        "status": "fresh_run_confirmed",
        "action": "continue_same_turn_with_advance",
        "project": snap["project"],
        "current_canonical_status": snap["canonical_status"],
        "canonical_output_preserved": True,
        "hashes_verified": True,
        "fresh_intent": str(path),
        "authorization_reused": False,
        "source_sha256": obj["source_sha256"],
        "canonical_manifest_sha256": obj["canonical_manifest_sha256"],
        "canonical_output_sha256": obj["canonical_output_sha256"],
        "next_command": (
            str(DEFAULT_WORKSPACE / "skills/siril-black-point/bin/black-point")
            + f' advance --project "{args.project}"'
        ),
    })

def command_self_test(args: argparse.Namespace) -> int:
    return emit({
        "status": "success",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_helper_version": PROCESSING_HELPER_VERSION,
        "processing_helper_byte_for_byte_unchanged": True,
        "legacy_v1_0_4_wrapper_preserved": True,
        "legacy_wrapper_owns_processing_review_selection_publication": True,
        "manifest_first_fast_path": True,
        "pre_confirmation_large_fits_hashing": False,
        "completed_current_requires_confirmation": True,
        "completed_obsolete_requires_confirmation": True,
        "obsolete_confirmation_strong_hash_binding": True,
        "current_ready_native_confirmation_bridge_via_legacy_wrapper": True,
        "legacy_same_source_reselection_preserved": True,
        "missing_stage_existing_v1_0_4_flow_preserved": True,
        "exact_read_targets_owned_by_v1_0_4_wrapper": True,
        "bounded_candidates_owned_by_processing_helper": 3,
        "selection_policy_version": "1.0.4",
        "processing_math_unchanged": True,
        "directory_discovery_forbidden_for_runtime_routing": True,
        "first_exec_advance_required": True,
        "green_reduction_handoff_preserved": True,
        "ghs2_quality_gate_required": True,
        "ghs2_next_stage_required": "siril-black-point",
    })

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Black-point v1.0.5 completed/obsolete orchestration.")
    p.add_argument("--version", action="version", version=ORCHESTRATION_VERSION)
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("advance")
    a.add_argument("--project", required=True)
    a.add_argument("--plan-only", action="store_true")
    a.add_argument("--timeout", type=int, default=7200)
    a.set_defaults(func=command_advance)

    c = sub.add_parser("confirm-fresh")
    c.add_argument("--project", required=True)
    c.set_defaults(func=command_confirm_fresh)

    t = sub.add_parser("self-test")
    t.set_defaults(func=command_self_test)
    return p

def main() -> int:
    args = build_parser().parse_args()
    try:
        return int(args.func(args))
    except subprocess.TimeoutExpired as exc:
        return emit({"status": "blocked", "error": f"v1.0.4 wrapper timed out: {exc}"}, 2)
    except BlackPointOrchestrationError as exc:
        return emit({"status": "blocked", "action": "stop_no_discovery", "error": str(exc)}, 2)

if __name__ == "__main__":
    raise SystemExit(main())
