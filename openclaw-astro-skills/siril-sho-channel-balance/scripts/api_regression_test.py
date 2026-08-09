#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib.util, json, sys
from pathlib import Path

REQUIRED = [
    "validate_upstream",
    "begin_stage",
    "confirm_fresh_run",
    "advance_stage",
    "review_refine_stage",
    "selection_review_plan",
    "select_publish_stage",
    "canonical_status",
    "status_project",
    "self_test",
    "build_parser",
    "main",
]

def load(path: Path):
    spec = importlib.util.spec_from_file_location("sho_channel_balance_api", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--helper", required=True, type=Path)
    args = ap.parse_args()
    m = load(args.helper)
    if m.VERSION != "1.1.0":
        raise RuntimeError(f"Expected 1.1.0, got {m.VERSION}")
    missing = [name for name in REQUIRED if not callable(getattr(m, name, None))]
    if missing:
        raise RuntimeError(f"Missing helper API: {missing}")
    parser = m.build_parser()
    action = next(a for a in parser._actions if a.dest == "command")
    commands = set(action.choices)
    required_commands = {
        "self-test",
        "advance",
        "confirm-fresh",
        "review-refine",
        "select-publish",
        "stage-status",
        "status",
    }
    if required_commands - commands:
        raise RuntimeError(f"Missing CLI commands: {sorted(required_commands - commands)}")
    if "fresh_run_confirmed" not in m.CLI_SUCCESS_STATUSES:
        raise RuntimeError("fresh-run confirmation is not a successful CLI status")
    print(json.dumps({
        "status": "success",
        "helper_version": m.VERSION,
        "api_surface_complete": True,
        "commands": sorted(required_commands),
    }, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
