#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib.util, json, sys, tempfile
from dataclasses import asdict
from pathlib import Path
import numpy as np
from astropy.io import fits

def load(path: Path):
    spec=importlib.util.spec_from_file_location("sho_cb_post_starnet", path)
    m=importlib.util.module_from_spec(spec); sys.modules[spec.name]=m; spec.loader.exec_module(m); return m

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--helper",required=True,type=Path); a=ap.parse_args(); m=load(a.helper)
    assert m.VERSION=="1.1.0"
    with tempfile.TemporaryDirectory(prefix="sho-cb-contract-") as td:
        ws=Path(td); name="T"; p=m.project_paths(ws,name); p["source"].parent.mkdir(parents=True)
        data=np.stack([np.full((32,32),0.02,np.float32),np.full((32,32),0.05,np.float32),np.full((32,32),0.01,np.float32)])
        fits.PrimaryHDU(data=data).writeto(p["source"]); ev=m.inspect_fits(p["source"])
        m.json_dump_atomic(p["source_review"],{"visual_review_completed":True})
        man={"helper_version":"1.5.2","status":"ready","project":name,"project_path":str(p["project"]),"stage_order":{"upstream":"siril-background-neutralization","current":"siril-starnet-removal","downstream":"siril-ghs-stretch-pass1"},"ghs_pass1_permitted":True,"starless_processing_permitted":True,"starless_background_processing_permitted":False,"visual_review_completed":True,"linear_starless":asdict(ev),"visual_review":{"record_path":str(p["source_review"]),"record_sha256":m.sha256_file(p["source_review"])}}
        m.json_dump_atomic(p["source_manifest"],man)
        _, got, summary=m.validate_upstream(ws,name)
        assert got.sha256==ev.sha256 and summary["contract_mode"]=="temporary-starnet-1.5.2-ghs-bridge"
        plan=m.advance_stage(workspace=ws,project_name=name,timeout_seconds=30,plan_only=True)
        assert plan["source_is_starless"] is True and plan["stars_layer_modified"] is False
        assert plan["source_path"].endswith("processing/starnet/SHO-starless-linear.fit")
        assert "med(R)" in plan["baseline_formula"]["green"]
    print(json.dumps({"status":"success","helper_version":m.VERSION,"post_starnet_starless_only":True,"legacy_starnet_1_5_2_bridge":True,"stars_layer_modified":False,"next_stage":"siril-ghs-stretch-pass1"},indent=2,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
