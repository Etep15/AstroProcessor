#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import sys
from pathlib import Path


REQUIRED_FUNCTIONS = [
    "validate_upstream",
    "validate_upstream_fast",
    "status_project",
    "status_project_fast",
    "workflow_state",
    "begin_stage",
    "confirm_fresh_run",
    "prepare_policy_reselection",
    "run_project",
    "review_plan",
    "record_visual_selection",
    "publish_project",
    "advance_stage",
    "select_publish_stage",
    "build_parser",
    "main",
]


def load(path: Path):
    spec = importlib.util.spec_from_file_location(
        "black_point_api_104",
        path,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not import staged helper.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--helper", required=True, type=Path)
    args = parser.parse_args()

    bp = load(args.helper.resolve())

    if bp.VERSION != "1.0.4":
        raise RuntimeError(
            f"Expected helper 1.0.4, got {bp.VERSION!r}"
        )

    missing = [
        name
        for name in REQUIRED_FUNCTIONS
        if not callable(getattr(bp, name, None))
    ]
    if missing:
        raise RuntimeError(
            f"Required helper functions are missing: {missing}"
        )

    parser_obj = bp.build_parser()
    command_action = next(
        action
        for action in parser_obj._actions
        if action.dest == "command"
    )
    commands = set(command_action.choices)

    required_commands = {
        "advance",
        "confirm-fresh",
        "select-publish",
        "stage-status",
        "status",
    }
    missing_commands = sorted(required_commands - commands)
    if missing_commands:
        raise RuntimeError(
            f"Required CLI commands missing: {missing_commands}"
        )

    select_parser = command_action.choices["select-publish"]
    options = {
        option
        for action in select_parser._actions
        for option in action.option_strings
    }

    required_options = {
        "--project",
        "--candidate",
        "--visual-notes",
        "--note",
        "--candidate-note",
        "--policy-override-reason",
    }
    missing_options = sorted(required_options - options)
    if missing_options:
        raise RuntimeError(
            f"select-publish options missing: {missing_options}"
        )

    publish_source = inspect.getsource(bp.publish_project)
    for marker in (
        '"selection_policy": selection_policy',
        '"numerical_recommended_candidate": numerical_name',
        '"recommended_candidate": policy_name',
    ):
        if marker not in publish_source:
            raise RuntimeError(
                f"Publication policy marker missing: {marker}"
            )

    print(
        json.dumps(
            {
                "status": "success",
                "helper_version": bp.VERSION,
                "required_functions_present": True,
                "required_cli_commands_present": True,
                "selection_policy_publication_fields_present": True,
                "aggressive_override_interface_present": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
