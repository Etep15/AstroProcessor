#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib.util, json, sys
from pathlib import Path

CANONICAL_ROOT = "/home/peter/.openclaw/workspace/agents/codewarrior/Projects"
WRONG_ROOT = "/home/peter/.openclaw/workspace/agents/codewarrior/AstroProcessor/Projects"

def load(path: Path):
    spec = importlib.util.spec_from_file_location("sho_channel_balance_routing", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--helper", required=True, type=Path)
    ap.add_argument("--skill", required=True, type=Path)
    args = ap.parse_args()

    m = load(args.helper)
    if m.VERSION != "1.1.0":
        raise RuntimeError(f"Expected helper 1.1.0, got {m.VERSION}")

    skill = args.skill.read_text(encoding="utf-8")
    required_skill_text = [
        "Stage-only routing contract — highest priority",
        CANONICAL_ROOT,
        "invoke `astroproc` for any reason",
        "inspect `/mnt/asiair`",
        "Do **not** use or inspect:",
        WRONG_ROOT,
        "sho-channel-balance advance --project",
    ]
    missing = [item for item in required_skill_text if item not in skill]
    if missing:
        raise RuntimeError(f"Stage routing contract missing text: {missing}")

    paths = m.project_paths(
        Path("/home/peter/.openclaw/workspace/agents/codewarrior"),
        "M16 July 2026",
    )
    expected = Path(CANONICAL_ROOT) / "M16 July 2026"
    if paths["project"] != expected:
        raise RuntimeError(f"Helper resolved wrong canonical project: {paths['project']}")
    if WRONG_ROOT in str(paths["project"]):
        raise RuntimeError("Helper resolved the forbidden AstroProcessor/Projects root")

    print(json.dumps({
        "status": "success",
        "helper_version": m.VERSION,
        "canonical_projects_root": CANONICAL_ROOT,
        "m16_project": str(paths["project"]),
        "alternate_astroprocessor_projects_root_forbidden": True,
        "astroproc_forbidden_for_stage_request": True,
        "asiair_discovery_forbidden_for_stage_request": True,
    }, indent=2, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
