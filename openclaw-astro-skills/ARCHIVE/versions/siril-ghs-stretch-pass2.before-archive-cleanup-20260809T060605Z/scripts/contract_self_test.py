#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, importlib.util, json, sys, tempfile
from pathlib import Path
import numpy as np
from astropy.io import fits

def load(path: Path):
    name = "ghs_pass2_contract"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load helper.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

def sha(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for c in iter(lambda:f.read(1024*1024),b""):
            h.update(c)
    return h.hexdigest()

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--helper",required=True,type=Path)
    a=p.parse_args()
    g=load(a.helper.resolve())
    if g.VERSION != "1.2.0":
        raise RuntimeError(f"Expected 1.2.0, got {g.VERSION}")

    with tempfile.TemporaryDirectory(prefix="ghs-pass2-contract-") as td:
        ws=Path(td)
        project=ws/"Projects"/"Synthetic"
        upstream=project/"processing"/"ghs-pass1"
        upstream.mkdir(parents=True)
        src=upstream/"SHO-starless-ghs-pass1.fit"
        data=np.full((3,64,64),0.09,dtype=np.float32)
        fits.PrimaryHDU(data).writeto(src)
        evidence=g.inspect_fits(src)
        manifest={
            "project":"Synthetic",
            "project_path":str(project),
            "status":"ready",
            "helper_version":"1.3.1",
            "visual_review_completed":True,
            "ghs_pass2_processing_permitted":True,
            "stage_order":{
                "upstream":"siril-starnet-removal",
                "current":"siril-ghs-stretch-pass1",
                "downstream":"siril-ghs-stretch-pass2",
            },
            "output":{
                "path":str(src),
                "sha256":evidence.sha256,
            },
        }
        (upstream/"ghs-pass1-manifest.json").write_text(
            json.dumps(manifest),encoding="utf-8"
        )
        paths=g.project_paths(ws,"Synthetic")
        m,e=g.validate_source(paths)
        if e.sha256 != evidence.sha256:
            raise RuntimeError("Positive upstream contract failed.")

        manifest["ghs_pass2_processing_permitted"]=False
        (upstream/"ghs-pass1-manifest.json").write_text(
            json.dumps(manifest),encoding="utf-8"
        )
        blocked=False
        try:
            g.validate_source(paths)
        except Exception:
            blocked=True
        if not blocked:
            raise RuntimeError("Negative permission contract failed.")

    print(json.dumps({
        "status":"success",
        "helper_version":g.VERSION,
        "required_upstream_helper":"1.3.1",
        "positive_contract_test":True,
        "permission_negative_test":True,
        "next_stage":"siril-black-point",
    },indent=2,sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
