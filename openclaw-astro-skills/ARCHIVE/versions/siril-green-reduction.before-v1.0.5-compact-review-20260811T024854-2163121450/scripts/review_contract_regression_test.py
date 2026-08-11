#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

def load(path: Path):
    spec = importlib.util.spec_from_file_location("green_reduction_review_contract", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--helper", required=True, type=Path)
    args = ap.parse_args()
    m = load(args.helper)

    if m.VERSION != "1.0.3":
        raise RuntimeError(f"Expected helper 1.0.3, got {m.VERSION}")

    fake_plan = {
        "status": "visual_review_required",
        "action": "read_previews_then_select_publish",
        "project": "/tmp/project",
        "project_name": "Synthetic",
        "run_root": "/tmp/run",
        "read_targets": [
            {"role": "before", "path": "/tmp/run/common/before.png", "sha256": "a" * 64},
            {"role": "candidate", "candidate": "candidate-00", "path": "/tmp/run/candidate-00/after.png", "sha256": "b" * 64},
            {"role": "candidate", "candidate": "candidate-01", "path": "/tmp/run/candidate-01/after.png", "sha256": "c" * 64},
            {"role": "candidate", "candidate": "candidate-02", "path": "/tmp/run/candidate-02/after.png", "sha256": "d" * 64},
        ],
        "publication_eligible_candidates": ["candidate-00", "candidate-01", "candidate-02"],
        "required_candidate_notes": ["candidate-00", "candidate-01", "candidate-02"],
        "recommended_candidate": "candidate-01",
        "candidates": [],
        "selection_policy": {"manual_successful_baseline": "0.15"},
        "assertive_override_instruction": "candidate-02 requires override",
    }

    payload = m.compact_review_plan(fake_plan)
    policy = payload.get("read_target_policy", {})

    if policy.get("path_handling") != "verbatim":
        raise RuntimeError("Read target paths are not required verbatim")
    if policy.get("directory_discovery_forbidden") is not True:
        raise RuntimeError("Directory discovery is not explicitly forbidden")
    if policy.get("on_read_failure") != "stop_and_report_exact_failed_path":
        raise RuntimeError("Read failure does not fail closed")
    if policy.get("do_not_construct_or_repair_paths") is not True:
        raise RuntimeError("Path construction/repair is not explicitly forbidden")

    forbidden = set(policy.get("forbidden_recovery_tools", []))
    required_forbidden = {"ls", "find", "cat", "grep", "jq", "globbing"}
    if not required_forbidden.issubset(forbidden):
        raise RuntimeError(f"Missing forbidden recovery tools: {sorted(required_forbidden - forbidden)}")

    instruction = payload.get("instruction", "")
    for required in ("verbatim", "STOP", "ls", "find", "residual green", "magenta/purple", "faint"):
        if required not in instruction:
            raise RuntimeError(f"Review instruction missing required concept: {required}")

    note_requirements = " ".join(payload.get("candidate_note_requirements", []))
    for required in ("residual green", "magenta", "purple", "faint", "Pillars", "dark-lane"):
        if required not in note_requirements:
            raise RuntimeError(f"Candidate-note contract missing: {required}")

    if len(json.dumps(payload).encode("utf-8")) > 4096:
        raise RuntimeError("Compact review plan exceeded 4096-byte review handoff cap")

    print(json.dumps({
        "status": "success",
        "helper_version": m.VERSION,
        "verbatim_paths_required": True,
        "read_failure_fails_closed": True,
        "directory_discovery_forbidden": True,
        "candidate_notes_require_visual_specificity": True,
    }, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
