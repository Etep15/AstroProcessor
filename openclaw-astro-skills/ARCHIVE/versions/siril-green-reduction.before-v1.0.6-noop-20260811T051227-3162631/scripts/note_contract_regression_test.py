#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

def load(path: Path):
    spec = importlib.util.spec_from_file_location("green_reduction_note_contract", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def expect_rejected(m, notes, eligible, label):
    try:
        m.parse_candidate_notes(notes, eligible)
    except Exception:
        return
    raise RuntimeError(f"{label} was incorrectly accepted")

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--helper", required=True, type=Path)
    args = ap.parse_args()
    m = load(args.helper)

    if m.VERSION != "1.0.3":
        raise RuntimeError(f"Expected helper 1.0.3, got {m.VERSION}")

    eligible = ["candidate-00", "candidate-01", "candidate-02"]

    vague_v102 = [
        "candidate-00=Conservative; residual green cast remains.",
        "candidate-01=Optimal; removes cast and preserves structure.",
        "candidate-02=Too aggressive; potential magenta shift.",
    ]
    expect_rejected(m, vague_v102, eligible, "v1.0.2 vague note set")

    missing_structure = [
        "candidate-00=green: residual green cast remains visible; magenta: no magenta or purple shift visible",
        "candidate-01=green: unwanted green cast is removed; magenta: no magenta or purple shift visible",
        "candidate-02=green: green is reduced further than needed; magenta: slight magenta shift is visible",
    ]
    expect_rejected(m, missing_structure, eligible, "notes missing structure field")

    valid = [
        (
            "candidate-00="
            "green: residual green cast remains clearly visible; "
            "magenta: no magenta or purple shift is visible; "
            "structure: faint outer emission, Pillars, and dark lanes remain preserved"
        ),
        (
            "candidate-01="
            "green: unwanted green cast is removed to a natural SHO balance; "
            "magenta: no magenta or purple over-correction is visible; "
            "structure: faint outer emission, Pillars, and dark lanes remain preserved"
        ),
        (
            "candidate-02="
            "green: green is reduced further than necessary; "
            "magenta: a slight magenta shift is visible in the nebula; "
            "structure: faint outer emission and dark-lane structure remain visible"
        ),
    ]

    parsed = m.parse_candidate_notes(valid, eligible)
    if set(parsed) != set(eligible):
        raise RuntimeError("Valid structured note set did not cover every candidate")

    print(json.dumps({
        "status": "success",
        "helper_version": m.VERSION,
        "v1_0_2_vague_notes_rejected": True,
        "missing_field_notes_rejected": True,
        "structured_notes_accepted": True,
        "required_fields": ["green", "magenta", "structure"],
    }, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
