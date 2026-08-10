#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ORCHESTRATION_VERSION = "1.2.0"
ENGINE_VERSION = "1.1.0"
STARNET_CONTRACT = "native-starnet-channel-balance-v1"
DOWNSTREAM_CONTRACT = "post-starnet-channel-balance-v1"

WORKSPACE = Path(os.environ.get(
    "SHO_CHANNEL_BALANCE_WORKSPACE",
    "/home/peter/.openclaw/workspace/agents/codewarrior",
))
ENGINE_PATH = Path(os.environ.get(
    "SHO_CHANNEL_BALANCE_ENGINE",
    "/home/peter/.openclaw/workspace/agents/codewarrior/skills/"
    "siril-sho-channel-balance/scripts/sho_channel_balance.py",
))
STATE_DIR_NAME = ".siril-sho-channel-balance-v1.2.0"


class OrchestrationError(RuntimeError):
    pass


def load_engine():
    if not ENGINE_PATH.is_file():
        raise OrchestrationError(f"Channel-balance processing engine is missing: {ENGINE_PATH}")
    spec = importlib.util.spec_from_file_location("sho_channel_balance_engine_v110", ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise OrchestrationError(f"Cannot load channel-balance engine: {ENGINE_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    if getattr(module, "VERSION", None) != ENGINE_VERSION:
        raise OrchestrationError(
            f"Expected channel-balance engine {ENGINE_VERSION}; got {getattr(module, 'VERSION', None)!r}"
        )
    return module


engine = load_engine()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OrchestrationError(f"Cannot read JSON {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise OrchestrationError(f"Expected JSON object: {path}")
    return payload


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uid()}.partial")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def paths_for(workspace: Path, project_name: str) -> dict[str, Path]:
    p = engine.project_paths(workspace, project_name)
    state = p["project"] / STATE_DIR_NAME
    return {
        **p,
        "state": state,
        "intents_v12": state / "fresh-intents",
        "orchestration_review": p["stable"] / "orchestration-review-v1.2.0.json",
    }


def validate_project_exists(workspace: Path, project_name: str) -> dict[str, Path]:
    p = paths_for(workspace, project_name)
    if not p["project"].is_dir():
        raise OrchestrationError(f"Project does not exist: {p['project']}")
    return p


def fast_upstream_manifest(workspace: Path, project_name: str) -> dict[str, Any]:
    p = validate_project_exists(workspace, project_name)
    if not p["source_manifest"].is_file():
        raise OrchestrationError(f"StarNet manifest is missing: {p['source_manifest']}")
    m = load_json(p["source_manifest"])
    errors = []
    if m.get("helper_version") != "1.5.2": errors.append("StarNet helper is not 1.5.2")
    if m.get("source_contract_revision") != STARNET_CONTRACT: errors.append("StarNet native channel-balance contract is missing")
    if m.get("status") != "ready": errors.append("StarNet manifest is not ready")
    if m.get("visual_review_completed") is not True: errors.append("StarNet visual review is incomplete")
    if m.get("sho_channel_balance_permitted") is not True: errors.append("StarNet does not permit SHO channel balance")
    if m.get("ghs_pass1_permitted") is not False: errors.append("StarNet incorrectly permits direct GHS pass 1")
    if m.get("next_stage") != "siril-sho-channel-balance": errors.append("StarNet next stage is not SHO channel balance")
    if m.get("starless_processing_permitted") is not True: errors.append("StarNet does not permit starless processing")
    if m.get("starless_background_processing_permitted") is not False: errors.append("StarNet incorrectly permits another background stage")
    if m.get("stage_order") != {
        "upstream": "siril-background-neutralization",
        "current": "siril-starnet-removal",
        "downstream": "siril-sho-channel-balance",
    }: errors.append("StarNet stage order is not the native channel-balance handoff")
    linear = m.get("linear_starless", {})
    source_sha = linear.get("sha256")
    if not isinstance(source_sha, str) or len(source_sha) != 64:
        errors.append("StarNet manifest does not contain a valid starless SHA-256")
    if errors:
        raise OrchestrationError("Upstream StarNet contract failed: " + "; ".join(errors))
    return {
        "manifest": m,
        "manifest_sha256": sha256_file(p["source_manifest"]),
        "source_sha256": source_sha,
        "visual_review_sha256": m.get("visual_review", {}).get("record_sha256"),
    }


def canonical_snapshot_fast(workspace: Path, project_name: str) -> dict[str, Any]:
    p = validate_project_exists(workspace, project_name)
    upstream = fast_upstream_manifest(workspace, project_name)
    if not p["stable_manifest"].is_file():
        return {
            "exists": False,
            "status": "missing",
            "hashes_verified": False,
            "current_upstream_source_sha256": upstream["source_sha256"],
        }
    manifest = load_json(p["stable_manifest"])
    output = p["stable_output"]
    completed = manifest.get("status") == "ready" and output.is_file()
    if not completed:
        return {
            "exists": True,
            "completed": False,
            "status": "invalid",
            "hashes_verified": False,
            "errors": ["Canonical manifest/output is not a complete ready checkpoint."],
            "current_upstream_source_sha256": upstream["source_sha256"],
        }
    errors = []
    if manifest.get("helper_version") != ENGINE_VERSION:
        errors.append(f"canonical processing engine is {manifest.get('helper_version')!r}, not {ENGINE_VERSION}")
    if manifest.get("next_stage") != "siril-ghs-stretch-pass1": errors.append("next stage is not GHS pass 1")
    if manifest.get("ghs_pass1_permitted") is not True: errors.append("GHS pass 1 is not permitted")
    if manifest.get("stage_order") != {
        "upstream": "siril-starnet-removal",
        "current": "siril-sho-channel-balance",
        "downstream": "siril-ghs-stretch-pass1",
    }: errors.append("stage order is not post-StarNet/pre-GHS")
    old_source_sha = manifest.get("source", {}).get("sha256")
    if old_source_sha != upstream["source_sha256"]:
        errors.append("StarNet starless upstream source changed")
    old_upstream_manifest_sha = manifest.get("upstream_summary", {}).get("manifest_sha256")
    if old_upstream_manifest_sha != upstream["manifest_sha256"]:
        errors.append("StarNet upstream manifest changed")
    if manifest.get("upstream_summary", {}).get("contract_mode") != STARNET_CONTRACT:
        errors.append("canonical channel balance predates the native StarNet source contract")
    return {
        "exists": True,
        "completed": True,
        "status": "ready" if not errors else "obsolete",
        "hashes_verified": False,
        "hash_verification_deferred_until": "confirm-fresh",
        "canonical_manifest_sha256": sha256_file(p["stable_manifest"]),
        "canonical_output_recorded_sha256": manifest.get("output", {}).get("sha256"),
        "canonical_source_sha256": old_source_sha,
        "current_upstream_source_sha256": upstream["source_sha256"],
        "current_upstream_manifest_sha256": upstream["manifest_sha256"],
        "errors": errors,
    }


def strong_snapshot(workspace: Path, project_name: str) -> dict[str, Any]:
    p = validate_project_exists(workspace, project_name)
    fast = canonical_snapshot_fast(workspace, project_name)
    if not fast.get("completed"):
        raise OrchestrationError("Fresh-run confirmation requires a completed canonical channel-balance result.")
    try:
        _, source, upstream = engine.validate_upstream(workspace, project_name)
    except Exception as exc:
        raise OrchestrationError(str(exc)) from exc
    actual_output_sha = sha256_file(p["stable_output"])
    manifest = load_json(p["stable_manifest"])
    recorded_output_sha = manifest.get("output", {}).get("sha256")
    if actual_output_sha != recorded_output_sha:
        raise OrchestrationError("Existing canonical channel-balance output checksum no longer matches its manifest.")
    return {
        **fast,
        "hashes_verified": True,
        "canonical_output_sha256": actual_output_sha,
        "canonical_manifest_sha256": sha256_file(p["stable_manifest"]),
        "current_upstream_source_sha256": source.sha256,
        "current_upstream_manifest_sha256": upstream["manifest_sha256"],
        "current_upstream_review_sha256": upstream["visual_review_record_sha256"],
    }


def authorized_intent_may_exist(p: dict[str, Path], project_name: str) -> bool:
    root = p["intents_v12"]
    if not root.is_dir(): return False
    for path in root.glob("fresh-run-*.json"):
        try: row = load_json(path)
        except Exception: continue
        if row.get("status") == "authorized" and row.get("project_name") == project_name:
            return True
    return False


def locate_intent(p: dict[str, Path], project_name: str, strong: dict[str, Any]) -> Path | None:
    root = p["intents_v12"]
    if not root.is_dir(): return None
    matches = []
    for path in root.glob("fresh-run-*.json"):
        try: row = load_json(path)
        except Exception: continue
        if (
            row.get("status") == "authorized"
            and row.get("project_name") == project_name
            and row.get("canonical_output_sha256") == strong.get("canonical_output_sha256")
            and row.get("canonical_manifest_sha256") == strong.get("canonical_manifest_sha256")
            and row.get("source_sha256") == strong.get("current_upstream_source_sha256")
            and row.get("source_manifest_sha256") == strong.get("current_upstream_manifest_sha256")
        ):
            matches.append((path.stat().st_mtime, path))
    if not matches: return None
    return sorted(matches, key=lambda x: x[0], reverse=True)[0][1]


def confirm_fresh(workspace: Path, project_name: str) -> dict[str, Any]:
    p = validate_project_exists(workspace, project_name)
    strong = strong_snapshot(workspace, project_name)
    p["intents_v12"].mkdir(parents=True, exist_ok=True)
    path = p["intents_v12"] / f"fresh-run-{uid()}.json"
    payload = {
        "schema_version": 1,
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "status": "authorized",
        "authorized_at": utc_now(),
        "project_name": project_name,
        "canonical_output_sha256": strong["canonical_output_sha256"],
        "canonical_manifest_sha256": strong["canonical_manifest_sha256"],
        "source_sha256": strong["current_upstream_source_sha256"],
        "source_manifest_sha256": strong["current_upstream_manifest_sha256"],
        "source_review_sha256": strong["current_upstream_review_sha256"],
    }
    write_json_atomic(path, payload)
    return {
        "status": "fresh_run_confirmed",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "project": str(p["project"]),
        "fresh_intent": str(path),
        "canonical_output_preserved": True,
        "current_canonical_status": strong["status"],
        "source_sha256": strong["current_upstream_source_sha256"],
        "canonical_output_sha256": strong["canonical_output_sha256"],
    }


def active_run(workspace: Path, project_name: str):
    p = validate_project_exists(workspace, project_name)
    try:
        return engine.latest_active_run(p)
    except Exception as exc:
        raise OrchestrationError(str(exc)) from exc


def wrap_visual_plan(run_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    base = engine.visual_review_plan(run_root, record)
    attempt = int(base["attempt"])
    # The source is identical for the entire run. Read it once on attempt 1;
    # subsequent attempts need only the new candidate preview.
    targets = base["read_targets"] if attempt == 1 else [base["read_targets"][1]]
    return {
        **base,
        "action": "continue_autonomously_review_refine",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "read_targets": targets,
        "source_reference": base["read_targets"][0],
        "source_reread_required": attempt == 1,
        "autonomous_completion_policy": {
            "ask_user": False,
            "read_exact_targets": True,
            "directory_discovery_forbidden": True,
            "media_attachment_forbidden": True,
            "choose_dominant_problem_autonomously": True,
            "numeric_coefficients_owned_by_engine": True,
            "continue_until_selection_or_blocker": True,
        },
        "review_note_minimum_characters": 40,
        "review_refine_command_template": {
            "command": "/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-sho-channel-balance/bin/sho-channel-balance review-refine",
            "required": [
                '--project "<project>"',
                f'--candidate "{base["candidate"]}"',
                '--dominant-problem "<one allowed value>"',
                '--green-note "<40+ chars specific visual observation>"',
                '--magenta-note "<40+ chars specific visual observation>"',
                '--red-note "<40+ chars specific visual observation>"',
                '--blue-note "<40+ chars specific visual observation>"',
                '--structure-note "<40+ chars specific visual observation>"',
                '--noise-note "<40+ chars specific visual observation>"',
            ],
            "optional": ['--overshoot-observed'],
        },
    }


def validate_review_text(name: str, value: str) -> str:
    cleaned = " ".join(value.split())
    if len(cleaned) < 40:
        raise OrchestrationError(f"{name} must contain at least 40 characters of specific visual evidence.")
    return cleaned


def wrap_selection_plan(run_root: Path, record: dict[str, Any]) -> dict[str, Any]:
    # Every generated candidate has already been read and reviewed before this
    # state is reachable. Avoid rereading the same source/candidates and select
    # from accumulated visual evidence plus technical summaries.
    base = engine.selection_review_plan(run_root, record)
    reviewed = []
    for c in record.get("candidates", []):
        reviewed.append({
            "candidate": c["candidate"],
            "attempt": c["attempt"],
            "coefficients": c["coefficients"],
            "review": c["review"],
            "blue_mad_ratio": c["quality_assessment"]["metrics"]["channel_mad_ratios"]["blue"],
            "technical_satisfactory": c["quality_assessment"]["satisfactory"],
            "preview_path": c["preview"]["path"],
            "preview_sha256": c["preview"]["sha256"],
        })
    return {
        **base,
        "action": "continue_autonomously_select_publish",
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "read_targets": [],
        "reread_required": False,
        "reviewed_candidates": reviewed,
        "selection_rule": (
            "Choose the best acceptable STARLESS candidate from the visual reviews already completed. "
            "Prefer the least aggressive coefficients when two candidates are materially equivalent. "
            "Do not ask the user and do not evaluate star colour."
        ),
        "selection_note_format": (
            "candidate-NN=balance:<40+ chars>; magenta:<40+ chars>; "
            "structure:<40+ chars>; noise:<40+ chars>"
        ),
        "selection_policy": {
            "ask_user": False,
            "use_accumulated_review_evidence": True,
            "candidate_specific_notes_required": True,
            "overall_visual_notes_minimum_characters": 80,
            "internal_semicolons_in_values_are_sanitized": True,
            "recommendation_or_latest_attempt_is_not_automatic": True,
        },
        "select_publish_command_template": {
            "command": "/home/peter/.openclaw/workspace/agents/codewarrior/skills/siril-sho-channel-balance/bin/sho-channel-balance select-publish",
            "required": [
                '--project "<project>"',
                '--candidate "<selected candidate>"',
                '--visual-notes "<80+ chars overall candidate comparison>"',
                *[
                    f'--note "{c["candidate"]}=balance:<40+ chars>; magenta:<40+ chars>; structure:<40+ chars>; noise:<40+ chars>"'
                    for c in record.get("candidates", [])
                ],
            ],
        },
    }


def advance(workspace: Path, project_name: str, timeout: int, plan_only: bool = False) -> dict[str, Any]:
    p = validate_project_exists(workspace, project_name)
    active = active_run(workspace, project_name)
    if active:
        root, record = active
        if record.get("status") == "awaiting_review": return wrap_visual_plan(root, record)
        if record.get("status") == "selection_review_required": return wrap_selection_plan(root, record)
        raise OrchestrationError(f"Unsupported active run status: {record.get('status')!r}")

    snap = canonical_snapshot_fast(workspace, project_name)
    if snap.get("exists") and not snap.get("completed"):
        return {
            "status": "blocked",
            "action": "stop",
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_engine_version": ENGINE_VERSION,
            "reason": "Existing canonical channel-balance checkpoint is incomplete or invalid; preserve and repair it before processing.",
        }
    if snap.get("completed"):
        if not authorized_intent_may_exist(p, project_name):
            return {
                "status": "confirmation_required",
                "action": "await_user_confirmation",
                "orchestration_version": ORCHESTRATION_VERSION,
                "processing_engine_version": ENGINE_VERSION,
                "project": str(p["project"]),
                "question": (
                    f"SHO channel balance for {project_name} has already completed"
                    + (" but is obsolete for the current StarNet result" if snap["status"] == "obsolete" else " successfully")
                    + ". Do you want me to run it again as a fresh run?"
                ),
                "confirmation_required": True,
                "current_canonical_status": snap["status"],
                "canonical_output_preserved": True,
                "hashes_verified": False,
                "hash_verification_deferred_until": "confirm-fresh",
                "obsolete_reasons": snap.get("errors", []),
                "current_upstream_source_sha256": snap.get("current_upstream_source_sha256"),
                "canonical_source_sha256": snap.get("canonical_source_sha256"),
            }
        strong = strong_snapshot(workspace, project_name)
        intent = locate_intent(p, project_name, strong)
        if intent is None:
            return {
                "status": "confirmation_required",
                "action": "await_user_confirmation",
                "orchestration_version": ORCHESTRATION_VERSION,
                "processing_engine_version": ENGINE_VERSION,
                "project": str(p["project"]),
                "question": f"SHO channel balance for {project_name} has already completed. Do you want me to run it again as a fresh run?",
                "confirmation_required": True,
                "current_canonical_status": snap["status"],
                "canonical_output_preserved": True,
                "hashes_verified": True,
                "authorization_match": False,
            }
        if plan_only:
            return {
                "status": "would_generate_baseline",
                "action": "fresh_authorization_verified",
                "orchestration_version": ORCHESTRATION_VERSION,
                "processing_engine_version": ENGINE_VERSION,
                "project": str(p["project"]),
                "source_sha256": strong["current_upstream_source_sha256"],
                "baseline_coefficients": engine.BASELINE_COEFFICIENTS,
                "maximum_attempts": engine.MAX_ATTEMPTS,
            }
        try:
            root, record = engine.create_run(workspace, project_name, timeout, fresh_intent=intent)
        except Exception as exc:
            raise OrchestrationError(str(exc)) from exc
        return wrap_visual_plan(root, record)

    if plan_only:
        upstream = fast_upstream_manifest(workspace, project_name)
        return {
            "status": "would_generate_baseline",
            "action": "generate_baseline_then_review",
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_engine_version": ENGINE_VERSION,
            "project": str(p["project"]),
            "source_sha256": upstream["source_sha256"],
            "baseline_coefficients": engine.BASELINE_COEFFICIENTS,
            "maximum_attempts": engine.MAX_ATTEMPTS,
        }
    try:
        root, record = engine.create_run(workspace, project_name, timeout, fresh_intent=None)
    except Exception as exc:
        raise OrchestrationError(str(exc)) from exc
    return wrap_visual_plan(root, record)


def review_refine(workspace: Path, args) -> dict[str, Any]:
    for label, value in (
        ("green-note", args.green_note), ("magenta-note", args.magenta_note),
        ("red-note", args.red_note), ("blue-note", args.blue_note),
        ("structure-note", args.structure_note), ("noise-note", args.noise_note),
    ):
        validate_review_text(label, value)
    try:
        payload = engine.review_refine_stage(
            workspace, args.project, args.candidate, args.dominant_problem,
            args.green_note, args.magenta_note, args.red_note, args.blue_note,
            args.structure_note, args.noise_note, args.overshoot_observed, args.timeout,
        )
    except Exception as exc:
        raise OrchestrationError(str(exc)) from exc
    active = active_run(workspace, args.project)
    if active is None:
        raise OrchestrationError("Channel-balance run disappeared after review refinement.")
    root, record = active
    if payload.get("status") == "visual_review_required": return wrap_visual_plan(root, record)
    if payload.get("status") == "selection_review_required": return wrap_selection_plan(root, record)
    raise OrchestrationError(f"Unexpected review-refine result: {payload.get('status')!r}")


FIELD_BOUNDARY = re.compile(r";\s*(?=(?:balance|magenta|structure|noise)\s*:)", re.I)


def parse_selection_notes(values: list[str], expected: list[str]) -> list[str]:
    expected_set = set(expected)
    parsed = {}
    for raw in values:
        if "=" not in raw: raise OrchestrationError("Each --note must start with candidate-NN=...")
        candidate, body = raw.split("=", 1); candidate = candidate.strip()
        if candidate not in expected_set: raise OrchestrationError(f"Unexpected candidate in selection note: {candidate}")
        fields = {}
        for part in [x.strip() for x in FIELD_BOUNDARY.split(body) if x.strip()]:
            if ":" not in part: raise OrchestrationError(f"{candidate} has an unlabeled selection field")
            key, value = part.split(":", 1); key = key.strip().lower(); value = " ".join(value.split())
            if key not in {"balance", "magenta", "structure", "noise"}: raise OrchestrationError(f"{candidate} has unknown selection field {key}")
            if key in fields: raise OrchestrationError(f"{candidate} repeats selection field {key}")
            if len(value) < 40: raise OrchestrationError(f"{candidate} {key} must contain at least 40 characters")
            fields[key] = value
        if set(fields) != {"balance", "magenta", "structure", "noise"}:
            raise OrchestrationError(f"{candidate} must contain exactly balance, magenta, structure, and noise fields")
        parsed[candidate] = fields
    if set(parsed) != expected_set:
        raise OrchestrationError(f"Selection notes must cover every generated candidate exactly: {expected}")
    signatures = [json.dumps(parsed[name], sort_keys=True) for name in expected]
    if len(signatures) > 1 and len(set(signatures)) == 1:
        raise OrchestrationError("Selection observations must be candidate-specific; identical boilerplate is not allowed.")
    # Reconstruct a form guaranteed safe for the v1.1.0 engine's simple semicolon parser.
    out = []
    for name in expected:
        f = parsed[name]
        clean = {k: v.replace(";", ",") for k, v in f.items()}
        out.append(
            f"{name}=balance:{clean['balance']}; magenta:{clean['magenta']}; "
            f"structure:{clean['structure']}; noise:{clean['noise']}"
        )
    return out


def select_publish(workspace: Path, args) -> dict[str, Any]:
    overall = " ".join(args.visual_notes.split())
    if len(overall) < 80:
        raise OrchestrationError("--visual-notes must contain at least 80 characters of overall visual comparison.")
    active = active_run(workspace, args.project)
    if active is None: raise OrchestrationError("No active channel-balance run exists.")
    root, record = active
    expected = [c["candidate"] for c in record.get("candidates", [])]
    safe_notes = parse_selection_notes(args.candidate_notes, expected)
    try:
        result = engine.select_publish_stage(workspace, args.project, args.candidate, overall, safe_notes)
    except Exception as exc:
        raise OrchestrationError(str(exc)) from exc
    if result.get("status") != "ready":
        raise OrchestrationError(f"Channel-balance publication did not become ready: {result}")
    p = paths_for(workspace, args.project)
    review_record = {
        "schema_version": 1,
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "recorded_at": utc_now(),
        "project_name": args.project,
        "run_root": str(root),
        "review_method": "openclaw-read-iterative",
        "selected_candidate": args.candidate,
        "selected_coefficients": result.get("selected_coefficients"),
        "visual_notes": overall,
        "candidate_notes": safe_notes,
        "source_contract_revision": DOWNSTREAM_CONTRACT,
        "next_stage": "siril-ghs-stretch-pass1",
        "ghs_pass1_permitted": True,
        "source_is_starless": True,
        "stars_layer_modified": False,
        "visual_review_completed": True,
    }
    write_json_atomic(p["orchestration_review"], review_record)
    return {
        **result,
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "orchestration_review_record": str(p["orchestration_review"]),
        "source_contract_revision": DOWNSTREAM_CONTRACT,
    }


def status(workspace: Path, project_name: str) -> dict[str, Any]:
    active = active_run(workspace, project_name)
    if active:
        root, record = active
        return {
            "status": record.get("status"),
            "orchestration_version": ORCHESTRATION_VERSION,
            "processing_engine_version": ENGINE_VERSION,
            "project": str(paths_for(workspace, project_name)["project"]),
            "run_root": str(root),
            "current_candidate": record.get("current_candidate"),
            "generated_candidate_count": len(record.get("candidates", [])),
            "ghs_pass1_permitted": False,
        }
    snap = canonical_snapshot_fast(workspace, project_name)
    return {
        "status": snap.get("status"),
        "orchestration_version": ORCHESTRATION_VERSION,
        "processing_engine_version": ENGINE_VERSION,
        "project": str(paths_for(workspace, project_name)["project"]),
        "canonical_exists": snap.get("exists"),
        "completed": snap.get("completed", False),
        "hashes_verified": False,
        "current_upstream_source_sha256": snap.get("current_upstream_source_sha256"),
        "canonical_source_sha256": snap.get("canonical_source_sha256"),
        "errors": snap.get("errors", []),
        "ghs_pass1_permitted": snap.get("status") == "ready",
    }


def self_test() -> dict[str, Any]:
    from astropy.io import fits
    import numpy as np

    with tempfile.TemporaryDirectory(prefix="sho-cb-orch-v120-") as td:
        workspace = Path(td) / "w"
        project_name = "T"
        p = paths_for(workspace, project_name)
        p["source"].parent.mkdir(parents=True, exist_ok=True)
        data = np.stack([
            np.full((16, 18), 0.03, dtype=np.float32),
            np.full((16, 18), 0.06, dtype=np.float32),
            np.full((16, 18), 0.04, dtype=np.float32),
        ])
        fits.PrimaryHDU(data=data).writeto(p["source"])
        p["source_review"].write_text('{"visual_review_completed":true}\n')
        source_sha = sha256_file(p["source"])
        review_sha = sha256_file(p["source_review"])
        source_manifest = {
            "helper_version": "1.5.2", "source_contract_revision": STARNET_CONTRACT,
            "status": "ready", "visual_review_completed": True,
            "sho_channel_balance_permitted": True, "ghs_pass1_permitted": False,
            "next_stage": "siril-sho-channel-balance",
            "stage_order": {"upstream":"siril-background-neutralization","current":"siril-starnet-removal","downstream":"siril-sho-channel-balance"},
            "starless_processing_permitted": True, "starless_background_processing_permitted": False,
            "project": project_name, "project_path": str(p["project"]),
            "linear_starless": {"path": str(p["source"]), "sha256": source_sha},
            "visual_review": {"record_path": str(p["source_review"]), "record_sha256": review_sha},
        }
        write_json_atomic(p["source_manifest"], source_manifest)
        p["stable"].mkdir(parents=True, exist_ok=True)
        p["stable_output"].write_bytes(b"old-balanced")
        old_sha = sha256_file(p["stable_output"])
        old_manifest = {
            "helper_version": ENGINE_VERSION, "status":"ready", "visual_review_completed":True,
            "next_stage":"siril-ghs-stretch-pass1", "ghs_pass1_permitted":True,
            "background_neutralization_permitted":False, "star_removal_permitted":False,
            "stage_order":{"upstream":"siril-starnet-removal","current":"siril-sho-channel-balance","downstream":"siril-ghs-stretch-pass1"},
            "output":{"sha256":old_sha},
            "source":{"sha256":"0"*64},
            "upstream_summary":{"manifest_sha256":"1"*64,"contract_mode":"temporary-starnet-1.5.2-ghs-bridge"},
        }
        write_json_atomic(p["stable_manifest"], old_manifest)
        snap = canonical_snapshot_fast(workspace, project_name)
        if snap.get("status") != "obsolete" or not snap.get("completed"):
            raise OrchestrationError(f"Synthetic obsolete checkpoint classification failed: {snap}")
        confirmed = confirm_fresh(workspace, project_name)
        if confirmed.get("status") != "fresh_run_confirmed":
            raise OrchestrationError("Synthetic obsolete completed-stage confirmation failed.")
        strong = strong_snapshot(workspace, project_name)
        intent = locate_intent(p, project_name, strong)
        if intent is None:
            raise OrchestrationError("Synthetic durable fresh intent did not bind correctly.")
        good = parse_selection_notes([
            "candidate-01=balance:Green is controlled while SII red and OIII blue remain clearly separated across the nebula; magenta:No broad magenta cast appears in the faint emission or around the central structures; structure:Pillars, dark lanes, and faint outer emission remain intact without visible clipping or flattening; noise:The weak OIII-derived channel remains smooth with no obvious amplification of mottled background noise",
            "candidate-02=balance:Colour separation is slightly stronger than candidate-01 while green remains below dominant levels; magenta:A slight purple tendency is visible only in the brightest transition areas and does not dominate; structure:Faint outer emission remains present, although the central contrast is marginally stronger than candidate-01; noise:Background texture remains controlled, with no material increase in weak-channel noise compared with candidate-01",
        ], ["candidate-01", "candidate-02"])
        if len(good) != 2:
            raise OrchestrationError("Selection note parser self-test failed.")
        try:
            parse_selection_notes([
                "candidate-01=balance:too short; magenta:too short; structure:too short; noise:too short"
            ], ["candidate-01"])
        except OrchestrationError:
            pass
        else:
            raise OrchestrationError("Selection note minimum-length self-test failed.")
    return {
        "status":"success",
        "orchestration_version":ORCHESTRATION_VERSION,
        "processing_engine_version":ENGINE_VERSION,
        "completed_obsolete_stage_requires_confirmation":True,
        "manifest_first_fast_status":True,
        "strong_hash_binding_after_confirmation":True,
        "autonomous_iterative_review":True,
        "autonomous_final_selection":True,
        "source_read_once_per_run":True,
        "selection_reread_required":False,
        "exact_read_targets_required":True,
        "directory_discovery_forbidden":True,
        "media_attachment_forbidden":True,
        "candidate_specific_selection_notes":True,
        "publication_format_repair_without_reprocessing":True,
        "star_layer_modified":False,
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="SHO channel-balance v1.2.0 autonomous orchestration wrapper")
    ap.add_argument("--version", action="version", version=ORCHESTRATION_VERSION)
    sub = ap.add_subparsers(dest="command", required=True)
    p=sub.add_parser("advance"); p.add_argument("--project",required=True); p.add_argument("--timeout",type=int,default=1800); p.add_argument("--plan-only",action="store_true")
    p=sub.add_parser("confirm-fresh"); p.add_argument("--project",required=True)
    p=sub.add_parser("review-refine"); p.add_argument("--project",required=True); p.add_argument("--candidate",required=True); p.add_argument("--dominant-problem",required=True,choices=engine.DOMINANT_PROBLEMS); p.add_argument("--green-note",required=True); p.add_argument("--magenta-note",required=True); p.add_argument("--red-note",required=True); p.add_argument("--blue-note",required=True); p.add_argument("--structure-note",required=True); p.add_argument("--noise-note",required=True); p.add_argument("--overshoot-observed",action="store_true"); p.add_argument("--timeout",type=int,default=1800)
    p=sub.add_parser("select-publish"); p.add_argument("--project",required=True); p.add_argument("--candidate",required=True); p.add_argument("--visual-notes",required=True); p.add_argument("--note",dest="candidate_notes",action="append",default=[])
    p=sub.add_parser("stage-status"); p.add_argument("--project",required=True)
    p=sub.add_parser("status"); p.add_argument("--project",required=True)
    sub.add_parser("self-test")
    return ap


def main() -> int:
    args=build_parser().parse_args()
    try:
        if args.command=="advance": payload=advance(WORKSPACE,args.project,args.timeout,args.plan_only)
        elif args.command=="confirm-fresh": payload=confirm_fresh(WORKSPACE,args.project)
        elif args.command=="review-refine": payload=review_refine(WORKSPACE,args)
        elif args.command=="select-publish": payload=select_publish(WORKSPACE,args)
        elif args.command in {"stage-status","status"}: payload=status(WORKSPACE,args.project)
        elif args.command=="self-test": payload=self_test()
        else: raise OrchestrationError(f"Unsupported command: {args.command}")
    except OrchestrationError as exc:
        print(json.dumps({"status":"blocked","orchestration_version":ORCHESTRATION_VERSION,"processing_engine_version":ENGINE_VERSION,"error":str(exc),"ghs_pass1_permitted":False},indent=2,sort_keys=True))
        return 2
    print(json.dumps(payload,indent=2,sort_keys=True))
    return 0 if payload.get("status") in {"success","missing","ready","obsolete","would_generate_baseline","confirmation_required","visual_review_required","selection_review_required","fresh_run_confirmed"} else 2

if __name__=="__main__":
    raise SystemExit(main())
