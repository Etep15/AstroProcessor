#!/usr/bin/env python3
"""Deterministic Siril linear NL-Bayes denoise workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any

import numpy as np
from astropy.io import fits


VERSION = "1.0.1"
WORKSPACE = Path(
    "/home/peter/.openclaw/workspace/agents/codewarrior"
)
SIRIL_APP = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/"
    ".toolchain/siril/1.4.4/squashfs-root/AppRun"
)
SIRIL_APPDIR = SIRIL_APP.parent
REQUIRED_SIRIL_VERSION = "1.4.4"

# This matches Peter's previously successful M16 NL-Bayes setting:
# NL-Bayes denoise, modulation 0.750, Cosmetic Correction enabled.
DENOISE_MODULATION = 0.75

FATAL_LOG_MARKERS = (
    "script execution failed",
    "command execution failed",
    "cannot load",
    "could not load",
    "not enough memory",
    "segmentation fault",
)


class LinearDenoiseError(RuntimeError):
    """Raised when a deterministic denoise stage cannot safely continue."""


@dataclass(frozen=True)
class FitsEvidence:
    path: str
    sha256: str
    size: int
    bitpix: int
    dtype: str
    channels: int
    width: int
    height: int
    finite_fraction: float
    minimum: float
    maximum: float
    median: float
    filter_header: str | None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def unique_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}-p{os.getpid()}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def json_dump_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{unique_id()}")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def inspect_fits(
    path: Path,
    *,
    expected_channels: int = 3,
    require_float32: bool = True,
) -> FitsEvidence:
    if not path.is_file():
        raise LinearDenoiseError(f"FITS file does not exist: {path}")

    with fits.open(path, memmap=True) as hdul:
        data = hdul[0].data
        header = hdul[0].header
        if data is None:
            raise LinearDenoiseError(f"FITS contains no image data: {path}")
        array = np.asarray(data)

        if array.ndim == 2:
            channels = 1
            height, width = array.shape
        elif array.ndim == 3:
            channels, height, width = array.shape
        else:
            raise LinearDenoiseError(
                f"Unsupported FITS dimensions {array.shape}: {path}"
            )

        if channels != expected_channels:
            raise LinearDenoiseError(
                f"Expected {expected_channels} channels, found {channels}: "
                f"{path}"
            )
        if require_float32 and array.dtype.kind != "f":
            raise LinearDenoiseError(
                f"Expected floating-point FITS data, found {array.dtype}: "
                f"{path}"
            )
        if require_float32 and array.dtype.itemsize != 4:
            raise LinearDenoiseError(
                f"Expected 32-bit float FITS data, found {array.dtype}: {path}"
            )

        finite = np.isfinite(array)
        finite_fraction = float(np.mean(finite))
        if finite_fraction == 0.0:
            minimum = maximum = median = math.nan
        else:
            finite_values = array[finite]
            minimum = float(np.min(finite_values))
            maximum = float(np.max(finite_values))
            median = float(np.median(finite_values))

        return FitsEvidence(
            path=str(path),
            sha256=sha256_file(path),
            size=path.stat().st_size,
            bitpix=int(header.get("BITPIX", 0)),
            dtype=str(array.dtype),
            channels=int(channels),
            width=int(width),
            height=int(height),
            finite_fraction=finite_fraction,
            minimum=minimum,
            maximum=maximum,
            median=median,
            filter_header=(
                str(header["FILTER"]) if "FILTER" in header else None
            ),
        )


def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    processing = project / "processing"
    stable = processing / "linear-denoise"
    return {
        "project": project,
        "processing": processing,
        "source": processing / "starnet" / "SHO-starless-linear.fit",
        "starnet_manifest": (
            processing / "starnet" / "starnet-manifest.json"
        ),
        "runs": project / ".siril-linear-denoise",
        "stable": stable,
        "stable_output": (
            stable / "SHO-starless-linear-denoised.fit"
        ),
        "stable_before_preview": (
            stable / "SHO-starless-linear-before-linked.png"
        ),
        "stable_after_preview": (
            stable / "SHO-starless-linear-denoised-linked.png"
        ),
        "stable_manifest": stable / "linear-denoise-manifest.json",
    }


def siril_version() -> dict[str, Any]:
    if not SIRIL_APP.is_file() or not os.access(SIRIL_APP, os.X_OK):
        raise LinearDenoiseError(
            f"Siril AppRun is unavailable or not executable: {SIRIL_APP}"
        )

    environment = os.environ.copy()
    environment["APPDIR"] = str(SIRIL_APPDIR)
    completed = subprocess.run(
        [str(SIRIL_APP), "siril-cli", "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        timeout=60,
        check=False,
    )
    combined = "\n".join(
        part for part in (completed.stdout, completed.stderr) if part
    ).strip()
    if completed.returncode != 0:
        raise LinearDenoiseError(
            f"Could not read Siril version (exit {completed.returncode}): "
            f"{combined}"
        )
    if REQUIRED_SIRIL_VERSION not in combined:
        raise LinearDenoiseError(
            f"Expected Siril {REQUIRED_SIRIL_VERSION}; got: {combined}"
        )
    return {
        "version": REQUIRED_SIRIL_VERSION,
        "version_output": combined,
        "path": str(SIRIL_APP),
    }


def run_siril_script(
    *,
    directory: Path,
    script: Path,
    stdout_log: Path,
    stderr_log: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)

    command = [
        str(SIRIL_APP),
        "siril-cli",
        "--directory",
        str(directory),
        "--script",
        str(script),
    ]
    environment = os.environ.copy()
    environment["APPDIR"] = str(SIRIL_APPDIR)

    started = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            timeout=timeout_seconds,
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")

    duration = time.monotonic() - started
    stdout_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")

    combined_lower = f"{stdout}\n{stderr}".lower()
    fatal_markers = [
        marker for marker in FATAL_LOG_MARKERS
        if marker in combined_lower
    ]

    return {
        "command": command,
        "display_command": (
            f'env APPDIR="{SIRIL_APPDIR}" '
            + " ".join(f'"{part}"' for part in command)
        ),
        "exit_status": int(returncode),
        "duration_seconds": round(duration, 3),
        "timed_out": timed_out,
        "timeout_seconds": int(timeout_seconds),
        "fatal_log_markers": fatal_markers,
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }


def denoise_script_text() -> str:
    return "\n".join(
        (
            f"requires {REQUIRED_SIRIL_VERSION}",
            'load "SHO-starless-linear.fit"',
            f"denoise -mod={DENOISE_MODULATION:.2f}",
            'save "SHO-starless-linear-denoised.fit"',
            "close",
            "",
        )
    )


def preview_script_text() -> str:
    return "\n".join(
        (
            f"requires {REQUIRED_SIRIL_VERSION}",
            'load "SHO-starless-linear.fit"',
            "autostretch -linked",
            (
                'savepng "../previews/'
                'SHO-starless-linear-before-linked"'
            ),
            "close",
            'load "SHO-starless-linear-denoised.fit"',
            "autostretch -linked",
            (
                'savepng "../previews/'
                'SHO-starless-linear-denoised-linked"'
            ),
            "close",
            "",
        )
    )


def robust_noise_proxy(array: np.ndarray) -> dict[str, float]:
    results: dict[str, float] = {}
    names = ("red", "green", "blue")
    for index, name in enumerate(names):
        channel = np.asarray(array[index], dtype=np.float64)
        threshold = float(np.percentile(channel, 90.0))
        # Sample every fourth row and column while retaining one-pixel
        # horizontal differences. Restrict to dimmer areas so real nebular
        # edges and bright details do not dominate the noise estimate.
        left = channel[::4, :-1:4]
        right = channel[::4, 1::4]
        valid = (
            np.isfinite(left)
            & np.isfinite(right)
            & (left <= threshold)
            & (right <= threshold)
        )
        differences = (right - left)[valid]
        if differences.size < 100:
            results[name] = math.nan
            continue
        centre = float(np.median(differences))
        mad = float(np.median(np.abs(differences - centre)))
        results[name] = 1.4826 * mad / math.sqrt(2.0)
    finite_values = [
        value for value in results.values() if math.isfinite(value)
    ]
    results["median"] = (
        float(np.median(finite_values))
        if finite_values
        else math.nan
    )
    return results


def quality_assessment(
    source_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    with fits.open(source_path, memmap=True) as hdul:
        source = np.asarray(hdul[0].data, dtype=np.float32)
    with fits.open(output_path, memmap=True) as hdul:
        output = np.asarray(hdul[0].data, dtype=np.float32)

    if source.shape != output.shape:
        return {
            "status": "failed",
            "satisfactory": False,
            "failed_checks": [
                {
                    "metric": "shape",
                    "source": list(source.shape),
                    "output": list(output.shape),
                }
            ],
        }

    finite_source = np.isfinite(source)
    finite_output = np.isfinite(output)
    finite_fraction = float(np.mean(finite_output))

    sample_source = source[:, ::3, ::3].astype(np.float64, copy=False)
    sample_output = output[:, ::3, ::3].astype(np.float64, copy=False)
    valid = np.isfinite(sample_source) & np.isfinite(sample_output)
    source_values = sample_source[valid]
    output_values = sample_output[valid]

    source_low = float(np.percentile(source_values, 0.5))
    source_high = float(np.percentile(source_values, 99.5))
    span = max(source_high - source_low, 1.0e-12)

    delta = output_values - source_values
    relative_rms_change = float(
        math.sqrt(float(np.mean(delta * delta))) / span
    )
    relative_median_shift = float(
        abs(float(np.median(output_values)) - float(np.median(source_values)))
        / span
    )

    source_centre = source_values - float(np.mean(source_values))
    output_centre = output_values - float(np.mean(output_values))
    denominator = math.sqrt(
        float(np.dot(source_centre, source_centre))
        * float(np.dot(output_centre, output_centre))
    )
    correlation = (
        float(np.dot(source_centre, output_centre) / denominator)
        if denominator > 0.0
        else math.nan
    )

    source_p99 = float(np.percentile(source_values, 99.0))
    output_p99 = float(np.percentile(output_values, 99.0))
    p99_retention = (
        output_p99 / source_p99
        if abs(source_p99) > 1.0e-12
        else math.nan
    )

    source_noise = robust_noise_proxy(source)
    output_noise = robust_noise_proxy(output)
    before_noise = float(source_noise["median"])
    after_noise = float(output_noise["median"])
    noise_ratio = (
        after_noise / before_noise
        if before_noise > 0.0 and math.isfinite(after_noise)
        else math.nan
    )

    source_minimum = float(np.min(source[finite_source]))
    output_minimum = float(np.min(output[finite_output]))
    minimum_floor = source_minimum - 0.10 * span

    thresholds = {
        "finite_fraction": 1.0,
        "minimum_correlation": 0.995,
        "minimum_relative_rms_change": 1.0e-7,
        "maximum_relative_rms_change": 0.15,
        "maximum_relative_median_shift": 0.03,
        "minimum_p99_retention": 0.75,
        "maximum_p99_retention": 1.25,
        "maximum_noise_ratio": 1.05,
        "minimum_output_value": minimum_floor,
    }

    failed: list[dict[str, Any]] = []

    def require_min(metric: str, value: float, minimum: float) -> None:
        if not math.isfinite(value) or value < minimum:
            failed.append(
                {
                    "metric": metric,
                    "value": value,
                    "minimum_required": minimum,
                }
            )

    def require_max(metric: str, value: float, maximum: float) -> None:
        if not math.isfinite(value) or value > maximum:
            failed.append(
                {
                    "metric": metric,
                    "value": value,
                    "maximum_allowed": maximum,
                }
            )

    require_min("finite_fraction", finite_fraction, 1.0)
    require_min("correlation", correlation, 0.995)
    require_min(
        "relative_rms_change",
        relative_rms_change,
        1.0e-7,
    )
    require_max(
        "relative_rms_change",
        relative_rms_change,
        0.15,
    )
    require_max(
        "relative_median_shift",
        relative_median_shift,
        0.03,
    )
    require_min("p99_retention", p99_retention, 0.75)
    require_max("p99_retention", p99_retention, 1.25)
    require_max("noise_ratio", noise_ratio, 1.05)
    require_min("output_minimum", output_minimum, minimum_floor)

    satisfactory = not failed
    return {
        "status": "satisfactory" if satisfactory else "needs_review",
        "satisfactory": satisfactory,
        "failed_checks": failed,
        "metrics": {
            "finite_fraction": finite_fraction,
            "correlation": correlation,
            "relative_rms_change": relative_rms_change,
            "relative_median_shift": relative_median_shift,
            "p99_retention": p99_retention,
            "source_noise_proxy": source_noise,
            "output_noise_proxy": output_noise,
            "noise_ratio": noise_ratio,
            "source_minimum": source_minimum,
            "output_minimum": output_minimum,
            "source_robust_span": span,
        },
        "thresholds": thresholds,
        "interpretation": (
            "NL-Bayes reduced the background-noise proxy while preserving "
            "the linear image's global structure and bright detail."
            if satisfactory
            else "The output requires review because one or more structural "
            "or noise-reduction safeguards did not pass."
        ),
    }


def validate_starnet_source(paths: dict[str, Path]) -> tuple[dict[str, Any], FitsEvidence]:
    if not paths["project"].is_dir():
        raise LinearDenoiseError(
            f"Project does not exist: {paths['project']}"
        )
    if not paths["starnet_manifest"].is_file():
        raise LinearDenoiseError(
            f"StarNet manifest is missing: {paths['starnet_manifest']}"
        )

    manifest = json.loads(
        paths["starnet_manifest"].read_text(encoding="utf-8")
    )
    if manifest.get("status") != "ready":
        raise LinearDenoiseError(
            "StarNet manifest status is not ready."
        )
    if not manifest.get("starless_background_processing_permitted"):
        raise LinearDenoiseError(
            "StarNet manifest does not permit starless background processing."
        )

    source_evidence = inspect_fits(paths["source"])
    expected = manifest.get("linear_starless", {})
    expected_hash = expected.get("sha256")
    if not expected_hash:
        raise LinearDenoiseError(
            "StarNet manifest does not record the starless SHA-256."
        )
    if source_evidence.sha256 != expected_hash:
        raise LinearDenoiseError(
            "Canonical starless FITS checksum does not match the StarNet "
            "manifest."
        )

    return manifest, source_evidence


def execute_denoise(
    source_path: Path,
    run_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    candidate = run_root / "candidate-00"
    work = candidate / "work"
    logs = candidate / "logs"
    previews = candidate / "previews"
    work.mkdir(parents=True, exist_ok=False)
    logs.mkdir()
    previews.mkdir()

    working_source = work / "SHO-starless-linear.fit"
    shutil.copy2(source_path, working_source)

    denoise_script = candidate / "linear-denoise.ssf"
    denoise_script.write_text(denoise_script_text(), encoding="utf-8")

    denoise_run = run_siril_script(
        directory=work,
        script=denoise_script,
        stdout_log=logs / "denoise-stdout.log",
        stderr_log=logs / "denoise-stderr.log",
        timeout_seconds=timeout_seconds,
    )
    if (
        denoise_run["exit_status"] != 0
        or denoise_run["timed_out"]
        or denoise_run["fatal_log_markers"]
    ):
        raise LinearDenoiseError(
            "Siril denoise failed; evidence is preserved at "
            f"{candidate}"
        )

    output = work / "SHO-starless-linear-denoised.fit"
    source_evidence = inspect_fits(working_source)
    output_evidence = inspect_fits(output)

    preview_script = candidate / "previews.ssf"
    preview_script.write_text(preview_script_text(), encoding="utf-8")
    preview_run = run_siril_script(
        directory=work,
        script=preview_script,
        stdout_log=logs / "preview-stdout.log",
        stderr_log=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 600),
    )

    before_preview = previews / "SHO-starless-linear-before-linked.png"
    after_preview = (
        previews / "SHO-starless-linear-denoised-linked.png"
    )
    preview_failures: list[str] = []
    if preview_run["exit_status"] != 0:
        preview_failures.append(
            f"preview exit status {preview_run['exit_status']}"
        )
    if preview_run["fatal_log_markers"]:
        preview_failures.append(
            f"preview fatal markers {preview_run['fatal_log_markers']}"
        )
    for preview in (before_preview, after_preview):
        if not preview.is_file():
            preview_failures.append(f"missing preview {preview}")
    if preview_failures:
        raise LinearDenoiseError(
            f"Preview generation failed ({preview_failures}); evidence is "
            f"preserved at {candidate}"
        )

    quality = quality_assessment(working_source, output)

    return {
        "candidate": "candidate-00",
        "candidate_directory": str(candidate),
        "created_at": utc_now(),
        "helper_version": VERSION,
        "method": {
            "algorithm": "Siril NL-Bayes",
            "command": f"denoise -mod={DENOISE_MODULATION:.2f}",
            "modulation": DENOISE_MODULATION,
            "cosmetic_correction": True,
            "vst": False,
            "da3d": False,
            "sos": False,
            "independent_channels": False,
        },
        "source": asdict(source_evidence),
        "output": asdict(output_evidence),
        "denoise_script": str(denoise_script),
        "denoise_script_sha256": sha256_file(denoise_script),
        "denoise_run": denoise_run,
        "preview_script": str(preview_script),
        "preview_run": preview_run,
        "previews": {
            "before_linked": str(before_preview),
            "after_linked": str(after_preview),
        },
        "quality_assessment": quality,
        "status": (
            "satisfactory"
            if quality["satisfactory"]
            else "needs_review"
        ),
    }


def publish(
    paths: dict[str, Path],
    run_root: Path,
    candidate: dict[str, Any],
    *,
    fresh_run: bool,
    source_evidence: FitsEvidence,
    starnet_manifest: dict[str, Any],
    siril: dict[str, Any],
) -> dict[str, Any]:
    existing = paths["stable"].exists()
    if existing and not fresh_run:
        raise LinearDenoiseError(
            f"Canonical linear-denoise directory already exists: "
            f"{paths['stable']}. Use --fresh-run to execute denoise again "
            "while preserving the previous directory intact."
        )

    if not candidate["quality_assessment"]["satisfactory"]:
        raise LinearDenoiseError(
            "Denoise candidate did not pass its quality safeguards; "
            "canonical output was not changed."
        )

    publish_dir = run_root / "publish-staging"
    publish_dir.mkdir(parents=True, exist_ok=False)

    candidate_dir = Path(candidate["candidate_directory"])
    candidate_output = (
        candidate_dir / "work" / "SHO-starless-linear-denoised.fit"
    )
    candidate_before = (
        candidate_dir
        / "previews"
        / "SHO-starless-linear-before-linked.png"
    )
    candidate_after = (
        candidate_dir
        / "previews"
        / "SHO-starless-linear-denoised-linked.png"
    )

    staged_output = (
        publish_dir / "SHO-starless-linear-denoised.fit"
    )
    staged_before = (
        publish_dir / "SHO-starless-linear-before-linked.png"
    )
    staged_after = (
        publish_dir / "SHO-starless-linear-denoised-linked.png"
    )
    shutil.copy2(candidate_output, staged_output)
    shutil.copy2(candidate_before, staged_before)
    shutil.copy2(candidate_after, staged_after)

    staged_evidence = inspect_fits(staged_output)
    final_evidence = asdict(staged_evidence)
    final_evidence["path"] = str(paths["stable_output"])

    previous = (
        run_root / "previous-processing-linear-denoise"
        if existing
        else None
    )
    if previous is not None and previous.exists():
        raise LinearDenoiseError(
            f"Preservation destination already exists: {previous}"
        )

    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "status": "ready",
        "helper_version": VERSION,
        "project": paths["project"].name,
        "project_path": str(paths["project"]),
        "source": asdict(source_evidence),
        "source_starnet_manifest": str(paths["starnet_manifest"]),
        "source_starnet_manifest_status": starnet_manifest.get("status"),
        "source_starnet_helper_version": starnet_manifest.get(
            "helper_version"
        ),
        "method": candidate["method"],
        "selected_candidate": candidate["candidate"],
        "quality_assessment": candidate["quality_assessment"],
        "output": final_evidence,
        "previews": {
            "before_linked": str(paths["stable_before_preview"]),
            "after_linked": str(paths["stable_after_preview"]),
        },
        "stable_paths": {
            "directory": str(paths["stable"]),
            "output": str(paths["stable_output"]),
            "before_preview": str(paths["stable_before_preview"]),
            "after_preview": str(paths["stable_after_preview"]),
            "manifest": str(paths["stable_manifest"]),
        },
        "previous_processing_linear_denoise_preserved_at": (
            str(previous) if previous is not None else None
        ),
        "publication_method": (
            "validate new result, preserve the previous canonical directory, "
            "then atomically rename the staged directory"
        ),
        "siril": siril,
        "linear_denoise_permitted": True,
        "downstream_linear_processing_permitted": True,
    }
    json_dump_atomic(
        publish_dir / "linear-denoise-manifest.json",
        manifest,
    )

    moved_existing = False
    try:
        if existing:
            paths["stable"].rename(previous)
            moved_existing = True
        publish_dir.rename(paths["stable"])
    except Exception:
        if moved_existing and not paths["stable"].exists():
            previous.rename(paths["stable"])
        raise

    return manifest


def run_project(
    *,
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
    fresh_run: bool,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if paths["stable"].exists() and not fresh_run:
        raise LinearDenoiseError(
            f"Canonical output already exists: {paths['stable']}. "
            "This command did not reuse it. Use --fresh-run to execute "
            "NL-Bayes again and preserve the current result."
        )

    siril = siril_version()
    starnet_manifest, source_evidence = validate_starnet_source(paths)

    run_started_at = utc_now()
    run_root = paths["runs"] / f"denoise-{unique_id()}"
    run_root.mkdir(parents=True, exist_ok=False)

    candidate = execute_denoise(
        paths["source"],
        run_root,
        timeout_seconds,
    )

    run_record = {
        "status": candidate["status"],
        "created_at": utc_now(),
        "run_started_at": run_started_at,
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "fresh_run_requested": bool(fresh_run),
        "candidate": candidate,
        "canonical_output_changed": False,
    }
    json_dump_atomic(run_root / "run-manifest.json", run_record)

    if not candidate["quality_assessment"]["satisfactory"]:
        return {
            **run_record,
            "status": "needs_review",
            "message": (
                "The candidate is preserved, but canonical output was not "
                "changed because one or more quality safeguards failed."
            ),
            "downstream_linear_processing_permitted": False,
        }

    manifest = publish(
        paths,
        run_root,
        candidate,
        fresh_run=fresh_run,
        source_evidence=source_evidence,
        starnet_manifest=starnet_manifest,
        siril=siril,
    )

    result = {
        "status": "ready",
        "created_at": utc_now(),
        "run_started_at": run_started_at,
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "selected_candidate": candidate["candidate"],
        "fresh_run_requested": bool(fresh_run),
        "stable_directory": str(paths["stable"]),
        "stable_output": str(paths["stable_output"]),
        "stable_before_preview": str(paths["stable_before_preview"]),
        "stable_after_preview": str(paths["stable_after_preview"]),
        "stable_manifest": str(paths["stable_manifest"]),
        "previous_processing_linear_denoise_preserved_at": manifest.get(
            "previous_processing_linear_denoise_preserved_at"
        ),
        "canonical_output_changed": True,
        "downstream_linear_processing_permitted": True,
        "manifest": manifest,
    }
    json_dump_atomic(run_root / "run-manifest.json", result)
    return result


def status_project(
    workspace: Path,
    project_name: str,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["stable_manifest"].is_file():
        return {
            "status": "missing",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "stable_directory": str(paths["stable"]),
            "downstream_linear_processing_permitted": False,
        }

    manifest = json.loads(
        paths["stable_manifest"].read_text(encoding="utf-8")
    )
    errors: list[str] = []

    if not paths["stable_output"].is_file():
        errors.append(f"Missing output: {paths['stable_output']}")
        output_evidence = None
    else:
        output_evidence = asdict(inspect_fits(paths["stable_output"]))
        expected_hash = manifest.get("output", {}).get("sha256")
        if expected_hash and output_evidence["sha256"] != expected_hash:
            errors.append("Output checksum does not match the manifest.")

    for preview_key in ("stable_before_preview", "stable_after_preview"):
        if not paths[preview_key].is_file():
            errors.append(f"Missing preview: {paths[preview_key]}")

    ready = manifest.get("status") == "ready" and not errors
    return {
        "status": "ready" if ready else "invalid",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "stable_directory": str(paths["stable"]),
        "manifest": str(paths["stable_manifest"]),
        "errors": errors,
        "source": manifest.get("source"),
        "method": manifest.get("method"),
        "quality_assessment": manifest.get("quality_assessment"),
        "output": output_evidence,
        "previews": manifest.get("previews"),
        "previous_processing_linear_denoise_preserved_at": manifest.get(
            "previous_processing_linear_denoise_preserved_at"
        ),
        "downstream_linear_processing_permitted": ready,
    }


def write_synthetic_fits(path: Path) -> None:
    rng = np.random.default_rng(1729)
    height = width = 512
    yy, xx = np.mgrid[0:height, 0:width]
    image = np.empty((3, height, width), dtype=np.float32)

    nebula = (
        0.009
        + 0.010 * np.exp(
            -(
                ((xx - 275.0) / 135.0) ** 2
                + ((yy - 250.0) / 105.0) ** 2
            )
        )
        + 0.0025 * np.sin(xx / 37.0) * np.cos(yy / 51.0)
    )

    channel_scales = (1.0, 0.78, 0.58)
    for channel, scale in enumerate(channel_scales):
        image[channel] = nebula * scale

    stars = (
        (70, 90, 0.22, 2.2),
        (180, 340, 0.42, 2.7),
        (390, 160, 0.33, 2.4),
        (315, 410, 0.58, 3.0),
        (245, 255, 0.75, 3.4),
    )
    for cy, cx, amplitude, sigma in stars:
        profile = amplitude * np.exp(
            -(
                (xx - cx) ** 2 + (yy - cy) ** 2
            )
            / (2.0 * sigma * sigma)
        )
        image[0] += profile
        image[1] += profile * 0.86
        image[2] += profile * 0.72

    noise = rng.normal(0.0, 0.0017, image.shape).astype(np.float32)
    image += noise
    image = np.clip(image, 0.0, 1.0).astype(np.float32)

    header = fits.Header()
    header["FILTER"] = "mixed_Starless"
    header["OBJECT"] = "Synthetic linear denoise self-test"
    fits.PrimaryHDU(data=image, header=header).writeto(
        path,
        overwrite=False,
        output_verify="fix",
    )



def self_test_execution_assessment(
    candidate: dict[str, Any],
) -> dict[str, Any]:
    """Validate real Siril execution without treating synthetic data as M16.

    The production quality assessment is still calculated and preserved, but
    its astrophotography-specific thresholds are not installation gates for a
    deliberately artificial 512 x 512 image.
    """
    source = candidate["source"]
    output = candidate["output"]
    quality = candidate["quality_assessment"]
    metrics = quality.get("metrics", {})
    failed: list[dict[str, Any]] = []

    def fail(metric: str, value: Any, requirement: str) -> None:
        failed.append(
            {
                "metric": metric,
                "value": value,
                "requirement": requirement,
            }
        )

    denoise_run = candidate["denoise_run"]
    preview_run = candidate["preview_run"]

    if denoise_run["exit_status"] != 0:
        fail(
            "denoise_exit_status",
            denoise_run["exit_status"],
            "must equal 0",
        )
    if denoise_run["timed_out"]:
        fail("denoise_timed_out", True, "must be false")
    if denoise_run["fatal_log_markers"]:
        fail(
            "denoise_fatal_log_markers",
            denoise_run["fatal_log_markers"],
            "must be empty",
        )

    if preview_run["exit_status"] != 0:
        fail(
            "preview_exit_status",
            preview_run["exit_status"],
            "must equal 0",
        )
    if preview_run["timed_out"]:
        fail("preview_timed_out", True, "must be false")
    if preview_run["fatal_log_markers"]:
        fail(
            "preview_fatal_log_markers",
            preview_run["fatal_log_markers"],
            "must be empty",
        )

    if source["sha256"] == output["sha256"]:
        fail(
            "source_output_sha256",
            source["sha256"],
            "source and output must differ",
        )

    for field in ("channels", "width", "height", "bitpix", "dtype"):
        if source[field] != output[field]:
            fail(
                f"preserve_{field}",
                {
                    "source": source[field],
                    "output": output[field],
                },
                "source and output must match",
            )

    if output["finite_fraction"] != 1.0:
        fail(
            "finite_fraction",
            output["finite_fraction"],
            "must equal 1.0",
        )

    correlation = metrics.get("correlation")
    if (
        correlation is None
        or not math.isfinite(float(correlation))
        or float(correlation) < 0.90
    ):
        fail(
            "correlation",
            correlation,
            "must be finite and at least 0.90",
        )

    relative_rms_change = metrics.get("relative_rms_change")
    if (
        relative_rms_change is None
        or not math.isfinite(float(relative_rms_change))
        or not (1.0e-8 <= float(relative_rms_change) <= 0.50)
    ):
        fail(
            "relative_rms_change",
            relative_rms_change,
            "must be finite and between 1e-8 and 0.50",
        )

    missing_previews = [
        path
        for path in candidate["previews"].values()
        if not Path(path).is_file()
    ]
    if missing_previews:
        fail(
            "missing_previews",
            missing_previews,
            "all before/after previews must exist",
        )

    satisfactory = not failed
    return {
        "status": "satisfactory" if satisfactory else "failed",
        "satisfactory": satisfactory,
        "failed_checks": failed,
        "production_quality_assessment_status": quality.get("status"),
        "production_quality_assessment_satisfactory": quality.get(
            "satisfactory"
        ),
        "interpretation": (
            "Siril executed NL-Bayes and preview generation correctly on the "
            "synthetic image. Production quality gates remain reserved for "
            "real project data."
            if satisfactory
            else "The real Siril execution self-test failed one or more "
            "format, process, output-change, or preview safeguards."
        ),
    }


def self_test(timeout_seconds: int) -> dict[str, Any]:
    siril = siril_version()
    root = (
        WORKSPACE
        / ".skill-self-tests"
        / "siril-linear-denoise"
        / unique_id()
    )
    root.mkdir(parents=True, exist_ok=False)
    source = root / "synthetic-starless-linear.fit"
    write_synthetic_fits(source)

    candidate = execute_denoise(
        source,
        root,
        timeout_seconds,
    )

    execution_assessment = self_test_execution_assessment(candidate)

    if not execution_assessment["satisfactory"]:
        raise LinearDenoiseError(
            "Linear-denoise execution self-test failed "
            f"{execution_assessment['failed_checks']}; evidence is preserved "
            f"at {root}"
        )

    return {
        "status": "success",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "self_test_directory": str(root),
        "siril": siril,
        "candidate": candidate,
        "execution_assessment": execution_assessment,
        "production_quality_assessment": candidate[
            "quality_assessment"
        ],
        "tests": [
            "real Siril NL-Bayes execution",
            "modulation 0.75",
            "default Cosmetic Correction enabled",
            "32-bit RGB FITS preservation",
            "finite output",
            "source/output dimension equality",
            "non-identical denoise output",
            "synthetic execution and output-change safeguards",
            "production quality assessment recorded diagnostically",
            "before and after linked previews",
            "evidence preservation",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run deterministic Siril linear NL-Bayes denoise on the "
            "canonical StarNet starless FITS."
        )
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command")

    self_test_parser = subparsers.add_parser("self-test")
    self_test_parser.add_argument("--timeout", type=int, default=1800)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--project", required=True)
    run_parser.add_argument("--timeout", type=int, default=7200)
    run_parser.add_argument(
        "--fresh-run",
        action="store_true",
        help=(
            "Execute NL-Bayes again. If processing/linear-denoise already "
            "exists, preserve it intact beneath the new evidence run before "
            "publishing."
        ),
    )

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--project", required=True)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.command is None:
        parser.print_help()
        return 2

    try:
        if args.command == "self-test":
            payload = self_test(args.timeout)
        elif args.command == "run":
            payload = run_project(
                workspace=WORKSPACE,
                project_name=args.project,
                timeout_seconds=args.timeout,
                fresh_run=args.fresh_run,
            )
        elif args.command == "status":
            payload = status_project(WORKSPACE, args.project)
        else:
            raise LinearDenoiseError(
                f"Unsupported command: {args.command}"
            )
    except Exception as exc:
        payload = {
            "status": "blocked",
            "helper_version": VERSION,
            "error": str(exc),
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2

    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("status") in ("success", "ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
