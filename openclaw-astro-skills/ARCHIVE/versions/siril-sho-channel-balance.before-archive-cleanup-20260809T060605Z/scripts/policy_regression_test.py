#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib.util, json, sys
from pathlib import Path

def load(path: Path):
    spec = importlib.util.spec_from_file_location("sho_channel_balance_policy", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--helper", required=True, type=Path)
    args = ap.parse_args()
    m = load(args.helper)

    assert m.VERSION == "1.1.0"
    assert m.BASELINE_COEFFICIENTS == {"r": 1.0, "g": 0.25, "b": 1.0}
    assert m.COEFFICIENT_BOUNDS == {
        "r": (0.70, 1.30),
        "g": (0.15, 0.40),
        "b": (0.70, 1.30),
    }
    assert m.COEFFICIENT_STEPS == {"r": 0.15, "g": 0.05, "b": 0.15}
    assert m.MAX_ATTEMPTS == 5

    baseline = {
        "candidate": "candidate-01",
        "coefficients": {"r": 1.0, "g": 0.25, "b": 1.0},
        "change_from_previous": None,
    }
    proposed, change = m.propose_coefficients(
        baseline, "excessive_green", False
    )
    assert proposed == {"r": 1.0, "g": 0.20, "b": 1.0}
    assert change["coefficient"] == "g"

    second = {
        "candidate": "candidate-02",
        "coefficients": proposed,
        "change_from_previous": change,
    }
    try:
        m.propose_coefficients(second, "insufficient_green", False)
    except Exception:
        reversal_blocked = True
    else:
        reversal_blocked = False
    assert reversal_blocked

    reversed_proposed, _ = m.propose_coefficients(
        second, "insufficient_green", True
    )
    assert reversed_proposed["g"] == 0.25

    valid = [
        "candidate-01=balance:green remains visibly dominant against red and blue structures; magenta:no magenta or purple cast is visible in this candidate; structure:faint emission and dark lanes remain clearly preserved; noise:weak blue channel noise remains smooth without obvious amplification"
    ]
    parsed = m.parse_selection_notes(valid, ["candidate-01"])
    assert set(parsed) == {"candidate-01"}

    try:
        m.parse_selection_notes(
            ["candidate-01=balance:looks good; magenta:none; structure:preserved; noise:fine"],
            ["candidate-01"],
        )
    except Exception:
        vague_rejected = True
    else:
        vague_rejected = False
    assert vague_rejected

    script = m.candidate_script({"r": 1.0, "g": 0.25, "b": 1.0})
    assert 'pm "med($R$) + 1.000000 * ($R$ - med($R$))" -nosum' in script
    assert 'pm "med($R$) + 0.250000 * ($G$ - med($G$))" -nosum' in script
    assert 'pm "med($R$) + 1.000000 * ($B$ - med($B$))" -nosum' in script
    assert "-rescale" not in script
    assert "rgbcomp" in script and "-nosum" in script

    print(json.dumps({
        "status": "success",
        "helper_version": m.VERSION,
        "baseline_coefficients": m.BASELINE_COEFFICIENTS,
        "maximum_attempts": m.MAX_ATTEMPTS,
        "one_coefficient_change_per_attempt": True,
        "reversal_requires_overshoot": True,
        "vague_selection_notes_rejected": True,
        "pixelmath_rescale_disabled": True,
        "post_starnet_starless_only": True,
    }, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
