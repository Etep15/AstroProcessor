#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path

import numpy as np
from astropy.io import fits


def load(path: Path):
    name = "ghs_pass2_implicit_fresh_112"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not import staged helper.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def write_upstream(ghs, workspace: Path, project_name: str) -> None:
    project = workspace / "Projects" / project_name
    upstream = project / "processing" / "ghs-pass1"
    upstream.mkdir(parents=True, exist_ok=False)

    yy, xx = np.mgrid[0:192, 0:192]
    glow = np.exp(
        -(
            ((xx - 98.0) / 54.0) ** 2
            + ((yy - 94.0) / 48.0) ** 2
        )
    )
    texture = 0.003 * np.sin(xx / 8.0) * np.cos(yy / 10.0)
    base = 0.082 + 0.032 * glow + texture
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
    ghs.json_dump_atomic(
        upstream / "ghs-pass1-manifest.json",
        manifest,
    )


def call_main(ghs, argv: list[str]) -> tuple[int, dict]:
    original = sys.argv[:]
    stdout = io.StringIO()
    try:
        sys.argv = ["ghs_pass2.py", *argv]
        with contextlib.redirect_stdout(stdout):
            code = ghs.main()
    finally:
        sys.argv = original

    text = stdout.getvalue().strip()
    if not text:
        raise RuntimeError(f"No JSON output for argv={argv!r}")
    return int(code), json.loads(text)


def select_and_publish(
    ghs,
    workspace: Path,
    project_name: str,
    run: dict,
) -> dict:
    eligible = list(run["publication_eligible_candidates"])
    recommended = run["recommended_candidate"]

    selection = ghs.record_visual_selection(
        workspace=workspace,
        project_name=project_name,
        run_root=Path(run["run_root"]),
        candidate_name=recommended,
        compared_candidates=eligible,
        visual_selection_notes=(
            "Synthetic regression compared every eligible candidate and "
            "selected the balanced recommendation."
        ),
    )
    if selection.get("status") != "ready_to_publish":
        raise RuntimeError(f"Could not persist selection: {selection}")

    published = ghs.publish_project(
        workspace=workspace,
        project_name=project_name,
        run_root=Path(run["run_root"]),
        candidate_name=None,
        visual_selection_notes=None,
        fresh_run=True,
    )
    if published.get("status") != "ready":
        raise RuntimeError(f"Could not publish baseline canonical: {published}")
    return published


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    ghs = load(args.helper.resolve())
    if ghs.VERSION != "1.1.2":
        raise RuntimeError(
            f"Expected helper 1.1.2, got {ghs.VERSION!r}"
        )

    root = (
        args.workspace.resolve()
        / ".skill-self-tests"
        / "siril-ghs-stretch-pass2-implicit-fresh-run"
        / ghs.unique_id()
    )
    synthetic_workspace = root / "workspace"
    synthetic_workspace.mkdir(parents=True, exist_ok=False)
    project_name = "Synthetic Implicit Fresh Run"
    write_upstream(ghs, synthetic_workspace, project_name)

    # First establish a genuine valid canonical pass-2 result.
    first = ghs.run_project(
        workspace=synthetic_workspace,
        project_name=project_name,
        timeout_seconds=args.timeout,
        fresh_run=True,
        max_candidates=3,
    )
    if first.get("publication_permitted") is not True:
        raise RuntimeError(f"Baseline run not publishable: {first}")
    first_published = select_and_publish(
        ghs,
        synthetic_workspace,
        project_name,
        first,
    )

    paths = ghs.project_paths(
        synthetic_workspace,
        project_name,
    )
    canonical_sha_before = ghs.sha256_file(paths["stable_output"])

    # Confirm workflow state says a new full stage may start while the
    # canonical result remains valid.
    state = ghs.workflow_state(
        synthetic_workspace,
        project_name,
    )
    if state.get("status") != "start_new_run":
        raise RuntimeError(f"Expected start_new_run, got {state}")
    if state.get("action") != "run_review_select_publish":
        raise RuntimeError(f"Unexpected action: {state}")
    if (
        state.get("canonical_status", {}).get("status")
        != "ready"
    ):
        raise RuntimeError("Existing canonical result is not ready.")

    # Reproduce the exact CodeWarrior mistake from the real test:
    # invoke CLI run WITHOUT --fresh-run even though canonical exists.
    ghs.WORKSPACE = synthetic_workspace
    code, second = call_main(
        ghs,
        [
            "run",
            "--project",
            project_name,
            "--max-candidates",
            "3",
            "--timeout",
            str(args.timeout),
        ],
    )

    if code != 0:
        raise RuntimeError(
            f"Implicit fresh run incorrectly returned exit {code}: {second}"
        )
    if second.get("status") != "awaiting_visual_selection":
        raise RuntimeError(
            f"Implicit fresh run did not generate candidates: {second}"
        )
    if second.get("fresh_run_requested") is not False:
        raise RuntimeError(
            "Regression did not actually omit --fresh-run."
        )
    if second.get("fresh_run_effective") is not True:
        raise RuntimeError(
            "Helper did not make the inferred fresh run effective."
        )
    if second.get("fresh_run_inferred") is not True:
        raise RuntimeError(
            "Helper did not record that fresh-run was inferred."
        )
    if not second.get("fresh_run_inference_reason"):
        raise RuntimeError(
            "Helper did not record fresh-run inference rationale."
        )

    # Generating new candidates must not touch the existing canonical result.
    canonical_sha_after = ghs.sha256_file(paths["stable_output"])
    if canonical_sha_after != canonical_sha_before:
        raise RuntimeError(
            "Implicit candidate run changed canonical output before publish."
        )

    # Once the second run exists, the same omitted flag must NOT create a
    # third duplicate run. It should fail closed and tell orchestration to
    # resume the existing run.
    code_again, duplicate_attempt = call_main(
        ghs,
        [
            "run",
            "--project",
            project_name,
            "--max-candidates",
            "3",
            "--timeout",
            str(args.timeout),
        ],
    )
    if code_again == 0:
        raise RuntimeError(
            "Duplicate candidate run was incorrectly allowed."
        )
    if duplicate_attempt.get("status") != "blocked":
        raise RuntimeError(
            f"Expected blocked duplicate attempt: {duplicate_attempt}"
        )
    error = str(duplicate_attempt.get("error", ""))
    if "compatible incomplete" not in error:
        raise RuntimeError(
            "Duplicate-run blocker did not explain incomplete-run resume."
        )

    resumed = ghs.workflow_state(
        synthetic_workspace,
        project_name,
    )
    if resumed.get("action") != "review_select_publish":
        raise RuntimeError(
            f"Workflow does not resume the second run: {resumed}"
        )
    if resumed.get("run_root") != second.get("run_root"):
        raise RuntimeError(
            "Workflow selected a different run after implicit generation."
        )

    print(
        json.dumps(
            {
                "status": "success",
                "helper_version": ghs.VERSION,
                "baseline_canonical_sha256": canonical_sha_before,
                "implicit_run_exit_code": code,
                "implicit_run_status": second["status"],
                "fresh_run_requested": second["fresh_run_requested"],
                "fresh_run_effective": second["fresh_run_effective"],
                "fresh_run_inferred": second["fresh_run_inferred"],
                "canonical_unchanged_during_candidate_generation": True,
                "duplicate_run_exit_code": code_again,
                "duplicate_run_status": duplicate_attempt["status"],
                "resume_action": resumed["action"],
                "test_root": str(root),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
