#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib.util, json, sys
from pathlib import Path
def load(path):
    s=importlib.util.spec_from_file_location("green_reduction_policy",path); m=importlib.util.module_from_spec(s); sys.modules[s.name]=m; s.loader.exec_module(m); return m
def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--helper",required=True,type=Path); a=ap.parse_args(); m=load(a.helper)
    if m.VERSION!="1.0.2": raise RuntimeError(f"Expected helper 1.0.2, got {m.VERSION}")
    script=m.green_reduction_script_text()
    expected=["rmgreen 2 0.100","rmgreen 2 0.150","rmgreen 2 0.200"]
    missing=[cmd for cmd in expected if cmd not in script]
    if missing: raise RuntimeError(f"Generated Siril script lost required rmgreen commands: {missing}")
    if "-nopreserve" in script: raise RuntimeError("Generated Siril script disables Preserve Lightness")
    if m.CANDIDATE_AMOUNTS!={"candidate-00":0.10,"candidate-01":0.15,"candidate-02":0.20}: raise RuntimeError("Candidate amounts changed")
    if m.RM_GREEN_TYPE!=2 or m.PRESERVE_LIGHTNESS is not True: raise RuntimeError("Maximum Mask/preserve-lightness policy changed")
    fake=[{"candidate":n,"quality_assessment":{"satisfactory":True}} for n in m.CANDIDATE_AMOUNTS]
    gate=m.publication_gate(fake)
    if gate.get("recommended_candidate")!="candidate-01": raise RuntimeError(f"Expected candidate-01 baseline, got {gate}")
    try: m.validate_assertive_override_reason(None)
    except Exception: pass
    else: raise RuntimeError("Missing candidate-02 override did not fail closed")
    valid="Residual green remains clearly visible at lower amounts; candidate-02 removes that green without magenta or purple and preserves faint nebular structure."
    m.validate_assertive_override_reason(valid)
    print(json.dumps({"status":"success","helper_version":m.VERSION,"candidate_amounts":m.CANDIDATE_AMOUNTS,"manual_baseline_candidate":"candidate-01","assertive_override_required":True},indent=2,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
