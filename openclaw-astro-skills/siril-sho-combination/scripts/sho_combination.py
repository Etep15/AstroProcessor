#!/usr/bin/env python3
"""Deterministic Siril SHO combination: SII→R, Ha→G, OIII→B."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

VERSION = "1.0.0"
MINIMUM_SIRIL_VERSION = "1.4.4"
DEFAULT_TIMEOUT_SECONDS = 900
MAPPING_TOLERANCE = 1e-6

SIRIL_ROOT = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/"
    ".toolchain/siril/1.4.4/squashfs-root"
)
SIRIL_APPRUN = SIRIL_ROOT / "AppRun"

CHANNEL_ORDER = ("SII", "Ha", "OIII")
ROLE_FILENAMES = {
    "SII": "R_SII.fit",
    "Ha": "G_Ha.fit",
    "OIII": "B_OIII.fit",
}
STABLE_OUTPUT_NAME = "SHO-linear.fit"
STABLE_MANIFEST_NAME = "sho-combination-manifest.json"


class ShoCombinationError(RuntimeError):
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
    exposure_seconds: float | None
    stack_count: int | None
    live_time_seconds: float | None
    filter_header: str | None


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def unique_id() -> str:
    return (
        dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        + f"-p{os.getpid()}"
    )


def derive_workspace() -> Path:
    resolved = Path(__file__).resolve()
    for parent in resolved.parents:
        if parent.name == "skills":
            return parent.parent
    raise ShoCombinationError(
        f"Cannot derive owning workspace from helper path: {resolved}"
    )


def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    return {
        "workspace": workspace,
        "project": project,
        "aligned": project / "processing" / "aligned",
        "alignment_manifest": (
            project / "processing" / "aligned" / "alignment-manifest.json"
        ),
        "sho": project / "processing" / "sho",
        "stable_output": (
            project / "processing" / "sho" / STABLE_OUTPUT_NAME
        ),
        "stable_manifest": (
            project / "processing" / "sho" / STABLE_MANIFEST_NAME
        ),
        "runs": project / ".siril-sho-combination",
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_dump_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(
        f".{path.name}.{unique_id()}.partial"
    )
    partial.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(partial, path)


def inspect_fits(path: Path, *, expected_channels: int | None = None) -> FitsEvidence:
    try:
        import numpy as np
        from astropy.io import fits
    except Exception as exc:
        raise ShoCombinationError(
            "Astropy and NumPy are required in the AstroProcessor "
            "virtual environment."
        ) from exc

    if not path.is_file():
        raise ShoCombinationError(f"FITS file is missing: {path}")

    try:
        with fits.open(
            path,
            memmap=False,
            do_not_scale_image_data=False,
        ) as hdul:
            data = hdul[0].data
            header = hdul[0].header.copy()
            if data is None:
                raise ShoCombinationError(
                    f"Primary FITS image has no data: {path}"
                )
            array = np.asarray(data)
    except ShoCombinationError:
        raise
    except Exception as exc:
        raise ShoCombinationError(
            f"Cannot read FITS image {path}: {exc}"
        ) from exc

    if array.ndim == 2:
        height, width = array.shape
        channels = 1
        statistics_array = array
    elif array.ndim == 3 and array.shape[0] in {1, 3, 4}:
        channels, height, width = array.shape
        statistics_array = array
    else:
        raise ShoCombinationError(
            f"Unsupported FITS shape {tuple(array.shape)} in {path}"
        )

    if expected_channels is not None and channels != expected_channels:
        raise ShoCombinationError(
            f"Expected {expected_channels} channel(s), found {channels}: {path}"
        )
    if width < 32 or height < 32:
        raise ShoCombinationError(
            f"Implausibly small FITS image: {path}"
        )

    finite = np.isfinite(statistics_array)
    finite_count = int(finite.sum())
    total = int(statistics_array.size)
    if finite_count == 0:
        raise ShoCombinationError(
            f"FITS image contains no finite pixels: {path}"
        )
    finite_values = statistics_array[finite]
    minimum = float(np.min(finite_values))
    maximum = float(np.max(finite_values))
    median = float(np.median(finite_values))
    if maximum <= minimum:
        raise ShoCombinationError(
            f"FITS image is constant or blank: {path}"
        )

    def optional_float(key: str) -> float | None:
        value = header.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except Exception:
            return None

    def optional_int(key: str) -> int | None:
        value = header.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    filter_value = header.get("FILTER")
    return FitsEvidence(
        path=str(path),
        size=path.stat().st_size,
        sha256=sha256_file(path),
        width=int(width),
        height=int(height),
        channels=int(channels),
        dtype=str(array.dtype),
        finite_fraction=finite_count / total,
        minimum=minimum,
        maximum=maximum,
        median=median,
        exposure_seconds=optional_float("EXPTIME"),
        stack_count=optional_int("STACKCNT"),
        live_time_seconds=optional_float("LIVETIME"),
        filter_header=(
            str(filter_value) if filter_value is not None else None
        ),
    )


def read_fits_array(path: Path):
    try:
        import numpy as np
        from astropy.io import fits
    except Exception as exc:
        raise ShoCombinationError(
            "Astropy and NumPy are required."
        ) from exc
    with fits.open(
        path,
        memmap=False,
        do_not_scale_image_data=False,
    ) as hdul:
        return np.asarray(hdul[0].data, dtype=np.float64)


def load_alignment_manifest(paths: dict[str, Path]) -> tuple[dict[str, Any], str]:
    manifest_path = paths["alignment_manifest"]
    if not manifest_path.is_file():
        raise ShoCombinationError(
            f"Ready alignment manifest is missing: {manifest_path}"
        )
    manifest_hash = sha256_file(manifest_path)
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ShoCombinationError(
            f"Cannot read alignment manifest {manifest_path}: {exc}"
        ) from exc

    if manifest.get("project") != paths["project"].name:
        raise ShoCombinationError(
            "Alignment manifest project does not match the requested project."
        )
    if Path(str(manifest.get("project_path", ""))).resolve() != paths[
        "project"
    ].resolve():
        raise ShoCombinationError(
            "Alignment manifest project path does not match."
        )
    if manifest.get("sho_composition_permitted") is not True:
        raise ShoCombinationError(
            "Alignment manifest does not permit SHO composition."
        )
    return manifest, manifest_hash


def validate_aligned_inputs(
    paths: dict[str, Path],
    manifest: dict[str, Any],
) -> dict[str, FitsEvidence]:
    outputs = manifest.get("outputs", {})
    if not isinstance(outputs, dict):
        raise ShoCombinationError(
            "Alignment manifest has no valid outputs object."
        )

    evidence: dict[str, FitsEvidence] = {}
    for filter_name in ("Ha", "SII", "OIII"):
        record = outputs.get(filter_name)
        if not isinstance(record, dict):
            raise ShoCombinationError(
                f"Alignment manifest has no {filter_name} output record."
            )
        source_path = Path(str(record.get("path", ""))).resolve()
        try:
            source_path.relative_to(paths["project"].resolve())
        except ValueError as exc:
            raise ShoCombinationError(
                f"Aligned {filter_name} path is outside the project: "
                f"{source_path}"
            ) from exc
        if not source_path.is_file():
            raise ShoCombinationError(
                f"Aligned {filter_name} master is missing: {source_path}"
            )
        current_hash = sha256_file(source_path)
        if current_hash != record.get("sha256"):
            raise ShoCombinationError(
                f"Aligned {filter_name} checksum no longer matches the "
                f"alignment manifest: {source_path}"
            )
        current = inspect_fits(source_path, expected_channels=1)
        if current.finite_fraction != 1.0:
            raise ShoCombinationError(
                f"Aligned {filter_name} contains non-finite pixels."
            )
        for key in ("width", "height", "channels"):
            if current.__dict__[key] != record.get(key):
                raise ShoCombinationError(
                    f"Aligned {filter_name} {key} differs from the manifest."
                )
        evidence[filter_name] = current

    dimensions = {
        (item.width, item.height, item.channels)
        for item in evidence.values()
    }
    if len(dimensions) != 1:
        raise ShoCombinationError(
            "Aligned Ha, SII, and OIII dimensions do not match."
        )
    return evidence


def copy_verified(source: Path, destination: Path, expected_hash: str) -> None:
    if destination.exists():
        raise ShoCombinationError(
            f"Refusing to overwrite existing path: {destination}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(
        f".{destination.name}.{unique_id()}.partial"
    )
    shutil.copy2(source, partial)
    if sha256_file(partial) != expected_hash:
        raise ShoCombinationError(
            f"Checksum changed while copying {source}. "
            f"Partial copy preserved at {partial}"
        )
    os.replace(partial, destination)


def siril_version() -> str:
    if not SIRIL_APPRUN.is_file() or not os.access(SIRIL_APPRUN, os.X_OK):
        raise ShoCombinationError(
            f"Siril AppRun is missing or not executable: {SIRIL_APPRUN}"
        )
    environment = os.environ.copy()
    environment["APPDIR"] = str(SIRIL_ROOT)
    completed = subprocess.run(
        [str(SIRIL_APPRUN), "siril-cli", "--version"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = (completed.stdout + "\n" + completed.stderr).strip()
    if completed.returncode != 0:
        raise ShoCombinationError(
            f"Cannot verify Siril version "
            f"(exit {completed.returncode}): {output}"
        )
    if MINIMUM_SIRIL_VERSION not in output:
        raise ShoCombinationError(
            f"Expected Siril {MINIMUM_SIRIL_VERSION}, received: {output}"
        )
    return output


def composition_script(output_name: str = "SHO_linear.fit") -> str:
    return "\n".join(
        [
            f"requires {MINIMUM_SIRIL_VERSION}",
            "setext fit",
            (
                'rgbcomp "R_SII.fit" "G_Ha.fit" "B_OIII.fit" '
                f"-out={output_name} -nosum"
            ),
            "close",
            "",
        ]
    )


def preview_script(input_path: Path, preview_stem: Path) -> str:
    return "\n".join(
        [
            f"requires {MINIMUM_SIRIL_VERSION}",
            f'load "{input_path}"',
            "autostretch -linked",
            f'savepng "{preview_stem}"',
            "close",
            "",
        ]
    )


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
    environment = os.environ.copy()
    environment["APPDIR"] = str(SIRIL_ROOT)
    started = time.monotonic()

    try:
        completed = subprocess.run(
            command,
            cwd=workdir,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        exit_status = completed.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        stderr += f"\nTimed out after {timeout_seconds} seconds.\n"
        exit_status = None
        timed_out = True

    duration = round(time.monotonic() - started, 3)
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stdout_path.write_text(stdout, encoding="utf-8")
    stderr_path.write_text(stderr, encoding="utf-8")

    combined = (stdout + "\n" + stderr).lower()
    fatal_markers = [
        marker
        for marker in (
            "script execution failed",
            "cannot open",
            "could not open",
            "error while",
            "fatal error",
        )
        if marker in combined
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
        "fatal_log_markers": fatal_markers,
    }


def validate_mapping(
    output_path: Path,
    inputs: dict[str, Path],
) -> dict[str, Any]:
    try:
        import numpy as np
    except Exception as exc:
        raise ShoCombinationError("NumPy is required.") from exc

    output = read_fits_array(output_path)
    if output.ndim != 3 or output.shape[0] != 3:
        raise ShoCombinationError(
            f"Expected RGB FITS shape (3, H, W), found {output.shape}."
        )

    expected = {
        "red_from_SII": read_fits_array(inputs["SII"]),
        "green_from_Ha": read_fits_array(inputs["Ha"]),
        "blue_from_OIII": read_fits_array(inputs["OIII"]),
    }
    planes = {
        "red_from_SII": output[0],
        "green_from_Ha": output[1],
        "blue_from_OIII": output[2],
    }

    results: dict[str, Any] = {}
    for key in ("red_from_SII", "green_from_Ha", "blue_from_OIII"):
        if planes[key].shape != expected[key].shape:
            raise ShoCombinationError(
                f"SHO mapping shape mismatch for {key}."
            )
        difference = np.abs(planes[key] - expected[key])
        maximum = float(np.max(difference))
        mean = float(np.mean(difference))
        passed = bool(
            np.allclose(
                planes[key],
                expected[key],
                rtol=MAPPING_TOLERANCE,
                atol=MAPPING_TOLERANCE,
                equal_nan=False,
            )
        )
        results[key] = {
            "maximum_absolute_difference": maximum,
            "mean_absolute_difference": mean,
            "tolerance": MAPPING_TOLERANCE,
            "passed": passed,
        }
        if not passed:
            raise ShoCombinationError(
                f"SHO channel mapping validation failed for {key}; "
                f"maximum difference={maximum}."
            )
    return results


def ensure_no_stable_outputs(paths: dict[str, Path]) -> None:
    existing = [
        str(path)
        for path in (
            paths["stable_output"],
            paths["stable_manifest"],
        )
        if path.exists()
    ]
    if existing:
        raise ShoCombinationError(
            "Stable SHO outputs already exist and will not be overwritten: "
            + json.dumps(existing)
        )


def status(project_name: str, workspace: Path) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    manifest_path = paths["stable_manifest"]
    if not manifest_path.is_file():
        return {
            "status": "not_run",
            "project": str(paths["project"]),
            "manifest": str(manifest_path),
            "star_removal_permitted": False,
        }

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "status": "blocked",
            "project": str(paths["project"]),
            "manifest": str(manifest_path),
            "errors": [f"Cannot read manifest: {exc}"],
            "star_removal_permitted": False,
        }

    errors: list[str] = []
    try:
        alignment_manifest = paths["alignment_manifest"]
        if not alignment_manifest.is_file():
            errors.append(
                f"Alignment manifest missing: {alignment_manifest}"
            )
        elif sha256_file(alignment_manifest) != manifest.get(
            "alignment_manifest_sha256"
        ):
            errors.append("Alignment manifest checksum changed.")

        recorded_inputs = manifest.get("inputs", {})
        current_inputs: dict[str, Path] = {}
        for filter_name in ("Ha", "SII", "OIII"):
            record = recorded_inputs.get(filter_name, {})
            path = Path(str(record.get("path", "")))
            current_inputs[filter_name] = path
            if not path.is_file():
                errors.append(
                    f"Aligned input missing for {filter_name}: {path}"
                )
            elif sha256_file(path) != record.get("sha256"):
                errors.append(
                    f"Aligned input checksum changed for {filter_name}: {path}"
                )

        output_record = manifest.get("output", {})
        output_path = Path(str(output_record.get("path", "")))
        output_evidence: dict[str, Any] | None = None
        mapping: dict[str, Any] | None = None
        if not output_path.is_file():
            errors.append(f"SHO output missing: {output_path}")
        elif sha256_file(output_path) != output_record.get("sha256"):
            errors.append(f"SHO output checksum changed: {output_path}")
        else:
            inspected = inspect_fits(output_path, expected_channels=3)
            output_evidence = asdict(inspected)
            if (
                inspected.width != output_record.get("width")
                or inspected.height != output_record.get("height")
            ):
                errors.append("SHO output dimensions changed.")
            if not errors:
                mapping = validate_mapping(output_path, current_inputs)
    except ShoCombinationError as exc:
        errors.append(str(exc))

    return {
        "status": "ready" if not errors else "blocked",
        "project": str(paths["project"]),
        "manifest": str(manifest_path),
        "output": output_evidence,
        "mapping_validation": mapping,
        "errors": errors,
        "star_removal_permitted": not errors,
    }


def run_combination(
    project_name: str,
    workspace: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["project"].is_dir():
        raise ShoCombinationError(
            f"Project does not exist: {paths['project']}"
        )
    ensure_no_stable_outputs(paths)

    version_output = siril_version()
    alignment_manifest, alignment_manifest_hash = load_alignment_manifest(paths)
    input_evidence = validate_aligned_inputs(paths, alignment_manifest)

    attempt = paths["runs"] / unique_id()
    workdir = attempt / "work"
    logs = attempt / "logs"
    previews = attempt / "previews"
    for directory in (workdir, logs, previews):
        directory.mkdir(parents=True, exist_ok=False)

    input_copies: dict[str, Path] = {}
    for filter_name in CHANNEL_ORDER:
        destination = workdir / ROLE_FILENAMES[filter_name]
        copy_verified(
            Path(input_evidence[filter_name].path),
            destination,
            input_evidence[filter_name].sha256,
        )
        input_copies[filter_name] = destination

    script_path = attempt / "combine-sho.ssf"
    script_path.write_text(
        composition_script(),
        encoding="utf-8",
    )
    script_hash = sha256_file(script_path)

    run_record = run_siril(
        workdir=workdir,
        script=script_path,
        stdout_path=logs / "stdout.log",
        stderr_path=logs / "stderr.log",
        timeout_seconds=timeout_seconds,
    )

    result_record_path = attempt / "sho-combination-result.json"
    preliminary = {
        "status": "running_validation",
        "helper_version": VERSION,
        "created_at": utc_now(),
        "project": project_name,
        "project_path": str(paths["project"]),
        "attempt": str(attempt),
        "siril_version_output": version_output,
        "alignment_manifest": str(paths["alignment_manifest"]),
        "alignment_manifest_sha256": alignment_manifest_hash,
        "inputs": {
            key: asdict(value)
            for key, value in input_evidence.items()
        },
        "input_copies": {
            key: str(value)
            for key, value in input_copies.items()
        },
        "mapping": {
            "red": "SII",
            "green": "Ha",
            "blue": "OIII",
        },
        "script": str(script_path),
        "script_sha256": script_hash,
        "siril_run": run_record,
    }
    json_dump_atomic(result_record_path, preliminary)

    if run_record["timed_out"]:
        raise ShoCombinationError(
            f"Siril SHO combination timed out. Attempt preserved at {attempt}"
        )
    if run_record["exit_status"] != 0:
        raise ShoCombinationError(
            f"Siril SHO combination exited with status "
            f"{run_record['exit_status']}. Attempt preserved at {attempt}"
        )
    if run_record["fatal_log_markers"]:
        raise ShoCombinationError(
            f"Siril logs contain fatal markers "
            f"{run_record['fatal_log_markers']}. "
            f"Attempt preserved at {attempt}"
        )

    attempt_output = workdir / "SHO_linear.fit"
    output_evidence = inspect_fits(
        attempt_output,
        expected_channels=3,
    )
    expected_width = input_evidence["Ha"].width
    expected_height = input_evidence["Ha"].height
    if (
        output_evidence.width != expected_width
        or output_evidence.height != expected_height
    ):
        raise ShoCombinationError(
            "SHO output dimensions do not match the aligned inputs."
        )
    if output_evidence.finite_fraction != 1.0:
        raise ShoCombinationError(
            "SHO output contains non-finite pixels."
        )

    mapping_validation = validate_mapping(
        attempt_output,
        input_copies,
    )

    # Confirm the upstream alignment manifest did not change during execution.
    if sha256_file(paths["alignment_manifest"]) != alignment_manifest_hash:
        raise ShoCombinationError(
            "Alignment manifest changed during SHO composition."
        )

    paths["sho"].mkdir(parents=True, exist_ok=True)
    copy_verified(
        attempt_output,
        paths["stable_output"],
        output_evidence.sha256,
    )
    stable_evidence = inspect_fits(
        paths["stable_output"],
        expected_channels=3,
    )

    preview_stem = previews / "SHO-linear-autostretch-preview"
    preview_script_path = attempt / "preview.ssf"
    preview_script_path.write_text(
        preview_script(paths["stable_output"], preview_stem),
        encoding="utf-8",
    )
    preview_run = run_siril(
        workdir=attempt,
        script=preview_script_path,
        stdout_path=logs / "preview-stdout.log",
        stderr_path=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 300),
    )
    preview_path = preview_stem.with_suffix(".png")

    manifest = {
        "schema_version": 1,
        "helper_version": VERSION,
        "created_at": utc_now(),
        "project": project_name,
        "project_path": str(paths["project"]),
        "alignment_manifest": str(paths["alignment_manifest"]),
        "alignment_manifest_sha256": alignment_manifest_hash,
        "mapping": {
            "red": "SII",
            "green": "Ha",
            "blue": "OIII",
        },
        "method": {
            "siril_command": "rgbcomp",
            "brightness_matching": False,
            "normalization": False,
            "permanent_stretch": False,
            "sum_exposure_headers": False,
        },
        "inputs": {
            key: asdict(value)
            for key, value in input_evidence.items()
        },
        "output": asdict(stable_evidence),
        "mapping_validation": mapping_validation,
        "attempt": str(attempt),
        "script": str(script_path),
        "script_sha256": script_hash,
        "siril_run": run_record,
        "preview_script": str(preview_script_path),
        "preview_run": preview_run,
        "preview": (
            str(preview_path) if preview_path.is_file() else None
        ),
        "star_removal_permitted": True,
    }
    json_dump_atomic(paths["stable_manifest"], manifest)

    final = {
        **preliminary,
        "status": "ready",
        "output": asdict(stable_evidence),
        "mapping_validation": mapping_validation,
        "preview_run": preview_run,
        "preview": (
            str(preview_path) if preview_path.is_file() else None
        ),
        "stable_manifest": str(paths["stable_manifest"]),
        "star_removal_permitted": True,
    }
    json_dump_atomic(result_record_path, final)
    return final


def self_test(workspace: Path) -> dict[str, Any]:
    try:
        import numpy as np
        from astropy.io import fits
    except Exception as exc:
        raise ShoCombinationError(
            "Astropy and NumPy are unavailable in the approved Python."
        ) from exc

    version_output = siril_version()
    test_root = (
        workspace
        / ".skill-self-tests"
        / "siril-sho-combination"
        / unique_id()
    )
    workdir = test_root / "work"
    logs = test_root / "logs"
    workdir.mkdir(parents=True, exist_ok=False)
    logs.mkdir(parents=True, exist_ok=False)

    height = 64
    width = 64
    y, x = np.mgrid[0:height, 0:width]
    arrays = {
        "SII": ((x + 1) / 1000.0).astype(np.float32),
        "Ha": ((y + 2) / 900.0).astype(np.float32),
        "OIII": (((x + y) + 3) / 1100.0).astype(np.float32),
    }
    for filter_name in CHANNEL_ORDER:
        path = workdir / ROLE_FILENAMES[filter_name]
        header = fits.Header()
        header["FILTER"] = filter_name
        fits.PrimaryHDU(
            data=arrays[filter_name],
            header=header,
        ).writeto(path)

    script_path = test_root / "self-test.ssf"
    script_path.write_text(composition_script(), encoding="utf-8")
    expected_script = (
        "requires 1.4.4\n"
        "setext fit\n"
        'rgbcomp "R_SII.fit" "G_Ha.fit" "B_OIII.fit" '
        "-out=SHO_linear.fit -nosum\n"
        "close\n"
    )
    if script_path.read_text(encoding="utf-8") != expected_script:
        raise ShoCombinationError(
            "Fixed Siril composition script changed unexpectedly."
        )

    run_record = run_siril(
        workdir=workdir,
        script=script_path,
        stdout_path=logs / "stdout.log",
        stderr_path=logs / "stderr.log",
        timeout_seconds=300,
    )
    if run_record["timed_out"] or run_record["exit_status"] != 0:
        raise ShoCombinationError(
            f"Synthetic Siril rgbcomp failed. Evidence preserved at {test_root}"
        )
    output_path = workdir / "SHO_linear.fit"
    output_evidence = inspect_fits(output_path, expected_channels=3)
    mapping_validation = validate_mapping(
        output_path,
        {
            "SII": workdir / ROLE_FILENAMES["SII"],
            "Ha": workdir / ROLE_FILENAMES["Ha"],
            "OIII": workdir / ROLE_FILENAMES["OIII"],
        },
    )

    result = {
        "status": "success",
        "helper_version": VERSION,
        "workspace": str(workspace),
        "self_test_directory": str(test_root),
        "siril_version_output": version_output,
        "output": asdict(output_evidence),
        "mapping_validation": mapping_validation,
        "tests": [
            "workspace derivation",
            "exact Siril AppRun version",
            "fixed rgbcomp script",
            "actual Siril RGB composition",
            "three-channel FITS validation",
            "SII to red mapping",
            "Ha to green mapping",
            "OIII to blue mapping",
            "self-test evidence preservation",
        ],
    }
    json_dump_atomic(test_root / "self-test-result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Combine aligned SII, Ha, and OIII masters into a linear SHO FITS."
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
            result = self_test(workspace)
        elif args.command == "run":
            if args.timeout < 60 or args.timeout > 3600:
                raise ShoCombinationError(
                    "Timeout must be between 60 and 3600 seconds."
                )
            result = run_combination(
                args.project,
                workspace,
                args.timeout,
            )
        else:
            result = status(args.project, workspace)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") in {
            "success",
            "ready",
            "not_run",
        } else 2
    except ShoCombinationError as exc:
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
