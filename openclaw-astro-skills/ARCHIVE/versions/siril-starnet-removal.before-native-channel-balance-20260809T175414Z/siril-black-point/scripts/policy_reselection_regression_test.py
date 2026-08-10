#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path


def load(path: Path):
    spec = importlib.util.spec_from_file_location(
        "black_point_reselection_104",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not import staged helper.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path: Path, data: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return sha(path)


def candidate(
    *,
    run: Path,
    name: str,
    source_sha: str,
    before_sha: str,
    clip: float,
    score: float,
    bp_value: float,
):
    preview = run / name / "previews"
    work = run / name / "work"
    preview.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    before = preview / "SHO-starless-ghs-pass2-before-black-point.png"
    before.write_bytes(b"same-before")
    after = preview / "SHO-starless-black-point-linear.png"
    after.write_bytes(("after-" + name).encode())
    output = work / "SHO-starless-black-point.fit"
    output.write_bytes(("output-" + name).encode())

    return {
        "candidate": name,
        "histogram_classification": "balanced",
        "selection_score": score,
        "parameters": {"BP": bp_value},
        "quality_assessment": {
            "satisfactory": True,
            "metrics": {
                "channel_low_clip_fraction": clip,
                "low_luma_clip_fraction": 0.0,
                "output_luma_p001": (
                    0.0083 if name == "candidate-00" else 0.0054
                ),
                "output_luma_median": (
                    0.0199 if name == "candidate-00" else 0.0165
                ),
            },
        },
        "preview_provenance": {
            "before_png_sha256": before_sha,
            "after_png_sha256": sha(after),
            "before_source_fits_sha256": source_sha,
        },
        "previews": {
            "before_linear": str(before),
            "after_linear": str(after),
        },
        "output": {
            "path": str(output),
            "sha256": sha(output),
            "size": output.stat().st_size,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    args = parser.parse_args()

    bp = load(args.helper.resolve())

    test_root = (
        args.workspace.resolve()
        / ".skill-self-tests"
        / "siril-black-point-reselection-v104"
        / bp.unique_id()
    )
    workspace = test_root / "workspace"
    project_name = "Synthetic Policy Reselection"
    project = workspace / "Projects" / project_name

    upstream = project / "processing" / "ghs-pass2"
    stable = project / "processing" / "black-point"
    runs = project / ".siril-black-point"
    upstream.mkdir(parents=True, exist_ok=True)
    stable.mkdir(parents=True, exist_ok=True)
    runs.mkdir(parents=True, exist_ok=True)

    upstream_fit = upstream / "SHO-starless-ghs-pass2.fit"
    source_sha = write(upstream_fit, b"synthetic-upstream")
    upstream_manifest = {
        "helper_version": "1.2.0",
        "status": "ready",
        "visual_review_completed": True,
        "black_point_processing_permitted": True,
        "next_stage": "siril-black-point",
        "quality_assessment": {"satisfactory": True},
        "output": {
            "path": str(upstream_fit),
            "sha256": source_sha,
            "size": upstream_fit.stat().st_size,
        },
    }
    (
        upstream / "ghs-pass2-manifest.json"
    ).write_text(
        json.dumps(upstream_manifest),
        encoding="utf-8",
    )

    stable_fit = stable / "SHO-starless-black-point.fit"
    stable_sha = write(stable_fit, b"v103-canonical")
    write(
        stable / "SHO-starless-ghs-pass2-before-black-point.png",
        b"old-before",
    )
    write(
        stable / "SHO-starless-black-point-linear.png",
        b"old-after",
    )

    run = runs / "black-point-existing-v102"
    run.mkdir()

    before_template = (
        run
        / "candidate-00"
        / "previews"
        / "SHO-starless-ghs-pass2-before-black-point.png"
    )
    before_template.parent.mkdir(parents=True)
    before_sha = write(before_template, b"same-before")

    c0 = candidate(
        run=run,
        name="candidate-00",
        source_sha=source_sha,
        before_sha=before_sha,
        clip=0.0015819817589420076,
        score=0.8986683762804781,
        bp_value=0.16458934,
    )
    c1 = candidate(
        run=run,
        name="candidate-01",
        source_sha=source_sha,
        before_sha=before_sha,
        clip=0.005088861955424912,
        score=0.2428565866451849,
        bp_value=0.16752649,
    )

    # Ensure candidate-00's manually pre-created before image still matches.
    Path(c0["previews"]["before_linear"]).write_bytes(b"same-before")

    run_manifest = {
        "helper_version": "1.0.2",
        "project_name": project_name,
        "source": {"sha256": source_sha},
        "status": "ready",
        "canonical_output_changed": True,
        "publication_permitted": True,
        "publication_eligible_candidates": [
            "candidate-00",
            "candidate-01",
        ],
        "completed_candidate_count": 2,
        "candidates": [c0, c1],
        "recommended_candidate": "candidate-01",
        "selected_candidate": "candidate-01",
        "visual_review_completed": True,
        "visual_selection": {
            "completed": True,
            "selected_candidate": "candidate-01",
        },
        "published_at": "synthetic-old-publication",
    }
    (run / "run-manifest.json").write_text(
        json.dumps(run_manifest),
        encoding="utf-8",
    )

    stable_manifest = {
        "helper_version": "1.0.3",
        "status": "ready",
        "project": project_name,
        "visual_review_completed": True,
        "selected_candidate": "candidate-01",
        "run_root": str(run),
        "source": {"sha256": source_sha},
        "output": {
            "sha256": stable_sha,
            "size": stable_fit.stat().st_size,
        },
        "next_stage": "siril-green-reduction",
        "green_reduction_processing_permitted": True,
        "visual_selection": {
            "visual_review_evidence": {
                "method": "openclaw-read",
            }
        },
    }
    (stable / "black-point-manifest.json").write_text(
        json.dumps(stable_manifest),
        encoding="utf-8",
    )

    status = bp.status_project_fast(workspace, project_name)
    if status.get("status") != "needs_reselection":
        raise RuntimeError(
            f"v1.0.3 canonical not marked needs_reselection: {status}"
        )
    if status.get("green_reduction_processing_permitted") is not False:
        raise RuntimeError(
            "Outdated selection policy still permits green reduction."
        )

    plan_only = bp.advance_stage(
        workspace=workspace,
        project_name=project_name,
        timeout_seconds=1,
        max_candidates=3,
        plan_only=True,
    )
    if plan_only.get("status") != "would_prepare_policy_reselection":
        raise RuntimeError(
            f"Unexpected plan-only result: {plan_only}"
        )
    if plan_only.get("recommended_candidate") != "candidate-00":
        raise RuntimeError(
            "Plan-only did not prefer candidate-00."
        )
    if plan_only.get("numerical_recommended_candidate") != "candidate-01":
        raise RuntimeError(
            "Plan-only lost original numerical recommendation."
        )

    stable_sha_before = sha(stable_fit)

    plan = bp.advance_stage(
        workspace=workspace,
        project_name=project_name,
        timeout_seconds=1,
        max_candidates=3,
        plan_only=False,
    )
    if plan.get("status") != "visual_review_required":
        raise RuntimeError(
            f"Policy reselection did not reach visual review: {plan}"
        )
    if plan.get("generated_new_run") is True:
        raise RuntimeError(
            "Policy reselection regenerated candidates."
        )
    if plan.get("recommended_candidate") != "candidate-00":
        raise RuntimeError(
            "Review plan did not recommend preferred candidate-00."
        )

    by_name = {
        item["candidate"]: item
        for item in plan["candidates"]
    }
    if (
        by_name["candidate-00"]["selection_policy_classification"]
        != "preferred"
    ):
        raise RuntimeError("candidate-00 not marked preferred.")
    if (
        by_name["candidate-01"]["selection_policy_classification"]
        != "aggressive"
    ):
        raise RuntimeError("candidate-01 not marked aggressive.")

    if sha(stable_fit) != stable_sha_before:
        raise RuntimeError(
            "Preparing reselection changed the current canonical FITS."
        )

    backups = list(
        run.glob(
            "run-manifest-before-v1.0.4-reselection-*.json"
        )
    )
    if len(backups) != 1:
        raise RuntimeError(
            f"Expected one preserved run-manifest backup, got {backups}"
        )

    print(
        json.dumps(
            {
                "status": "success",
                "helper_version": bp.VERSION,
                "v103_canonical_status": "needs_reselection",
                "green_reduction_blocked_until_reselection": True,
                "candidate_regeneration_required": False,
                "numerical_recommendation": "candidate-01",
                "selection_policy_recommendation": "candidate-00",
                "candidate_00_classification": "preferred",
                "candidate_01_classification": "aggressive",
                "old_canonical_preserved_during_reselection_prepare": True,
                "old_run_manifest_preserved": True,
                "test_root": str(test_root),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
