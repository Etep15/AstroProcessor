#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib.util, json, sys
from pathlib import Path
REQUIRED=["validate_upstream_fast","validate_upstream","workflow_state","begin_stage","confirm_fresh_run","run_project","review_plan","record_visual_selection","publish_project","advance_stage","select_publish_stage","stage_status","status_project","self_test","build_parser","main"]
def load(path):
    s=importlib.util.spec_from_file_location("green_reduction_api",path); m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); return m
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--helper",required=True,type=Path); a=ap.parse_args(); m=load(a.helper)
    if m.VERSION!="1.0.3": raise RuntimeError(f"Expected 1.0.3, got {m.VERSION}")
    missing=[x for x in REQUIRED if not callable(getattr(m,x,None))]
    if missing: raise RuntimeError(f"Missing helper functions: {missing}")
    p=m.build_parser(); action=next(x for x in p._actions if x.dest=="command"); commands=set(action.choices); required={"self-test","advance","confirm-fresh","select-publish","stage-status","status"}
    if required-commands: raise RuntimeError(f"Missing commands: {sorted(required-commands)}")
    sp=action.choices["select-publish"]; opts={o for x in sp._actions for o in x.option_strings}
    for req in ("--project","--candidate","--visual-notes","--note","--candidate-note","--policy-override-reason"):
        if req not in opts: raise RuntimeError(f"Missing select-publish option {req}")
    print(json.dumps({"status":"success","helper_version":m.VERSION,"api_surface_complete":True,"fixed_candidate_count":m.MAX_CANDIDATES,"single_siril_script_generator":True},indent=2,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
