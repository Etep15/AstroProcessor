#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib.util, json, sys, tempfile
from dataclasses import asdict
from pathlib import Path
import numpy as np
from astropy.io import fits

def load(path: Path):
    spec=importlib.util.spec_from_file_location("sho_cb_native_contract",path)
    m=importlib.util.module_from_spec(spec); sys.modules[spec.name]=m; spec.loader.exec_module(m); return m

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--helper",required=True,type=Path); a=ap.parse_args(); m=load(a.helper)
    assert m.VERSION=="1.1.0"
    assert m.REQUIRED_STARNET_SOURCE_CONTRACT_REVISION=="native-starnet-channel-balance-v1"
    with tempfile.TemporaryDirectory(prefix="sho-cb-native-contract-") as td:
        ws=Path(td); name="T"; p=m.project_paths(ws,name); p["source"].parent.mkdir(parents=True)
        data=np.stack([np.full((32,32),0.02,np.float32),np.full((32,32),0.05,np.float32),np.full((32,32),0.01,np.float32)])
        fits.PrimaryHDU(data=data).writeto(p["source"]); ev=m.inspect_fits(p["source"])
        m.json_dump_atomic(p["source_review"],{"visual_review_completed":True})
        native={
            "helper_version":"1.5.2","source_contract_revision":"native-starnet-channel-balance-v1","status":"ready",
            "project":name,"project_path":str(p["project"]),
            "stage_order":{"upstream":"siril-background-neutralization","current":"siril-starnet-removal","downstream":"siril-sho-channel-balance"},
            "next_stage":"siril-sho-channel-balance","sho_channel_balance_permitted":True,"ghs_pass1_permitted":False,
            "starless_processing_permitted":True,"starless_background_processing_permitted":False,"visual_review_completed":True,
            "linear_starless":asdict(ev),"visual_review":{"record_path":str(p["source_review"]),"record_sha256":m.sha256_file(p["source_review"])},
        }
        m.json_dump_atomic(p["source_manifest"],native)
        _, got, summary=m.validate_upstream(ws,name)
        assert got.sha256==ev.sha256 and summary["contract_mode"]=="native-starnet-channel-balance-v1"
        plan=m.advance_stage(workspace=ws,project_name=name,timeout_seconds=30,plan_only=True)
        assert plan["status"]=="would_generate_baseline"
        assert plan["source_is_starless"] is True and plan["stars_layer_modified"] is False
        legacy=dict(native)
        legacy.pop("source_contract_revision",None); legacy.pop("sho_channel_balance_permitted",None)
        legacy["stage_order"]={"upstream":"siril-background-neutralization","current":"siril-starnet-removal","downstream":"siril-ghs-stretch-pass1"}
        legacy["next_stage"]="siril-ghs-stretch-pass1"; legacy["ghs_pass1_permitted"]=True
        m.json_dump_atomic(p["source_manifest"],legacy)
        try: m.validate_upstream(ws,name)
        except Exception: legacy_rejected=True
        else: legacy_rejected=False
        assert legacy_rejected
    print(json.dumps({
        "status":"success","helper_version":m.VERSION,"source_contract_revision":"native-starnet-channel-balance-v1",
        "native_contract_accepted":True,"legacy_direct_ghs_rejected":True,"post_starnet_starless_only":True,
        "stars_layer_modified":False,"channel_downstream":"siril-ghs-stretch-pass1",
    },indent=2,sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
