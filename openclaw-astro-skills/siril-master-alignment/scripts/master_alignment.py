#!/usr/bin/env python3
"""Deterministic Siril master alignment for Ha, SII, and OIII."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

VERSION = "1.0.2"
FILTERS = ("Ha", "SII", "OIII")
FITS_SUFFIXES = {".fit", ".fits", ".fts"}

SIRIL_ROOT = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/"
    ".toolchain/siril/1.4.4/squashfs-root"
)
SIRIL_APPRUN = SIRIL_ROOT / "AppRun"
MINIMUM_SIRIL_VERSION = "1.4.4"
DEFAULT_TIMEOUT_SECONDS = 3600

FATAL_LOG_PATTERNS = (
    "script execution failed",
    "not enough stars",
    "registration failed",
    "sequence processing failed",
    "cannot open",
    "could not open",
    "error while",
    "fatal error",
)


class AlignmentError(RuntimeError):
    pass


@dataclass(frozen=True)
class FitsEvidence:
    path: str
    size: int
    sha256: str
    width: int
    height: int
    channels: int
    dtype: str
    finite_fraction: float
    minimum: float
    maximum: float
    median: float


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run_id() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def json_dump_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def derive_workspace() -> Path:
    resolved = Path(__file__).resolve()
    for parent in resolved.parents:
        if parent.name == "skills":
            return parent.parent
    raise AlignmentError(
        f"Cannot derive owning workspace from installed helper path: {resolved}"
    )


def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    return {
        "workspace": workspace,
        "project": project,
        "processing": project / "processing",
        "aligned": project / "processing" / "aligned",
        "runs": project / ".siril-master-alignment",
    }


def is_fits(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in FITS_SUFFIXES


def candidate_master_paths(processing: Path, filter_name: str) -> list[Path]:
    """Return supported completed-master locations for one filter.

    Mono_Preprocessing.ssf normally writes a named stack such as
    ``processing/Ha/result_Ha_1800s.fit`` inside the filter working directory.
    Earlier skill version 1.0.0 searched only the processing root and the
    generic ``processing/<filter>/result.fit`` path.
    """
    candidates: list[Path] = []
    pattern = re.compile(
        rf"^result_{re.escape(filter_name)}_.+\.(fit|fits|fts)$",
        re.IGNORECASE,
    )

    search_directories = [
        processing,
        processing / filter_name,
    ]
    for directory in search_directories:
        if not directory.is_dir():
            continue
        candidates.extend(
            path
            for path in directory.iterdir()
            if path.is_file() and pattern.fullmatch(path.name)
        )

    generic_per_filter = processing / filter_name / "result.fit"
    if generic_per_filter.is_file():
        candidates.append(generic_per_filter)

    return sorted({path.resolve() for path in candidates})


def discover_master(processing: Path, filter_name: str) -> Path:
    candidates = candidate_master_paths(processing, filter_name)
    if not candidates:
        raise AlignmentError(
            f"No completed {filter_name} master found beneath {processing}."
        )

    by_hash: dict[str, list[Path]] = {}
    for candidate in candidates:
        by_hash.setdefault(sha256_file(candidate), []).append(candidate)

    if len(by_hash) > 1:
        summary = {
            digest: [str(path) for path in paths]
            for digest, paths in sorted(by_hash.items())
        }
        raise AlignmentError(
            f"Multiple distinct {filter_name} master candidates exist. "
            f"Explicit user selection is required: {summary}"
        )

    identical_paths = next(iter(by_hash.values()))
    filter_directory = processing / filter_name
    filter_named_candidates = [
        path
        for path in identical_paths
        if path.parent == filter_directory
        and path.name.lower().startswith(f"result_{filter_name.lower()}_")
    ]
    root_named_candidates = [
        path
        for path in identical_paths
        if path.parent == processing
        and path.name.lower().startswith(f"result_{filter_name.lower()}_")
    ]
    return sorted(
        filter_named_candidates
        or root_named_candidates
        or identical_paths
    )[0]


def inspect_fits(path: Path) -> FitsEvidence:
    try:
        import numpy as np
        from astropy.io import fits
    except Exception as exc:
        raise AlignmentError(
            "Astropy and NumPy are required in the AstroProcessor virtual "
            "environment."
        ) from exc

    if not is_fits(path):
        raise AlignmentError(f"FITS file is missing or unsupported: {path}")

    try:
        with fits.open(
            path,
            memmap=False,
            do_not_scale_image_data=False,
        ) as hdul:
            data = hdul[0].data
            if data is None:
                raise AlignmentError(f"Primary FITS image has no data: {path}")
            array = np.asarray(data)
    except AlignmentError:
        raise
    except Exception as exc:
        raise AlignmentError(f"Cannot read FITS image {path}: {exc}") from exc

    if array.ndim == 2:
        height, width = array.shape
        channels = 1
    elif array.ndim == 3 and array.shape[0] == 1:
        _, height, width = array.shape
        channels = 1
        array = array[0]
    else:
        raise AlignmentError(
            f"Expected a monochrome FITS image but found shape "
            f"{tuple(array.shape)} in {path}"
        )

    if width < 32 or height < 32:
        raise AlignmentError(f"Implausibly small FITS image: {path}")

    finite = np.isfinite(array)
    finite_count = int(finite.sum())
    total = int(array.size)
    if finite_count == 0:
        raise AlignmentError(f"FITS image contains no finite pixels: {path}")

    finite_values = array[finite]
    minimum = float(np.min(finite_values))
    maximum = float(np.max(finite_values))
    median = float(np.median(finite_values))
    if maximum <= minimum:
        raise AlignmentError(f"FITS image is constant or blank: {path}")

    return FitsEvidence(
        path=str(path),
        size=path.stat().st_size,
        sha256=sha256_file(path),
        width=int(width),
        height=int(height),
        channels=channels,
        dtype=str(array.dtype),
        finite_fraction=finite_count / total,
        minimum=minimum,
        maximum=maximum,
        median=median,
    )


def validate_input_set(inputs: dict[str, FitsEvidence]) -> None:
    dimensions = {
        (evidence.width, evidence.height, evidence.channels)
        for evidence in inputs.values()
    }
    if len(dimensions) != 1:
        raise AlignmentError(
            "Master dimensions/channels do not match: "
            + json.dumps(
                {
                    filter_name: [
                        evidence.width,
                        evidence.height,
                        evidence.channels,
                    ]
                    for filter_name, evidence in inputs.items()
                },
                sort_keys=True,
            )
        )
    for filter_name, evidence in inputs.items():
        if evidence.finite_fraction < 1.0:
            raise AlignmentError(
                f"{filter_name} master contains non-finite pixels: "
                f"{evidence.finite_fraction:.8f} finite"
            )


def copy_verified(source: Path, destination: Path, expected_hash: str) -> None:
    if destination.exists():
        raise AlignmentError(f"Refusing to overwrite: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.partial")
    try:
        shutil.copy2(source, temporary)
        copied_hash = sha256_file(temporary)
        if copied_hash != expected_hash:
            raise AlignmentError(
                f"Checksum changed while copying {source} to {destination}"
            )
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def siril_version() -> str:
    if not SIRIL_APPRUN.is_file() or not os.access(SIRIL_APPRUN, os.X_OK):
        raise AlignmentError(f"Siril AppRun is missing or not executable: {SIRIL_APPRUN}")
    env = os.environ.copy()
    env["APPDIR"] = str(SIRIL_ROOT)
    completed = subprocess.run(
        [str(SIRIL_APPRUN), "siril-cli", "--version"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0:
        raise AlignmentError(
            f"Cannot verify Siril version (exit {completed.returncode}): {output}"
        )
    if MINIMUM_SIRIL_VERSION not in output:
        raise AlignmentError(
            f"Expected Siril {MINIMUM_SIRIL_VERSION}, received: {output}"
        )
    return output


def alignment_script() -> str:
    return "\n".join(
        [
            f"requires {MINIMUM_SIRIL_VERSION}",
            "setext fit",
            "setfindstar reset",
            "link masters",
            "register masters -2pass -transf=homography",
            (
                "seqapplyreg masters -framing=min "
                "-interp=lanczos4 -prefix=aligned_"
            ),
            "",
        ]
    )


def preview_script(outputs: dict[str, Path], preview_dir: Path) -> str:
    lines = [f"requires {MINIMUM_SIRIL_VERSION}"]
    for filter_name in FILTERS:
        output = outputs[filter_name]
        preview_stem = preview_dir / f"aligned_{filter_name}_preview"
        lines.extend(
            [
                f'load "{output}"',
                "autostretch -linked",
                f'savepng "{preview_stem}"',
                "close",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def siril_command(workdir: Path, script: Path) -> list[str]:
    return [
        str(SIRIL_APPRUN),
        "siril-cli",
        "--directory",
        str(workdir),
        "--script",
        str(script),
    ]


def run_siril(
    *,
    workdir: Path,
    script: Path,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    command = siril_command(workdir, script)
    env = os.environ.copy()
    env["APPDIR"] = str(SIRIL_ROOT)
    started = time.monotonic()
    timed_out = False
    exit_status: int | None = None

    try:
        completed = subprocess.run(
            command,
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        exit_status = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        stderr += f"\nTimed out after {timeout_seconds} seconds.\n"

    duration = round(time.monotonic() - started, 3)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")
    combined = (stdout + "\n" + stderr).lower()

    fatal_matches = [
        pattern for pattern in FATAL_LOG_PATTERNS if pattern in combined
    ]

    return {
        "command": command,
        "display_command": (
            f'env APPDIR="{SIRIL_ROOT}" "{SIRIL_APPRUN}" siril-cli '
            f'--directory "{workdir}" --script "{script}"'
        ),
        "exit_status": exit_status,
        "timed_out": timed_out,
        "timeout_seconds": timeout_seconds,
        "duration_seconds": duration,
        "stdout_log": str(stdout_path),
        "stderr_log": str(stderr_path),
        "fatal_log_patterns": fatal_matches,
    }


def aligned_sequence_files(workdir: Path) -> list[Path]:
    """Return Siril's exported aligned sequence members in numeric order.

    Siril writes the linked sequence as ``masters_00001.fit`` and the applied
    registration sequence as ``aligned_masters_00001.fit``. Version 1.0.1
    incorrectly omitted the underscore before the numeric frame identifier.
    """
    pattern = re.compile(
        r"^aligned_masters_(\d+)\.(fit|fits|fts)$",
        re.IGNORECASE,
    )
    matched: list[tuple[int, Path]] = []
    for path in workdir.iterdir():
        if not path.is_file():
            continue
        match = pattern.fullmatch(path.name)
        if match:
            matched.append((int(match.group(1)), path))
    return [path for _index, path in sorted(matched)]


def ensure_no_stable_outputs(aligned_dir: Path) -> None:
    targets = [
        aligned_dir / f"aligned_{filter_name}.fit"
        for filter_name in FILTERS
    ] + [aligned_dir / "alignment-manifest.json"]
    existing = [str(path) for path in targets if path.exists()]
    if existing:
        raise AlignmentError(
            "Stable alignment outputs already exist and will not be "
            f"overwritten: {existing}"
        )


def validate_aligned_outputs(
    work_outputs: dict[str, Path],
    input_dimensions: tuple[int, int],
) -> dict[str, FitsEvidence]:
    evidence = {
        filter_name: inspect_fits(path)
        for filter_name, path in work_outputs.items()
    }
    dimensions = {
        (item.width, item.height, item.channels)
        for item in evidence.values()
    }
    if len(dimensions) != 1:
        raise AlignmentError(
            "Aligned outputs do not share identical dimensions."
        )
    width, height, channels = next(iter(dimensions))
    if channels != 1:
        raise AlignmentError("Aligned outputs are not monochrome.")
    input_width, input_height = input_dimensions
    if width > input_width or height > input_height:
        raise AlignmentError(
            "Minimum-common-area output is unexpectedly larger than input."
        )
    if width < int(input_width * 0.5) or height < int(input_height * 0.5):
        raise AlignmentError(
            "Common crop retained less than half the input extent; "
            "manual review is required."
        )
    return evidence


def stable_status(paths: dict[str, Path]) -> dict[str, Any]:
    manifest_path = paths["aligned"] / "alignment-manifest.json"
    if not manifest_path.is_file():
        return {
            "status": "not_run",
            "project": str(paths["project"]),
            "manifest": str(manifest_path),
        }

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "status": "blocked",
            "project": str(paths["project"]),
            "manifest": str(manifest_path),
            "error": f"Cannot read manifest: {exc}",
        }

    errors: list[str] = []
    outputs: dict[str, dict[str, Any]] = {}
    recorded_outputs = manifest.get("outputs", {})
    for filter_name in FILTERS:
        record = recorded_outputs.get(filter_name, {})
        path = Path(str(record.get("path", "")))
        if not path.is_file():
            errors.append(f"Missing aligned output for {filter_name}: {path}")
            continue
        current_hash = sha256_file(path)
        if current_hash != record.get("sha256"):
            errors.append(f"Checksum mismatch for aligned {filter_name}: {path}")
            continue
        try:
            evidence = inspect_fits(path)
            outputs[filter_name] = asdict(evidence)
        except AlignmentError as exc:
            errors.append(str(exc))

    recorded_inputs = manifest.get("inputs", {})
    for filter_name in FILTERS:
        record = recorded_inputs.get(filter_name, {})
        path = Path(str(record.get("path", "")))
        if not path.is_file():
            errors.append(f"Missing source master for {filter_name}: {path}")
            continue
        if sha256_file(path) != record.get("sha256"):
            errors.append(f"Source master changed for {filter_name}: {path}")

    if len(outputs) == 3:
        dimensions = {
            (
                item["width"],
                item["height"],
                item["channels"],
            )
            for item in outputs.values()
        }
        if len(dimensions) != 1:
            errors.append("Aligned output dimensions no longer match.")

    return {
        "status": "ready" if not errors else "blocked",
        "project": str(paths["project"]),
        "manifest": str(manifest_path),
        "outputs": outputs,
        "errors": errors,
        "sho_composition_permitted": not errors,
    }


def run_alignment(
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["project"].is_dir():
        raise AlignmentError(f"Project does not exist: {paths['project']}")
    if not paths["processing"].is_dir():
        raise AlignmentError(
            f"Project processing directory does not exist: {paths['processing']}"
        )

    ensure_no_stable_outputs(paths["aligned"])
    version_output = siril_version()

    masters = {
        filter_name: discover_master(paths["processing"], filter_name)
        for filter_name in FILTERS
    }
    input_evidence = {
        filter_name: inspect_fits(path)
        for filter_name, path in masters.items()
    }
    validate_input_set(input_evidence)
    input_width = input_evidence["Ha"].width
    input_height = input_evidence["Ha"].height

    attempt_id = run_id()
    attempt = paths["runs"] / attempt_id
    workdir = attempt / "work"
    logs = attempt / "logs"
    previews = attempt / "previews"
    for directory in (workdir, logs, previews):
        directory.mkdir(parents=True, exist_ok=False)

    input_order = {
        "Ha": workdir / "01_Ha.fit",
        "SII": workdir / "02_SII.fit",
        "OIII": workdir / "03_OIII.fit",
    }
    for filter_name in FILTERS:
        copy_verified(
            masters[filter_name],
            input_order[filter_name],
            input_evidence[filter_name].sha256,
        )

    script_path = attempt / "align.ssf"
    script_path.write_text(alignment_script(), encoding="utf-8")
    script_hash = sha256_file(script_path)

    run_record = run_siril(
        workdir=workdir,
        script=script_path,
        stdout_path=logs / "stdout.log",
        stderr_path=logs / "stderr.log",
        timeout_seconds=timeout_seconds,
    )

    preliminary_result = {
        "status": "running_validation",
        "helper_version": VERSION,
        "created_at": utc_now(),
        "project": project_name,
        "project_path": str(paths["project"]),
        "attempt": str(attempt),
        "siril_version_output": version_output,
        "inputs": {
            key: asdict(value) for key, value in input_evidence.items()
        },
        "input_copies": {
            key: str(value) for key, value in input_order.items()
        },
        "alignment_script": str(script_path),
        "alignment_script_sha256": script_hash,
        "siril_run": run_record,
    }
    json_dump_atomic(attempt / "alignment-result.json", preliminary_result)

    if run_record["timed_out"]:
        raise AlignmentError(
            f"Siril alignment timed out after {timeout_seconds} seconds. "
            f"Attempt preserved at {attempt}"
        )
    if run_record["exit_status"] != 0:
        raise AlignmentError(
            f"Siril alignment exited with status {run_record['exit_status']}. "
            f"Attempt preserved at {attempt}"
        )
    if run_record["fatal_log_patterns"]:
        raise AlignmentError(
            f"Siril logs contain fatal patterns "
            f"{run_record['fatal_log_patterns']}. Attempt preserved at {attempt}"
        )

    sequence_outputs = aligned_sequence_files(workdir)
    if len(sequence_outputs) != 3:
        raise AlignmentError(
            f"Expected exactly three aligned sequence outputs, found "
            f"{len(sequence_outputs)}: {[str(path) for path in sequence_outputs]}"
        )

    work_outputs = {
        filter_name: sequence_outputs[index]
        for index, filter_name in enumerate(FILTERS)
    }
    aligned_evidence = validate_aligned_outputs(
        work_outputs,
        (input_width, input_height),
    )

    paths["aligned"].mkdir(parents=True, exist_ok=True)
    stable_outputs = {
        filter_name: paths["aligned"] / f"aligned_{filter_name}.fit"
        for filter_name in FILTERS
    }
    for filter_name in FILTERS:
        copy_verified(
            work_outputs[filter_name],
            stable_outputs[filter_name],
            aligned_evidence[filter_name].sha256,
        )

    stable_evidence = {
        filter_name: inspect_fits(path)
        for filter_name, path in stable_outputs.items()
    }

    preview_path = attempt / "preview.ssf"
    preview_path.write_text(
        preview_script(stable_outputs, previews),
        encoding="utf-8",
    )
    preview_run = run_siril(
        workdir=attempt,
        script=preview_path,
        stdout_path=logs / "preview-stdout.log",
        stderr_path=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 900),
    )
    preview_files = {
        filter_name: previews / f"aligned_{filter_name}_preview.png"
        for filter_name in FILTERS
    }

    manifest_path = paths["aligned"] / "alignment-manifest.json"
    manifest = {
        "schema_version": 1,
        "helper_version": VERSION,
        "created_at": utc_now(),
        "project": project_name,
        "project_path": str(paths["project"]),
        "method": {
            "registration": "global two-pass",
            "transformation": "homography",
            "interpolation": "lanczos4",
            "framing": "minimum common area",
            "sequence_order": list(FILTERS),
        },
        "inputs": {
            filter_name: asdict(input_evidence[filter_name])
            for filter_name in FILTERS
        },
        "outputs": {
            filter_name: asdict(stable_evidence[filter_name])
            for filter_name in FILTERS
        },
        "attempt": str(attempt),
        "alignment_script": str(script_path),
        "alignment_script_sha256": script_hash,
        "siril_run": run_record,
        "preview_script": str(preview_path),
        "preview_run": preview_run,
        "previews": {
            filter_name: (
                str(path) if path.is_file() else None
            )
            for filter_name, path in preview_files.items()
        },
        "sho_composition_permitted": True,
    }
    json_dump_atomic(manifest_path, manifest)

    final_result = {
        **preliminary_result,
        "status": "ready",
        "outputs": {
            key: asdict(value) for key, value in stable_evidence.items()
        },
        "stable_manifest": str(manifest_path),
        "preview_run": preview_run,
        "previews": {
            filter_name: (
                str(path) if path.is_file() else None
            )
            for filter_name, path in preview_files.items()
        },
        "sho_composition_permitted": True,
    }
    json_dump_atomic(attempt / "alignment-result.json", final_result)
    return final_result


def self_test() -> dict[str, Any]:
    if alignment_script() != (
        "requires 1.4.4\n"
        "setext fit\n"
        "setfindstar reset\n"
        "link masters\n"
        "register masters -2pass -transf=homography\n"
        "seqapplyreg masters -framing=min "
        "-interp=lanczos4 -prefix=aligned_\n"
    ):
        raise AlignmentError("Alignment script construction changed unexpectedly.")

    workspace = derive_workspace()
    version_output = siril_version()

    try:
        import numpy as np
        from astropy.io import fits
    except Exception as exc:
        raise AlignmentError(
            "Astropy and NumPy are unavailable in the approved Python."
        ) from exc

    with tempfile.TemporaryDirectory(prefix="siril-master-alignment-test-") as tmp:
        path = Path(tmp) / "test.fit"
        header = fits.Header()
        header["FILTER"] = "Ha"
        fits.PrimaryHDU(
            data=np.arange(64 * 64, dtype=np.float32).reshape(64, 64),
            header=header,
        ).writeto(path)
        evidence = inspect_fits(path)
        if evidence.width != 64 or evidence.height != 64:
            raise AlignmentError("Synthetic FITS validation failed.")

        processing = Path(tmp) / "processing"
        filter_directory = processing / "Ha"
        filter_directory.mkdir(parents=True)
        named_master = filter_directory / "result_Ha_1800s.fit"
        shutil.copy2(path, named_master)
        discovered = discover_master(processing, "Ha")
        if discovered != named_master.resolve():
            raise AlignmentError(
                "Filter-local Mono_Preprocessing master discovery failed."
            )

        exported = Path(tmp) / "exported"
        exported.mkdir()
        expected_exports = []
        for index in (1, 2, 3):
            exported_path = exported / f"aligned_masters_{index:05d}.fit"
            exported_path.write_bytes(f"aligned-{index}".encode("utf-8"))
            expected_exports.append(exported_path)
        (exported / "aligned_masters_.seq").write_text("synthetic")
        (exported / "aligned_masters_00004.txt").write_text("ignore")
        found_exports = aligned_sequence_files(exported)
        if found_exports != expected_exports:
            raise AlignmentError(
                "Siril aligned sequence output discovery failed."
            )

    return {
        "status": "success",
        "helper_version": VERSION,
        "workspace": str(workspace),
        "siril_version_output": version_output,
        "tests": [
            "workspace derivation",
            "exact Siril AppRun version",
            "fixed alignment script",
            "Astropy/NumPy FITS validation",
            "SHA-256 evidence",
            "filter-local Mono_Preprocessing master discovery",
            "Siril aligned_masters_00001.fit output discovery",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Align Ha, SII, and OIII master stacks with Siril and crop to "
            "their minimum common area."
        )
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("self-test")

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--project", required=True)
    run_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--project", required=True)

    args = parser.parse_args()

    try:
        workspace = derive_workspace()
        if args.command == "self-test":
            result = self_test()
        elif args.command == "run":
            if args.timeout < 60 or args.timeout > 10800:
                raise AlignmentError(
                    "Timeout must be between 60 and 10800 seconds."
                )
            result = run_alignment(workspace, args.project, args.timeout)
        else:
            result = stable_status(project_paths(workspace, args.project))
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") in {
            "success",
            "ready",
            "not_run",
        } else 2
    except AlignmentError as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "helper_version": VERSION,
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "helper_version": VERSION,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
