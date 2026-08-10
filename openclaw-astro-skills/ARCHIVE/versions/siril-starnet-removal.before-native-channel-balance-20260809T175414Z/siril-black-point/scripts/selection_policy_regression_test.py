#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path


def load(path: Path):
    spec = importlib.util.spec_from_file_location(
        "black_point_policy_104",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not import staged helper.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def candidate(name, clip, score):
    return {
        "candidate": name,
        "histogram_classification": "balanced",
        "selection_score": score,
        "quality_assessment": {
            "satisfactory": True,
            "metrics": {
                "channel_low_clip_fraction": clip,
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", required=True, type=Path)
    args = parser.parse_args()

    bp = load(args.helper.resolve())

    candidates = [
        candidate(
            "candidate-00",
            0.0015819817589420076,
            0.8986683762804781,
        ),
        candidate(
            "candidate-01",
            0.005088861955424912,
            0.2428565866451849,
        ),
    ]

    summary = bp.selection_policy_summary(candidates)

    if summary["numerical_recommended_candidate"] != "candidate-01":
        raise RuntimeError(
            "Expected numerical recommendation candidate-01."
        )
    if summary["recommended_candidate"] != "candidate-00":
        raise RuntimeError(
            "v1.0.4 policy did not prefer candidate-00."
        )

    p0 = summary["candidate_policy"]["candidate-00"]
    p1 = summary["candidate_policy"]["candidate-01"]

    if p0["classification"] != "preferred":
        raise RuntimeError(
            f"candidate-00 classification wrong: {p0}"
        )
    if p1["classification"] != "aggressive":
        raise RuntimeError(
            f"candidate-01 classification wrong: {p1}"
        )

    if not bp.aggressive_policy_override_required(
        "candidate-01",
        candidates,
    ):
        raise RuntimeError(
            "Aggressive candidate should require an override."
        )
    if bp.aggressive_policy_override_required(
        "candidate-00",
        candidates,
    ):
        raise RuntimeError(
            "Preferred candidate must not require an override."
        )

    try:
        bp.validate_aggressive_override_reason(None)
    except Exception:
        pass
    else:
        raise RuntimeError(
            "Missing aggressive override reason did not fail closed."
        )

    valid_reason = (
        "Faint outer emission remains visibly preserved while pillar "
        "structure and dark-lane separation improve materially, with no "
        "loss of low-contrast detail."
    )
    if bp.validate_aggressive_override_reason(valid_reason) != valid_reason:
        raise RuntimeError(
            "Valid aggressive override reason was not accepted."
        )

    print(
        json.dumps(
            {
                "status": "success",
                "helper_version": bp.VERSION,
                "numerical_recommendation": "candidate-01",
                "selection_policy_recommendation": "candidate-00",
                "candidate_00_classification": "preferred",
                "candidate_01_classification": "aggressive",
                "aggressive_override_required": True,
                "missing_override_blocked": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
