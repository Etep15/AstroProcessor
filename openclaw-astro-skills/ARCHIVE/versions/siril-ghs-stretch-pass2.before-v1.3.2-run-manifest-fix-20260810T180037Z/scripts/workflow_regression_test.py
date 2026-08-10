#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
from astropy.io import fits


def load(path: Path):
    name = "ghs_pass2_workflow_110"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not import staged helper.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_upstream(ghs, workspace: Path, project_name: str):
    project = workspace / "Projects" / project_name
    upstream = project / "processing" / "ghs-pass1"
    upstream.mkdir(parents=True, exist_ok=False)

    yy, xx = np.mgrid[0:256, 0:256]
    glow = np.exp(
        -(
            ((xx - 134.0) / 72.0) ** 2
            + ((yy - 124.0) / 62.0) ** 2
        )
    )
    texture = 0.004 * np.sin(xx / 11.0) * np.cos(yy / 13.0)
    base = 0.082 + 0.033 * glow + texture
    data = np.stack(
        (base * 0.95, base * 1.05, base),
        axis=0,
    ).astype(np.float32)

    source = upstream / "SHO-starless-ghs-pass1.fit"
    hdu = fits.PrimaryHDU(data)
    hdu.header["FILTER"] = "mixed_Starless"
    hdu.writeto(source)

    evidence = ghs.inspect_fits(source)
    manifest = {
        "schema_version": 3,
        "project": project_name,
        "project_path": str(project),
        "status": "ready",
        "helper_version": "1.3.1",
        "visual_review_completed": True,
        "ghs_pass2_processing_permitted": True,
        "next_stage": "siril-ghs-stretch-pass2",
        "stage_order": {
            "upstream": "siril-starnet-removal",
            "current": "siril-ghs-stretch-pass1",
            "downstream": "siril-ghs-stretch-pass2",
        },
        "output": {
            "path": str(source),
            "sha256": evidence.sha256,
        },
    }
    manifest_path = upstream / "ghs-pass1-manifest.json"
    ghs.json_dump_atomic(manifest_path, manifest)
    return project


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    ghs = load(args.helper.resolve())
    if ghs.VERSION != "1.2.0":
        raise RuntimeError(
            f"Expected helper 1.2.0, got {ghs.VERSION!r}"
        )

    test_root = (
        args.workspace.resolve()
        / ".skill-self-tests"
        / "siril-ghs-stretch-pass2-single-invocation"
        / ghs.unique_id()
    )
    synthetic_workspace = test_root / "workspace"
    synthetic_workspace.mkdir(parents=True, exist_ok=False)
    project_name = "Synthetic Single Invocation"
    write_upstream(ghs, synthetic_workspace, project_name)

    initial = ghs.workflow_state(
        synthetic_workspace,
        project_name,
    )
    if initial.get("action") != "run_review_select_publish":
        raise RuntimeError(f"Initial workflow state invalid: {initial}")

    run = ghs.run_project(
        workspace=synthetic_workspace,
        project_name=project_name,
        timeout_seconds=args.timeout,
        fresh_run=True,
        max_candidates=3,
    )
    if run.get("publication_permitted") is not True:
        raise RuntimeError(f"Run did not open publication gate: {run}")

    # Simulate an OpenClaw turn dying immediately after candidate generation.
    resumed_after_run = ghs.workflow_state(
        synthetic_workspace,
        project_name,
    )
    if resumed_after_run.get("action") != "review_select_publish":
        raise RuntimeError(
            "Workflow did not resume at visual selection after interrupted "
            f"candidate generation: {resumed_after_run}"
        )
    if resumed_after_run.get("run_root") != run.get("run_root"):
        raise RuntimeError("Workflow tried to create/choose a different run.")

    eligible = list(run["publication_eligible_candidates"])
    recommended = run["recommended_candidate"]
    if recommended not in eligible:
        raise RuntimeError("Recommended candidate is not eligible.")

    selection = ghs.record_visual_selection(
        workspace=synthetic_workspace,
        project_name=project_name,
        run_root=Path(run["run_root"]),
        candidate_name=recommended,
        compared_candidates=eligible,
        visual_selection_notes=(
            "Synthetic CodeWarrior review compared every eligible candidate "
            "at the same display scale and selected the recommended balanced "
            "candidate with preserved structure and highlight headroom."
        ),
    )
    if selection.get("status") != "ready_to_publish":
        raise RuntimeError(f"Selection was not persisted: {selection}")

    # Simulate the agent dying after review but before publish.
    resumed_after_selection = ghs.workflow_state(
        synthetic_workspace,
        project_name,
    )
    if resumed_after_selection.get("action") != "publish_recorded_selection":
        raise RuntimeError(
            "Workflow did not resume at durable publication after an "
            f"interrupted post-review turn: {resumed_after_selection}"
        )
    if resumed_after_selection.get("selected_candidate") != recommended:
        raise RuntimeError("Durable selected candidate was not recovered.")

    # Simulate an earlier failed publication staging area. It must be
    # preserved automatically by publish().
    run_root = Path(run["run_root"])
    stale = run_root / "publish-staging"
    stale.mkdir(parents=True, exist_ok=False)
    (stale / "failure-marker.txt").write_text(
        "preserve synthetic failed publication\n",
        encoding="utf-8",
    )

    # No candidate or notes supplied here: v1.2.0 must use the durable
    # selection record, proving no second recovery prompt is required.
    published = ghs.publish_project(
        workspace=synthetic_workspace,
        project_name=project_name,
        run_root=run_root,
        candidate_name=None,
        visual_selection_notes=None,
        fresh_run=True,
    )
    if published.get("status") != "ready":
        raise RuntimeError(f"Publication failed: {published}")
    if published.get("selected_candidate") != recommended:
        raise RuntimeError("Publication did not use durable selection.")
    preserved = published.get("failed_publish_staging_preserved_at")
    if not preserved or not Path(preserved).is_dir():
        raise RuntimeError("Failed publish staging was not preserved.")
    if not (Path(preserved) / "failure-marker.txt").is_file():
        raise RuntimeError("Preserved failure evidence is incomplete.")

    final = ghs.status_project(
        synthetic_workspace,
        project_name,
    )
    if final.get("status") != "ready":
        raise RuntimeError(f"Final status not ready: {final}")
    if final.get("black_point_processing_permitted") is not True:
        raise RuntimeError("Black-point permission missing after publication.")

    # Once the run is complete, a future explicit stage invocation can start
    # a new full run, while status remains ready until that new publish.
    next_invocation = ghs.workflow_state(
        synthetic_workspace,
        project_name,
    )
    if next_invocation.get("action") != "run_review_select_publish":
        raise RuntimeError(
            f"Completed workflow did not reset cleanly: {next_invocation}"
        )
    if (
        next_invocation.get("canonical_status", {}).get("status")
        != "ready"
    ):
        raise RuntimeError(
            "Completed canonical result was not preserved as ready."
        )

    print(
        json.dumps(
            {
                "status": "success",
                "helper_version": ghs.VERSION,
                "initial_action": initial["action"],
                "after_run_action": resumed_after_run["action"],
                "after_selection_action": (
                    resumed_after_selection["action"]
                ),
                "selected_candidate": recommended,
                "failed_publish_staging_preserved_at": preserved,
                "final_status": final["status"],
                "black_point_processing_permitted": True,
                "next_invocation_action": next_invocation["action"],
                "test_root": str(test_root),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
