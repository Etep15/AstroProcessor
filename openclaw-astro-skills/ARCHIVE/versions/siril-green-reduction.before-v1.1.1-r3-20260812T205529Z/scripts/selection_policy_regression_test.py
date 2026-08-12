#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def load(path: Path):
    spec = importlib.util.spec_from_file_location("green_reduction_policy_v106", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not import helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def require_sequence(lines: list[str], expected: list[str], label: str) -> None:
    width = len(expected)
    if not any(lines[i:i + width] == expected for i in range(len(lines) - width + 1)):
        raise RuntimeError(f"Generated Siril script lost {label}: expected contiguous sequence {expected}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--helper", required=True, type=Path)
    args = ap.parse_args()
    m = load(args.helper)

    expected_amounts = {
        "candidate-00": 0.00,
        "candidate-01": 0.10,
        "candidate-02": 0.15,
    }
    expected_classes = {
        "candidate-00": "no-correction",
        "candidate-01": "mild",
        "candidate-02": "moderate",
    }
    if m.VERSION != "1.0.3":
        raise RuntimeError(f"Expected compatibility helper 1.0.3, got {m.VERSION}")
    if getattr(m, "PROCESSING_POLICY_REVISION", None) != "optional-noop-0.00-0.10-0.15-v1":
        raise RuntimeError("Processing policy revision is not v1.0.6 no-op/mild policy")
    if m.CANDIDATE_AMOUNTS != expected_amounts:
        raise RuntimeError(f"Candidate amounts changed: {m.CANDIDATE_AMOUNTS}")
    if m.CANDIDATE_CLASSIFICATION != expected_classes:
        raise RuntimeError(f"Candidate classifications changed: {m.CANDIDATE_CLASSIFICATION}")
    if m.MANUAL_BASELINE_AMOUNT != 0.10:
        raise RuntimeError(f"Expected mild baseline 0.10, got {m.MANUAL_BASELINE_AMOUNT}")
    if m.RM_GREEN_TYPE != 2 or m.PRESERVE_LIGHTNESS is not True:
        raise RuntimeError("Maximum Mask/preserve-lightness policy changed")

    script = m.green_reduction_script_text()
    lines = [line.strip() for line in script.splitlines() if line.strip()]
    commands = [line for line in lines if line.startswith("rmgreen ")]
    expected_commands = ["rmgreen 2 0.100", "rmgreen 2 0.150"]
    if commands != expected_commands:
        raise RuntimeError(f"Generated Siril correction commands differ from v1.0.6 policy: {commands}")
    if "rmgreen 2 0.000" in script:
        raise RuntimeError("candidate-00 is not a true no-op; rmgreen 2 0.000 is still present")

    require_sequence(lines, [
        'load "SHO-starless-black-point.fit"',
        'save "../candidate-00/work/SHO-starless-green-reduced.fit"',
        'savepng "../candidate-00/previews/SHO-starless-green-reduced"',
        'close',
    ], "candidate-00 no-op passthrough block")
    require_sequence(lines, [
        'load "SHO-starless-black-point.fit"',
        'rmgreen 2 0.100',
        'save "../candidate-01/work/SHO-starless-green-reduced.fit"',
        'savepng "../candidate-01/previews/SHO-starless-green-reduced"',
        'close',
    ], "candidate-01 mild-correction block")
    require_sequence(lines, [
        'load "SHO-starless-black-point.fit"',
        'rmgreen 2 0.150',
        'save "../candidate-02/work/SHO-starless-green-reduced.fit"',
        'savepng "../candidate-02/previews/SHO-starless-green-reduced"',
        'close',
    ], "candidate-02 moderate-correction block")

    fake = [
        {"candidate": name, "quality_assessment": {"satisfactory": True}}
        for name in m.CANDIDATE_AMOUNTS
    ]
    gate = m.publication_gate(fake)
    if gate.get("recommended_candidate") != "candidate-01":
        raise RuntimeError(f"Expected candidate-01 mild recommendation, got {gate}")

    try:
        m.validate_assertive_override_reason(None)
    except Exception:
        pass
    else:
        raise RuntimeError("Missing candidate-02 override did not fail closed")

    valid = (
        "The no-correction and 0.10 candidates both leave clearly unwanted green; "
        "0.15 removes that residual green without magenta/purple and preserves faint nebular structure."
    )
    m.validate_assertive_override_reason(valid)

    print(json.dumps({
        "status": "success",
        "helper_version": m.VERSION,
        "processing_policy_revision": m.PROCESSING_POLICY_REVISION,
        "candidate_amounts": m.CANDIDATE_AMOUNTS,
        "candidate_classification": m.CANDIDATE_CLASSIFICATION,
        "recommended_candidate": gate.get("recommended_candidate"),
        "candidate_02_override_required": True,
        "candidate_00_true_noop": True,
        "siril_correction_commands": commands,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
