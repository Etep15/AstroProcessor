#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, importlib.util, json, sys
from pathlib import Path
import numpy as np
from astropy.io import fits

EXPECTED_SOURCE_SHA="f03d50e2bc94cbaaa929fb5c40ca2a91a99e152ffc8d74fb0a1645a311119b78"
EXPECTED_PASS1_MANIFEST_SHA="60889c708901ec530c9a639e5318b42a6174169e02f6a68b4c4418ff38468a0b"
PROJECT="M16 July 2026"
STRIDE=4

def load(path):
    name="ghs_pass2_probe"
    spec=importlib.util.spec_from_file_location(name,path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot import pass-2 helper.")
    m=importlib.util.module_from_spec(spec)
    sys.modules[name]=m
    spec.loader.exec_module(m)
    return m

def digest(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for c in iter(lambda:f.read(1024*1024),b""):
            h.update(c)
    return h.hexdigest()

def snapshot(root):
    if not root.exists():
        return {}
    return {
        str(p.relative_to(root)):digest(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--helper",required=True,type=Path)
    p.add_argument("--workspace",required=True,type=Path)
    p.add_argument("--timeout",type=int,default=1800)
    a=p.parse_args()
    g=load(a.helper.resolve())
    if g.VERSION!="1.2.0":
        raise RuntimeError(f"Expected 1.2.0, got {g.VERSION}")

    paths=g.project_paths(a.workspace.resolve(),PROJECT)
    before=snapshot(paths["stable"])
    manifest,evidence=g.validate_source(paths)
    if evidence.sha256!=EXPECTED_SOURCE_SHA:
        raise RuntimeError(
            f"Pass-1 source changed: {evidence.sha256}"
        )
    actual_manifest_sha=digest(paths["source_manifest"])
    if actual_manifest_sha!=EXPECTED_PASS1_MANIFEST_SHA:
        raise RuntimeError(
            "Published pass-1 manifest differs from the reviewed 1.3.1 "
            f"manifest: {actual_manifest_sha}"
        )

    root=(
        a.workspace.resolve()/".skill-self-tests"/
        "siril-ghs-stretch-pass2-m16-probe"/g.unique_id()
    )
    root.mkdir(parents=True,exist_ok=False)
    probe=root/"M16-ghs-pass1-stride4.fit"

    with fits.open(paths["source"],memmap=True) as hdul:
        data=np.asarray(hdul[0].data)
        header=hdul[0].header.copy()
        sampled=np.asarray(data[:,::STRIDE,::STRIDE],dtype=np.float32)
    fits.PrimaryHDU(data=sampled,header=header).writeto(probe)

    source_median=float(g.inspect_fits(probe).median)
    p0=g.normalize_parameters({
        "D":1.40,"B":3.00,
        "SP":max(0.040,min(0.180,source_median)),
        "LP":0.0,"HP":0.950,
    })
    c0=g.execute_candidate(
        probe,root,a.timeout,candidate_index=0,parameters=p0,
        adaptation_reason="M16 pass-2 installation probe baseline."
    )
    p1,r1=g.plan_second_candidate(c0)
    c1=g.execute_candidate(
        probe,root,a.timeout,candidate_index=1,parameters=p1,
        adaptation_reason=r1
    )
    p2,r2=g.plan_third_candidate(c0,c1)
    c2=g.execute_candidate(
        probe,root,a.timeout,candidate_index=2,parameters=p2,
        adaptation_reason=r2
    )
    candidates=[c0,c1,c2]

    for candidate in candidates:
        provenance = candidate.get("preview_provenance", {})
        if provenance.get("before_source_fits_sha256") != candidate["source"]["sha256"]:
            raise RuntimeError(
                f"{candidate['candidate']} before-preview source checksum "
                "does not match pass-1 source."
            )
        if provenance.get("after_source_fits_sha256") != candidate["output"]["sha256"]:
            raise RuntimeError(
                f"{candidate['candidate']} after-preview source checksum "
                "does not match pass-2 output."
            )
        if provenance.get("before_after_pngs_distinct") is not True:
            raise RuntimeError(
                f"{candidate['candidate']} before/after preview images are "
                "not distinct."
            )

    gate=g.publication_gate(candidates)
    eligible=[
        c for c in candidates if g.candidate_publication_eligible(c)
    ]
    if not gate["publication_permitted"] or not eligible:
        raise RuntimeError(
            f"Pass-2 M16 probe did not produce an eligible candidate: {gate}"
        )
    rec=g.recommended_candidate(candidates)
    if rec is None:
        raise RuntimeError("No recommended pass-2 candidate.")

    rm=rec["quality_assessment"]["metrics"]
    median=float(rm["output_luma_median"])
    if not (0.135 <= median <= 0.225):
        raise RuntimeError(
            f"Recommended pass-2 median {median:.6f} outside 0.135–0.225."
        )
    if float(rm["high_clip_fraction"])>0 or float(rm["low_clip_fraction"])>0:
        raise RuntimeError("Recommended pass-2 candidate clips.")
    if float(rm["output_maximum"])>0.97:
        raise RuntimeError("Recommended pass-2 candidate exceeds max-value gate.")

    after=snapshot(paths["stable"])
    if before!=after:
        raise RuntimeError(
            "Canonical processing/ghs-pass2 changed during probe."
        )

    compact=[]
    for c in candidates:
        m=c["quality_assessment"]["metrics"]
        compact.append({
            "candidate":c["candidate"],
            "parameters":c["parameters"],
            "classification":c["histogram_classification"],
            "median":m["output_luma_median"],
            "p90":m["output_luma_p90"],
            "p99":m["output_luma_p99"],
            "maximum":m["output_maximum"],
            "correlation":m["luma_correlation"],
            "selection_score":c["selection_score"],
        })

    print(json.dumps({
        "status":"success",
        "helper_version":g.VERSION,
        "source_sha256":evidence.sha256,
        "source_manifest_sha256":actual_manifest_sha,
        "probe_stride":STRIDE,
        "probe_shape":list(sampled.shape),
        "candidates":compact,
        "publication_gate":gate,
        "recommended_candidate":rec["candidate"],
        "recommended_median":median,
        "canonical_ghs_pass2_unchanged":True,
        "probe_directory":str(root),
    },indent=2,sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
