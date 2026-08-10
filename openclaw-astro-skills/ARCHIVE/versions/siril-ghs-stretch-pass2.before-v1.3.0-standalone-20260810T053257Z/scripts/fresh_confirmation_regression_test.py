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
    name = "ghs_pass2_fresh_confirmation_120"
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
    old = sys.argv[:]
    stdout = io.StringIO()
    try:
        sys.argv = ["ghs_pass2.py", *argv]
        with contextlib.redirect_stdout(stdout):
            code = ghs.main()
    finally:
        sys.argv = old
    text = stdout.getvalue().strip()
    if not text:
        raise RuntimeError(f"No JSON output for argv={argv!r}")
    return int(code), json.loads(text)


def select_and_publish(ghs, workspace: Path, project_name: str, run: dict):
    eligible = list(run["publication_eligible_candidates"])
    recommended = run["recommended_candidate"]
    selected = ghs.record_visual_selection(
        workspace=workspace,
        project_name=project_name,
        run_root=Path(run["run_root"]),
        candidate_name=recommended,
        compared_candidates=eligible,
        visual_selection_notes=(
            "Synthetic confirmation regression compared every eligible "
            "candidate and selected the balanced recommendation."
        ),
    )
    if selected.get("status") != "ready_to_publish":
        raise RuntimeError(f"Selection failed: {selected}")

    published = ghs.publish_project(
        workspace=workspace,
        project_name=project_name,
        run_root=Path(run["run_root"]),
        candidate_name=None,
        visual_selection_notes=None,
        fresh_run=True,
    )
    if published.get("status") != "ready":
        raise RuntimeError(f"Publication failed: {published}")
    return published


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

    root = (
        args.workspace.resolve()
        / ".skill-self-tests"
        / "siril-ghs-stretch-pass2-fresh-confirmation"
        / ghs.unique_id()
    )
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=False)
    project_name = "Synthetic Fresh Confirmation"
    write_upstream(ghs, workspace, project_name)

    first_begin = ghs.begin_stage(workspace, project_name)
    if first_begin.get("status") != "start_new_run":
        raise RuntimeError(f"Initial begin invalid: {first_begin}")
    if first_begin.get("confirmation_required") is not False:
        raise RuntimeError("Initial run incorrectly requires confirmation.")

    first = ghs.run_project(
        workspace=workspace,
        project_name=project_name,
        timeout_seconds=args.timeout,
        fresh_run=False,
        max_candidates=3,
    )
    if first.get("publication_permitted") is not True:
        raise RuntimeError(f"Initial run not publishable: {first}")
    select_and_publish(ghs, workspace, project_name, first)

    paths = ghs.project_paths(workspace, project_name)
    canonical_sha = ghs.sha256_file(paths["stable_output"])

    ghs.WORKSPACE = workspace
    code, ask = call_main(
        ghs,
        ["begin", "--project", project_name],
    )
    if code != 0:
        raise RuntimeError(f"confirmation_required returned exit {code}")
    if ask.get("status") != "confirmation_required":
        raise RuntimeError(f"Expected confirmation_required: {ask}")
    if ask.get("action") != "confirm_fresh_run":
        raise RuntimeError(f"Unexpected confirmation action: {ask}")
    if ask.get("confirmation_required") is not True:
        raise RuntimeError("Confirmation flag not set.")
    if "Do you want me to run it again as a fresh run?" not in ask.get(
        "question", ""
    ):
        raise RuntimeError(f"Unexpected question: {ask.get('question')!r}")

    state_after_ask = ghs.workflow_state(workspace, project_name)
    if state_after_ask.get("action") != "run_review_select_publish":
        raise RuntimeError(
            f"Question unexpectedly created an incomplete run: {state_after_ask}"
        )
    if ghs.sha256_file(paths["stable_output"]) != canonical_sha:
        raise RuntimeError("Confirmation question changed canonical output.")

    code, blocked_bare = call_main(
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
    if code == 0 or blocked_bare.get("status") != "blocked":
        raise RuntimeError(
            f"Bare run bypassed confirmation: {blocked_bare}"
        )

    code, blocked_flag = call_main(
        ghs,
        [
            "run",
            "--project",
            project_name,
            "--fresh-run",
            "--max-candidates",
            "3",
            "--timeout",
            str(args.timeout),
        ],
    )
    if code == 0 or blocked_flag.get("status") != "blocked":
        raise RuntimeError(
            f"--fresh-run bypassed confirmation: {blocked_flag}"
        )

    code, confirmed = call_main(
        ghs,
        ["confirm-fresh", "--project", project_name],
    )
    if code != 0:
        raise RuntimeError(f"confirm-fresh returned exit {code}")
    if confirmed.get("status") != "fresh_run_authorized":
        raise RuntimeError(f"Confirmation not authorized: {confirmed}")
    request_id = confirmed.get("fresh_run_request_id")
    if not request_id:
        raise RuntimeError("Confirmation request id missing.")

    resumed = ghs.begin_stage(workspace, project_name)
    if resumed.get("status") != "fresh_run_authorized":
        raise RuntimeError(
            f"Durable authorization was not recovered: {resumed}"
        )
    if resumed.get("fresh_run_request_id") != request_id:
        raise RuntimeError("Recovered authorization id changed.")
    if resumed.get("confirmation_required") is not False:
        raise RuntimeError("User would be asked to confirm twice.")

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
        raise RuntimeError(f"Authorized run returned exit {code}: {second}")
    if second.get("status") != "awaiting_visual_selection":
        raise RuntimeError(f"Authorized run failed: {second}")
    if second.get("fresh_run_requested") is not False:
        raise RuntimeError("Regression accidentally supplied --fresh-run.")
    if second.get("fresh_run_effective") is not True:
        raise RuntimeError("Confirmed rerun was not effective.")
    if second.get("fresh_run_authorized") is not True:
        raise RuntimeError("Run did not record authorization.")
    if second.get("fresh_run_request_id") != request_id:
        raise RuntimeError("Run did not consume the right authorization.")
    if ghs.sha256_file(paths["stable_output"]) != canonical_sha:
        raise RuntimeError(
            "Candidate generation changed canonical output before publish."
        )

    begin_incomplete = ghs.begin_stage(workspace, project_name)
    if begin_incomplete.get("action") != "review_select_publish":
        raise RuntimeError(
            f"Incomplete run not resumed: {begin_incomplete}"
        )
    if begin_incomplete.get("confirmation_required") is not False:
        raise RuntimeError("Incomplete run incorrectly asks for fresh confirm.")

    select_and_publish(ghs, workspace, project_name, second)

    final = ghs.status_project(workspace, project_name)
    if final.get("status") != "ready":
        raise RuntimeError(f"Final canonical not ready: {final}")

    third_begin = ghs.begin_stage(workspace, project_name)
    if third_begin.get("status") != "confirmation_required":
        raise RuntimeError(
            f"Consumed confirmation leaked into later rerun: {third_begin}"
        )
    if third_begin.get("fresh_run_request_id") == request_id:
        raise RuntimeError("New rerun reused consumed confirmation request.")

    print(
        json.dumps(
            {
                "status": "success",
                "helper_version": ghs.VERSION,
                "initial_begin": first_begin["status"],
                "completed_begin": ask["status"],
                "bare_run_before_confirmation": blocked_bare["status"],
                "fresh_flag_before_confirmation": blocked_flag["status"],
                "confirmed_status": confirmed["status"],
                "authorization_survived_interruption": True,
                "authorized_run_status": second["status"],
                "canonical_unchanged_during_candidate_generation": True,
                "incomplete_run_resume_action": (
                    begin_incomplete["action"]
                ),
                "new_rerun_requires_new_confirmation": True,
                "test_root": str(root),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
