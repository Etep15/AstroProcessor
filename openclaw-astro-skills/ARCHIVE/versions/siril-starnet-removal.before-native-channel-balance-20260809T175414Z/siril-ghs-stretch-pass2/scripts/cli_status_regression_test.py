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
    name = "ghs_pass2_cli_status_111"
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
        raise RuntimeError(f"No CLI JSON returned for argv={argv!r}")
    payload = json.loads(text)
    return int(code), payload


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
        / "siril-ghs-stretch-pass2-cli-status"
        / ghs.unique_id()
    )
    synthetic_workspace = root / "workspace"
    synthetic_workspace.mkdir(parents=True, exist_ok=False)
    project_name = "Synthetic CLI Status"
    write_upstream(ghs, synthetic_workspace, project_name)

    # Redirect the helper's CLI global workspace into the isolated synthetic
    # workspace so we exercise the real main()/argparse/return-code path.
    ghs.WORKSPACE = synthetic_workspace

    code, payload = call_main(
        ghs,
        ["workflow-state", "--project", project_name],
    )
    if payload.get("status") != "start_new_run":
        raise RuntimeError(
            f"Expected start_new_run payload, got {payload}"
        )
    if code != 0:
        raise RuntimeError(
            f"start_new_run incorrectly returned exit {code}"
        )

    run = ghs.run_project(
        workspace=synthetic_workspace,
        project_name=project_name,
        timeout_seconds=args.timeout,
        fresh_run=True,
        max_candidates=3,
    )
    if run.get("publication_permitted") is not True:
        raise RuntimeError(
            f"Synthetic run did not become publishable: {run}"
        )

    code, payload = call_main(
        ghs,
        ["workflow-state", "--project", project_name],
    )
    if payload.get("status") != "awaiting_visual_selection":
        raise RuntimeError(
            f"Expected awaiting_visual_selection, got {payload}"
        )
    if code != 0:
        raise RuntimeError(
            "awaiting_visual_selection must return exit 0"
        )

    eligible = list(run["publication_eligible_candidates"])
    recommended = run["recommended_candidate"]

    select_argv = [
        "select",
        "--project",
        project_name,
        "--run-root",
        run["run_root"],
        "--candidate",
        recommended,
    ]
    for candidate in eligible:
        select_argv.extend(["--compared", candidate])
    select_argv.extend(
        [
            "--visual-notes",
            (
                "Synthetic CLI regression compared every eligible candidate "
                "and selected the balanced numerical recommendation."
            ),
        ]
    )

    code, payload = call_main(ghs, select_argv)
    if payload.get("status") != "ready_to_publish":
        raise RuntimeError(
            f"Expected ready_to_publish, got {payload}"
        )
    if code != 0:
        raise RuntimeError(
            f"ready_to_publish incorrectly returned exit {code}"
        )

    code, payload = call_main(
        ghs,
        ["workflow-state", "--project", project_name],
    )
    if payload.get("status") != "ready_to_publish":
        raise RuntimeError(
            f"Expected durable ready_to_publish state, got {payload}"
        )
    if payload.get("action") != "publish_recorded_selection":
        raise RuntimeError(
            f"Unexpected durable action: {payload}"
        )
    if code != 0:
        raise RuntimeError(
            "publish_recorded_selection workflow state must return exit 0"
        )

    published = ghs.publish_project(
        workspace=synthetic_workspace,
        project_name=project_name,
        run_root=Path(run["run_root"]),
        candidate_name=None,
        visual_selection_notes=None,
        fresh_run=True,
    )
    if published.get("status") != "ready":
        raise RuntimeError(f"Publication failed: {published}")

    code, payload = call_main(
        ghs,
        ["status", "--project", project_name],
    )
    if payload.get("status") != "ready":
        raise RuntimeError(f"Expected ready status, got {payload}")
    if code != 0:
        raise RuntimeError("ready status must return exit 0")

    # A real blocked state must still be non-zero.
    blocked_payload = {"status": "blocked"}
    if blocked_payload["status"] in ghs.CLI_SUCCESS_STATUSES:
        raise RuntimeError(
            "blocked must not be included in CLI_SUCCESS_STATUSES"
        )

    print(
        json.dumps(
            {
                "status": "success",
                "helper_version": ghs.VERSION,
                "verified_exit_zero_statuses": [
                    "start_new_run",
                    "awaiting_visual_selection",
                    "ready_to_publish",
                    "ready",
                ],
                "blocked_status_remains_nonzero": True,
                "test_root": str(root),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
