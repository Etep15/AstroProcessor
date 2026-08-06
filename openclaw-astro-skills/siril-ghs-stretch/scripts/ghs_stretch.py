#!/usr/bin/env python3
"""Bounded adaptive first-pass GHS stretch for the canonical starless image."""

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
import time
from typing import Any

import numpy as np
from astropy.io import fits


VERSION = "1.1.0"
WORKSPACE = Path(
    "/home/peter/.openclaw/workspace/agents/codewarrior"
)
SIRIL_APP = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/"
    ".toolchain/siril/1.4.4/squashfs-root/AppRun"
)
SIRIL_APPDIR = SIRIL_APP.parent
REQUIRED_SIRIL_VERSION = "1.4.4"
REQUIRED_DENOISE_VERSION = "1.0.1"

# Exact first-pass M16 settings from the successful manual Siril 1.4.4 run.
GHS_D = 4.4
GHS_B = 15.0
GHS_SP = 0.004
GHS_LP = 0.0
GHS_HP = 0.86
GHS_CLIPMODE = "rgbblend"
GHS_COLOUR_MODEL = "even"

MAX_CANDIDATES_LIMIT = 3
PARAMETER_BOUNDS = {
    "D": {"minimum": 3.8, "maximum": 5.0},
    "B": {"minimum": 10.0, "maximum": 20.0},
    "SP": {"minimum": 0.0025, "maximum": 0.0060},
    "LP": {"minimum": 0.0, "maximum": 0.0},
    "HP": {"minimum": 0.82, "maximum": 0.92},
}
SELECTION_TARGETS = {
    "output_luma_median": 0.085,
    "output_luma_p90": 0.35,
    "output_luma_p99": 0.60,
    "minimum_preferred_luma_correlation": 0.97,
}

FATAL_LOG_MARKERS = (
    "script execution failed",
    "command execution failed",
    "cannot load",
    "could not load",
    "not enough memory",
    "segmentation fault",
)


class GhsStretchError(RuntimeError):
    """Raised when the deterministic GHS stage cannot safely continue."""


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


def inspect_fits(path: Path) -> FitsEvidence:
    if not path.is_file():
        raise GhsStretchError(f"FITS file does not exist: {path}")

    with fits.open(path, memmap=True) as hdul:
        data = hdul[0].data
        header = hdul[0].header
        if data is None:
            raise GhsStretchError(f"FITS contains no image data: {path}")
        array = np.asarray(data)

        if array.ndim != 3:
            raise GhsStretchError(
                f"Expected a three-dimensional RGB FITS, found "
                f"{array.shape}: {path}"
            )
        channels, height, width = array.shape
        if channels != 3:
            raise GhsStretchError(
                f"Expected three channels, found {channels}: {path}"
            )
        if array.dtype.kind != "f" or array.dtype.itemsize != 4:
            raise GhsStretchError(
                f"Expected 32-bit floating-point FITS data, found "
                f"{array.dtype}: {path}"
            )

        finite = np.isfinite(array)
        finite_fraction = float(np.mean(finite))
        if finite_fraction == 0.0:
            minimum = maximum = median = math.nan
        else:
            values = array[finite]
            minimum = float(np.min(values))
            maximum = float(np.max(values))
            median = float(np.median(values))

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
    stable = processing / "ghs-pass1"
    return {
        "project": project,
        "processing": processing,
        "source": (
            processing
            / "linear-denoise"
            / "SHO-starless-linear-denoised.fit"
        ),
        "source_manifest": (
            processing
            / "linear-denoise"
            / "linear-denoise-manifest.json"
        ),
        "runs": project / ".siril-ghs-stretch",
        "stable": stable,
        "stable_output": stable / "SHO-starless-ghs-pass1.fit",
        "stable_before_preview": (
            stable
            / "SHO-starless-linear-denoised-before-linked.png"
        ),
        "stable_after_preview": (
            stable / "SHO-starless-ghs-pass1-linear.png"
        ),
        "stable_manifest": stable / "ghs-pass1-manifest.json",
    }


def siril_version() -> dict[str, Any]:
    if not SIRIL_APP.is_file() or not os.access(SIRIL_APP, os.X_OK):
        raise GhsStretchError(
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
        raise GhsStretchError(
            f"Could not read Siril version (exit {completed.returncode}): "
            f"{combined}"
        )
    if REQUIRED_SIRIL_VERSION not in combined:
        raise GhsStretchError(
            f"Expected Siril {REQUIRED_SIRIL_VERSION}; got: {combined}"
        )
    return {
        "version": REQUIRED_SIRIL_VERSION,
        "version_output": combined,
        "path": str(SIRIL_APP),
    }


def ght_arguments(command: str, parameters: dict[str, float]) -> str:
    return (
        f"{command} "
        f"-D={parameters['D']:.3f} "
        f"-B={parameters['B']:.3f} "
        f"-LP={parameters['LP']:.5f} "
        f"-SP={parameters['SP']:.5f} "
        f"-HP={parameters['HP']:.5f} "
        f"-clipmode={GHS_CLIPMODE} "
        f"-{GHS_COLOUR_MODEL}"
    )

def stretch_script_text(parameters: dict[str, float]) -> str:
    return "\n".join(
        (
            f"requires {REQUIRED_SIRIL_VERSION}",
            'load "SHO-starless-linear-denoised.fit"',
            ght_arguments("ght", parameters),
            'save "SHO-starless-ghs-pass1.fit"',
            ght_arguments("invght", parameters),
            'save "SHO-starless-ghs-pass1-roundtrip.fit"',
            "close",
            "",
        )
    )

def preview_script_text() -> str:
    return "\n".join(
        (
            f"requires {REQUIRED_SIRIL_VERSION}",
            'load "SHO-starless-linear-denoised.fit"',
            "autostretch -linked",
            (
                'savepng "../previews/'
                'SHO-starless-linear-denoised-before-linked"'
            ),
            "close",
            'load "SHO-starless-ghs-pass1.fit"',
            (
                'savepng "../previews/'
                'SHO-starless-ghs-pass1-linear"'
            ),
            "close",
            "",
        )
    )

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
        marker
        for marker in FATAL_LOG_MARKERS
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


def luma_sample(path: Path) -> np.ndarray:
    with fits.open(path, memmap=True) as hdul:
        array = np.asarray(hdul[0].data, dtype=np.float32)
    # Even-weighted luminance is the mean of R, G and B.
    luma = np.mean(array[:, ::3, ::3], axis=0, dtype=np.float64)
    return luma[np.isfinite(luma)]


def production_quality_assessment(
    source_path: Path,
    output_path: Path,
    roundtrip_path: Path,
) -> dict[str, Any]:
    source_evidence = inspect_fits(source_path)
    output_evidence = inspect_fits(output_path)
    roundtrip_evidence = inspect_fits(roundtrip_path)

    failed: list[dict[str, Any]] = []

    def fail(metric: str, value: Any, requirement: str) -> None:
        failed.append(
            {
                "metric": metric,
                "value": value,
                "requirement": requirement,
            }
        )

    if (
        source_evidence.width != output_evidence.width
        or source_evidence.height != output_evidence.height
        or source_evidence.channels != output_evidence.channels
    ):
        fail(
            "output_dimensions",
            {
                "source": [
                    source_evidence.channels,
                    source_evidence.height,
                    source_evidence.width,
                ],
                "output": [
                    output_evidence.channels,
                    output_evidence.height,
                    output_evidence.width,
                ],
            },
            "must match the source",
        )

    if (
        source_evidence.width != roundtrip_evidence.width
        or source_evidence.height != roundtrip_evidence.height
        or source_evidence.channels != roundtrip_evidence.channels
    ):
        fail(
            "roundtrip_dimensions",
            {
                "source": [
                    source_evidence.channels,
                    source_evidence.height,
                    source_evidence.width,
                ],
                "roundtrip": [
                    roundtrip_evidence.channels,
                    roundtrip_evidence.height,
                    roundtrip_evidence.width,
                ],
            },
            "must match the source",
        )

    if output_evidence.finite_fraction != 1.0:
        fail(
            "output_finite_fraction",
            output_evidence.finite_fraction,
            "must equal 1.0",
        )
    if roundtrip_evidence.finite_fraction != 1.0:
        fail(
            "roundtrip_finite_fraction",
            roundtrip_evidence.finite_fraction,
            "must equal 1.0",
        )

    if output_evidence.sha256 == source_evidence.sha256:
        fail(
            "output_sha256",
            output_evidence.sha256,
            "must differ from the source",
        )

    with fits.open(source_path, memmap=True) as hdul:
        source = np.asarray(hdul[0].data, dtype=np.float32)
    with fits.open(output_path, memmap=True) as hdul:
        output = np.asarray(hdul[0].data, dtype=np.float32)
    with fits.open(roundtrip_path, memmap=True) as hdul:
        roundtrip = np.asarray(hdul[0].data, dtype=np.float32)

    output_finite = np.isfinite(output)
    low_clip_fraction = float(np.mean(output[output_finite] <= 0.0))
    high_clip_fraction = float(np.mean(output[output_finite] >= 1.0))

    source_luma = luma_sample(source_path)
    output_luma = luma_sample(output_path)

    source_median = float(np.median(source_luma))
    output_median = float(np.median(output_luma))
    source_p90 = float(np.percentile(source_luma, 90.0))
    output_p90 = float(np.percentile(output_luma, 90.0))
    source_p99 = float(np.percentile(source_luma, 99.0))
    output_p99 = float(np.percentile(output_luma, 99.0))

    source_centre = source_luma - float(np.mean(source_luma))
    output_centre = output_luma - float(np.mean(output_luma))
    denominator = math.sqrt(
        float(np.dot(source_centre, source_centre))
        * float(np.dot(output_centre, output_centre))
    )
    luma_correlation = (
        float(np.dot(source_centre, output_centre) / denominator)
        if denominator > 0.0
        else math.nan
    )

    valid_roundtrip = (
        np.isfinite(source[:, ::3, ::3])
        & np.isfinite(roundtrip[:, ::3, ::3])
    )
    source_sample = source[:, ::3, ::3][valid_roundtrip].astype(
        np.float64,
        copy=False,
    )
    roundtrip_sample = roundtrip[:, ::3, ::3][valid_roundtrip].astype(
        np.float64,
        copy=False,
    )
    source_span = max(
        float(np.percentile(source_sample, 99.5))
        - float(np.percentile(source_sample, 0.5)),
        1.0e-12,
    )
    roundtrip_delta = roundtrip_sample - source_sample
    roundtrip_relative_rms = float(
        math.sqrt(float(np.mean(roundtrip_delta * roundtrip_delta)))
        / source_span
    )
    roundtrip_max_absolute = float(
        np.max(np.abs(roundtrip_delta))
    )

    background_lift = (
        output_median / source_median
        if source_median > 0.0
        else math.nan
    )

    thresholds = {
        "maximum_low_clip_fraction": 1.0e-7,
        "maximum_high_clip_fraction": 1.0e-7,
        "minimum_luma_correlation": 0.85,
        "minimum_output_median": 0.02,
        "maximum_output_median": 0.70,
        "minimum_output_p99": 0.08,
        "maximum_output_p99": 0.995,
        "minimum_background_lift": 2.0,
        "maximum_background_lift": 500.0,
        "maximum_roundtrip_relative_rms": 0.01,
        "maximum_roundtrip_absolute_error": 0.01,
    }

    if low_clip_fraction > thresholds["maximum_low_clip_fraction"]:
        fail(
            "low_clip_fraction",
            low_clip_fraction,
            f"must be <= {thresholds['maximum_low_clip_fraction']}",
        )
    if high_clip_fraction > thresholds["maximum_high_clip_fraction"]:
        fail(
            "high_clip_fraction",
            high_clip_fraction,
            f"must be <= {thresholds['maximum_high_clip_fraction']}",
        )
    if (
        not math.isfinite(luma_correlation)
        or luma_correlation < thresholds["minimum_luma_correlation"]
    ):
        fail(
            "luma_correlation",
            luma_correlation,
            f"must be >= {thresholds['minimum_luma_correlation']}",
        )
    if not (
        thresholds["minimum_output_median"]
        <= output_median
        <= thresholds["maximum_output_median"]
    ):
        fail(
            "output_luma_median",
            output_median,
            (
                f"must be between {thresholds['minimum_output_median']} "
                f"and {thresholds['maximum_output_median']}"
            ),
        )
    if not (
        thresholds["minimum_output_p99"]
        <= output_p99
        <= thresholds["maximum_output_p99"]
    ):
        fail(
            "output_luma_p99",
            output_p99,
            (
                f"must be between {thresholds['minimum_output_p99']} "
                f"and {thresholds['maximum_output_p99']}"
            ),
        )
    if (
        not math.isfinite(background_lift)
        or not (
            thresholds["minimum_background_lift"]
            <= background_lift
            <= thresholds["maximum_background_lift"]
        )
    ):
        fail(
            "background_lift",
            background_lift,
            (
                f"must be between {thresholds['minimum_background_lift']} "
                f"and {thresholds['maximum_background_lift']}"
            ),
        )
    if (
        roundtrip_relative_rms
        > thresholds["maximum_roundtrip_relative_rms"]
    ):
        fail(
            "roundtrip_relative_rms",
            roundtrip_relative_rms,
            (
                "must be <= "
                f"{thresholds['maximum_roundtrip_relative_rms']}"
            ),
        )
    if (
        roundtrip_max_absolute
        > thresholds["maximum_roundtrip_absolute_error"]
    ):
        fail(
            "roundtrip_max_absolute_error",
            roundtrip_max_absolute,
            (
                "must be <= "
                f"{thresholds['maximum_roundtrip_absolute_error']}"
            ),
        )

    satisfactory = not failed
    return {
        "status": "satisfactory" if satisfactory else "needs_review",
        "satisfactory": satisfactory,
        "failed_checks": failed,
        "metrics": {
            "low_clip_fraction": low_clip_fraction,
            "high_clip_fraction": high_clip_fraction,
            "source_luma_median": source_median,
            "output_luma_median": output_median,
            "source_luma_p90": source_p90,
            "output_luma_p90": output_p90,
            "source_luma_p99": source_p99,
            "output_luma_p99": output_p99,
            "background_lift": background_lift,
            "luma_correlation": luma_correlation,
            "roundtrip_relative_rms": roundtrip_relative_rms,
            "roundtrip_max_absolute_error": roundtrip_max_absolute,
            "source_minimum": source_evidence.minimum,
            "source_maximum": source_evidence.maximum,
            "output_minimum": output_evidence.minimum,
            "output_maximum": output_evidence.maximum,
        },
        "thresholds": thresholds,
        "interpretation": (
            "The first GHS pass lifted the denoised starless image without "
            "channel clipping, and inverse GHT recovered the linear source "
            "within the accepted numerical tolerance."
            if satisfactory
            else "The first GHS candidate requires review because one or "
            "more clipping, histogram, structure, or inverse-roundtrip "
            "safeguards did not pass."
        ),
    }


def validate_source(
    paths: dict[str, Path],
) -> tuple[dict[str, Any], FitsEvidence]:
    if not paths["project"].is_dir():
        raise GhsStretchError(
            f"Project does not exist: {paths['project']}"
        )
    if not paths["source_manifest"].is_file():
        raise GhsStretchError(
            f"Linear-denoise manifest is missing: "
            f"{paths['source_manifest']}"
        )

    manifest = json.loads(
        paths["source_manifest"].read_text(encoding="utf-8")
    )
    if manifest.get("status") != "ready":
        raise GhsStretchError(
            "Linear-denoise manifest status is not ready."
        )
    if not manifest.get("downstream_linear_processing_permitted"):
        raise GhsStretchError(
            "Linear-denoise manifest does not permit downstream processing."
        )
    if manifest.get("helper_version") != REQUIRED_DENOISE_VERSION:
        raise GhsStretchError(
            f"Expected linear-denoise helper {REQUIRED_DENOISE_VERSION}; "
            f"manifest reports {manifest.get('helper_version')}."
        )

    source_evidence = inspect_fits(paths["source"])
    expected_hash = manifest.get("output", {}).get("sha256")
    if not expected_hash:
        raise GhsStretchError(
            "Linear-denoise manifest does not record the output SHA-256."
        )
    if source_evidence.sha256 != expected_hash:
        raise GhsStretchError(
            "Canonical denoised FITS checksum does not match the "
            "linear-denoise manifest."
        )

    return manifest, source_evidence



def clamp_parameter(name: str, value: float) -> float:
    bounds = PARAMETER_BOUNDS[name]
    return min(bounds["maximum"], max(bounds["minimum"], value))


def normalize_parameters(parameters: dict[str, float]) -> dict[str, float]:
    normalized = {
        "D": round(clamp_parameter("D", parameters["D"]), 3),
        "B": round(clamp_parameter("B", parameters["B"]), 3),
        "SP": round(clamp_parameter("SP", parameters["SP"]), 5),
        "LP": 0.0,
        "HP": round(clamp_parameter("HP", parameters["HP"]), 5),
    }
    return normalized


def baseline_parameters() -> dict[str, float]:
    return normalize_parameters(
        {
            "D": GHS_D,
            "B": GHS_B,
            "SP": GHS_SP,
            "LP": GHS_LP,
            "HP": GHS_HP,
        }
    )


def histogram_classification(metrics: dict[str, Any]) -> str:
    median = float(metrics["output_luma_median"])
    p90 = float(metrics["output_luma_p90"])
    p99 = float(metrics["output_luma_p99"])
    low_clip = float(metrics["low_clip_fraction"])
    high_clip = float(metrics["high_clip_fraction"])

    if (
        low_clip > 0.0
        or high_clip > 0.0
        or median > 0.18
        or p90 > 0.60
        or p99 > 0.90
    ):
        return "too_strong"

    if median < 0.05 or p90 < 0.22 or p99 < 0.35:
        return "too_gentle"

    return "balanced"


def parameters_key(parameters: dict[str, float]) -> tuple[float, ...]:
    return (
        parameters["D"],
        parameters["B"],
        parameters["SP"],
        parameters["LP"],
        parameters["HP"],
    )


def ensure_unique_parameters(
    proposed: dict[str, float],
    existing: list[dict[str, float]],
    *,
    preferred_direction: str,
) -> dict[str, float]:
    normalized = normalize_parameters(proposed)
    existing_keys = {parameters_key(item) for item in existing}
    if parameters_key(normalized) not in existing_keys:
        return normalized

    deltas = (
        (0.10, 0.50, 0.00010, -0.005)
        if preferred_direction == "stronger"
        else (-0.10, -0.50, -0.00010, 0.005)
    )
    alternate = normalize_parameters(
        {
            "D": normalized["D"] + deltas[0],
            "B": normalized["B"] + deltas[1],
            "SP": normalized["SP"] + deltas[2],
            "LP": 0.0,
            "HP": normalized["HP"] + deltas[3],
        }
    )
    if parameters_key(alternate) not in existing_keys:
        return alternate

    reverse = normalize_parameters(
        {
            "D": normalized["D"] - deltas[0],
            "B": normalized["B"] - deltas[1],
            "SP": normalized["SP"] - deltas[2],
            "LP": 0.0,
            "HP": normalized["HP"] - deltas[3],
        }
    )
    if parameters_key(reverse) not in existing_keys:
        return reverse

    raise GhsStretchError(
        "Could not produce a unique bounded adaptive parameter set."
    )


def plan_second_candidate(
    baseline: dict[str, Any],
) -> tuple[dict[str, float], str]:
    metrics = baseline["quality_assessment"]["metrics"]
    classification = histogram_classification(metrics)
    current = baseline["parameters"]

    if classification == "too_gentle":
        proposed = {
            "D": current["D"] + 0.35,
            "B": current["B"] + 2.0,
            "SP": current["SP"] + 0.00040,
            "LP": 0.0,
            "HP": current["HP"] - 0.010,
        }
        reason = (
            "Baseline histogram was too subdued; apply the predefined "
            "stronger step."
        )
        direction = "stronger"
    elif classification == "too_strong":
        proposed = {
            "D": current["D"] - 0.35,
            "B": current["B"] - 2.0,
            "SP": current["SP"] - 0.00040,
            "LP": 0.0,
            "HP": current["HP"] + 0.020,
        }
        reason = (
            "Baseline histogram or clipping was too strong; apply the "
            "predefined gentler step."
        )
        direction = "gentler"
    else:
        proposed = {
            "D": current["D"] - 0.25,
            "B": current["B"] - 1.5,
            "SP": current["SP"],
            "LP": 0.0,
            "HP": current["HP"] + 0.010,
        }
        reason = (
            "Baseline was technically balanced; create the predefined "
            "gentler comparison candidate for visual review."
        )
        direction = "gentler"

    parameters = ensure_unique_parameters(
        proposed,
        [baseline["parameters"]],
        preferred_direction=direction,
    )
    return parameters, reason


def plan_third_candidate(
    baseline: dict[str, Any],
    second: dict[str, Any],
) -> tuple[dict[str, float], str]:
    metrics = second["quality_assessment"]["metrics"]
    classification = histogram_classification(metrics)
    current = second["parameters"]
    existing = [baseline["parameters"], second["parameters"]]

    if classification == "too_gentle":
        proposed = {
            "D": current["D"] + 0.20,
            "B": current["B"] + 1.0,
            "SP": current["SP"] + 0.00020,
            "LP": 0.0,
            "HP": current["HP"] - 0.005,
        }
        direction = "stronger"
        reason = (
            "Second candidate remained too subdued; apply one final "
            "predefined stronger refinement."
        )
    elif classification == "too_strong":
        proposed = {
            "D": current["D"] - 0.20,
            "B": current["B"] - 1.0,
            "SP": current["SP"] - 0.00020,
            "LP": 0.0,
            "HP": current["HP"] + 0.010,
        }
        direction = "gentler"
        reason = (
            "Second candidate remained too strong; apply one final "
            "predefined gentler refinement."
        )
    else:
        median_error = (
            SELECTION_TARGETS["output_luma_median"]
            - float(metrics["output_luma_median"])
        )
        p90_error = (
            SELECTION_TARGETS["output_luma_p90"]
            - float(metrics["output_luma_p90"])
        )
        p99_error = (
            SELECTION_TARGETS["output_luma_p99"]
            - float(metrics["output_luma_p99"])
        )
        combined = (
            median_error / 0.085
            + p90_error / 0.35
            + 0.5 * p99_error / 0.60
        )

        if combined > 0.08:
            proposed = {
                "D": current["D"] + 0.12,
                "B": current["B"] + 0.60,
                "SP": current["SP"] + 0.00010,
                "LP": 0.0,
                "HP": current["HP"] - 0.005,
            }
            direction = "stronger"
            reason = (
                "Second candidate was balanced but below the predefined "
                "pass-1 histogram targets; apply a final small stronger "
                "refinement."
            )
        elif combined < -0.08:
            proposed = {
                "D": current["D"] - 0.12,
                "B": current["B"] - 0.60,
                "SP": current["SP"] - 0.00010,
                "LP": 0.0,
                "HP": current["HP"] + 0.005,
            }
            direction = "gentler"
            reason = (
                "Second candidate was balanced but above the predefined "
                "pass-1 histogram targets; apply a final small gentler "
                "refinement."
            )
        else:
            baseline_score = float(baseline["selection_score"])
            second_score = float(second["selection_score"])
            better = (
                baseline["parameters"]
                if baseline_score <= second_score
                else second["parameters"]
            )
            other = (
                second["parameters"]
                if baseline_score <= second_score
                else baseline["parameters"]
            )
            proposed = {
                "D": (better["D"] + other["D"]) / 2.0,
                "B": (better["B"] + other["B"]) / 2.0,
                "SP": (better["SP"] + other["SP"]) / 2.0,
                "LP": 0.0,
                "HP": (better["HP"] + other["HP"]) / 2.0,
            }
            direction = (
                "stronger"
                if proposed["D"] >= current["D"]
                else "gentler"
            )
            reason = (
                "Second candidate was already near the predefined targets; "
                "use the bounded midpoint between the two best numerical "
                "solutions as the final refinement."
            )

    parameters = ensure_unique_parameters(
        proposed,
        existing,
        preferred_direction=direction,
    )
    return parameters, reason


def candidate_selection_score(
    quality_assessment: dict[str, Any],
) -> float:
    metrics = quality_assessment["metrics"]
    targets = SELECTION_TARGETS

    median = max(float(metrics["output_luma_median"]), 1.0e-9)
    target_median = targets["output_luma_median"]
    p90 = float(metrics["output_luma_p90"])
    p99 = float(metrics["output_luma_p99"])
    correlation = float(metrics["luma_correlation"])
    low_clip = float(metrics["low_clip_fraction"])
    high_clip = float(metrics["high_clip_fraction"])
    roundtrip = float(metrics["roundtrip_relative_rms"])

    score = (
        1.5 * abs(math.log(median / target_median))
        + abs(p90 - targets["output_luma_p90"]) / 0.20
        + abs(p99 - targets["output_luma_p99"]) / 0.25
        + 12.0
        * max(
            0.0,
            targets["minimum_preferred_luma_correlation"] - correlation,
        )
        + 25.0 * roundtrip
        + 1.0e6 * (low_clip + high_clip)
    )
    if not quality_assessment["satisfactory"]:
        score += 1000.0
    return float(score)


def recommended_candidate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    eligible = [
        candidate
        for candidate in candidates
        if candidate["quality_assessment"]["satisfactory"]
    ]
    if not eligible:
        return None
    return min(
        eligible,
        key=lambda candidate: (
            float(candidate["selection_score"]),
            candidate["candidate"],
        ),
    )


def execute_candidate(
    source_path: Path,
    run_root: Path,
    timeout_seconds: int,
    *,
    candidate_index: int,
    parameters: dict[str, float],
    adaptation_reason: str,
) -> dict[str, Any]:
    candidate_name = f"candidate-{candidate_index:02d}"
    candidate = run_root / candidate_name
    work = candidate / "work"
    logs = candidate / "logs"
    previews = candidate / "previews"
    work.mkdir(parents=True, exist_ok=False)
    logs.mkdir()
    previews.mkdir()

    parameters = normalize_parameters(parameters)
    working_source = work / "SHO-starless-linear-denoised.fit"
    shutil.copy2(source_path, working_source)

    stretch_script = candidate / "ghs-pass1.ssf"
    stretch_script.write_text(
        stretch_script_text(parameters),
        encoding="utf-8",
    )
    stretch_run = run_siril_script(
        directory=work,
        script=stretch_script,
        stdout_log=logs / "stretch-stdout.log",
        stderr_log=logs / "stretch-stderr.log",
        timeout_seconds=timeout_seconds,
    )
    if (
        stretch_run["exit_status"] != 0
        or stretch_run["timed_out"]
        or stretch_run["fatal_log_markers"]
    ):
        raise GhsStretchError(
            f"Siril GHS pass 1 failed for {candidate_name}; evidence is "
            f"preserved at {candidate}"
        )

    output = work / "SHO-starless-ghs-pass1.fit"
    roundtrip = work / "SHO-starless-ghs-pass1-roundtrip.fit"
    source_evidence = inspect_fits(working_source)
    output_evidence = inspect_fits(output)
    roundtrip_evidence = inspect_fits(roundtrip)

    preview_script = candidate / "previews.ssf"
    preview_script.write_text(
        preview_script_text(),
        encoding="utf-8",
    )
    preview_run = run_siril_script(
        directory=work,
        script=preview_script,
        stdout_log=logs / "preview-stdout.log",
        stderr_log=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 600),
    )

    before_preview = (
        previews / "SHO-starless-linear-denoised-before-linked.png"
    )
    after_preview = previews / "SHO-starless-ghs-pass1-linear.png"
    preview_failures: list[str] = []
    if preview_run["exit_status"] != 0:
        preview_failures.append(
            f"preview exit status {preview_run['exit_status']}"
        )
    if preview_run["timed_out"]:
        preview_failures.append("preview timed out")
    if preview_run["fatal_log_markers"]:
        preview_failures.append(
            f"preview fatal markers {preview_run['fatal_log_markers']}"
        )
    for preview in (before_preview, after_preview):
        if not preview.is_file():
            preview_failures.append(f"missing preview {preview}")
    if preview_failures:
        raise GhsStretchError(
            f"Preview generation failed ({preview_failures}); evidence is "
            f"preserved at {candidate}"
        )

    quality = production_quality_assessment(
        working_source,
        output,
        roundtrip,
    )
    score = candidate_selection_score(quality)

    return {
        "candidate": candidate_name,
        "candidate_directory": str(candidate),
        "created_at": utc_now(),
        "helper_version": VERSION,
        "parameters": parameters,
        "parameter_bounds": PARAMETER_BOUNDS,
        "adaptation_reason": adaptation_reason,
        "histogram_classification": histogram_classification(
            quality["metrics"]
        ),
        "selection_score": score,
        "method": {
            "algorithm": "Siril Generalised Hyperbolic Stretch",
            "command": ght_arguments("ght", parameters),
            "inverse_command": ght_arguments("invght", parameters),
            "display_stretch_factor": parameters["D"],
            "local_stretch_intensity_B": parameters["B"],
            "symmetry_point_SP": parameters["SP"],
            "shadow_protection_LP": parameters["LP"],
            "highlight_protection_HP": parameters["HP"],
            "colour_model": "even weighted luminance",
            "clip_mode": "RGB Blend",
            "channels": "RGB",
        },
        "source": asdict(source_evidence),
        "output": asdict(output_evidence),
        "roundtrip": asdict(roundtrip_evidence),
        "stretch_script": str(stretch_script),
        "stretch_script_sha256": sha256_file(stretch_script),
        "stretch_run": stretch_run,
        "preview_script": str(preview_script),
        "preview_run": preview_run,
        "previews": {
            "before_linked": str(before_preview),
            "after_linear": str(after_preview),
        },
        "quality_assessment": quality,
        "status": (
            "satisfactory"
            if quality["satisfactory"]
            else "needs_review"
        ),
    }

def publish(
    *,
    paths: dict[str, Path],
    run_root: Path,
    candidate: dict[str, Any],
    candidates: list[dict[str, Any]],
    recommended: dict[str, Any] | None,
    fresh_run: bool,
    source_manifest: dict[str, Any],
    source_evidence: FitsEvidence,
    siril: dict[str, Any],
    visual_selection_notes: str,
) -> dict[str, Any]:
    existing = paths["stable"].exists()
    if existing and not fresh_run:
        raise GhsStretchError(
            f"Canonical GHS pass-1 directory already exists: "
            f"{paths['stable']}. Use --fresh-run to publish the selected "
            "new candidate while preserving the previous directory intact."
        )

    if not candidate["quality_assessment"]["satisfactory"]:
        raise GhsStretchError(
            "The selected GHS candidate did not pass production safeguards; "
            "canonical output was not changed."
        )
    if not visual_selection_notes.strip():
        raise GhsStretchError(
            "Visual selection notes are required before publication."
        )

    publish_dir = run_root / "publish-staging"
    if publish_dir.exists():
        raise GhsStretchError(
            f"Publication staging already exists: {publish_dir}"
        )
    publish_dir.mkdir(parents=True, exist_ok=False)

    candidate_dir = Path(candidate["candidate_directory"])
    candidate_output = (
        candidate_dir / "work" / "SHO-starless-ghs-pass1.fit"
    )
    candidate_before = (
        candidate_dir
        / "previews"
        / "SHO-starless-linear-denoised-before-linked.png"
    )
    candidate_after = (
        candidate_dir
        / "previews"
        / "SHO-starless-ghs-pass1-linear.png"
    )

    staged_output = publish_dir / "SHO-starless-ghs-pass1.fit"
    staged_before = (
        publish_dir
        / "SHO-starless-linear-denoised-before-linked.png"
    )
    staged_after = (
        publish_dir / "SHO-starless-ghs-pass1-linear.png"
    )
    shutil.copy2(candidate_output, staged_output)
    shutil.copy2(candidate_before, staged_before)
    shutil.copy2(candidate_after, staged_after)

    staged_evidence = inspect_fits(staged_output)
    expected_hash = candidate["output"]["sha256"]
    if staged_evidence.sha256 != expected_hash:
        raise GhsStretchError(
            "Selected candidate checksum changed during publication staging."
        )

    final_evidence = asdict(staged_evidence)
    final_evidence["path"] = str(paths["stable_output"])

    previous = (
        run_root / "previous-processing-ghs-pass1"
        if existing
        else None
    )
    if previous is not None and previous.exists():
        raise GhsStretchError(
            f"Preservation destination already exists: {previous}"
        )

    manifest = {
        "schema_version": 2,
        "created_at": utc_now(),
        "status": "ready",
        "helper_version": VERSION,
        "project": paths["project"].name,
        "project_path": str(paths["project"]),
        "adaptive_policy": {
            "maximum_total_candidates": MAX_CANDIDATES_LIMIT,
            "parameter_bounds": PARAMETER_BOUNDS,
            "selection_targets": SELECTION_TARGETS,
            "baseline_is_proven_manual_configuration": True,
            "arbitrary_parameters_permitted": False,
        },
        "source": asdict(source_evidence),
        "source_linear_denoise_manifest": str(
            paths["source_manifest"]
        ),
        "source_linear_denoise_status": source_manifest.get("status"),
        "source_linear_denoise_helper_version": source_manifest.get(
            "helper_version"
        ),
        "candidates": candidates,
        "recommended_candidate": (
            recommended["candidate"] if recommended is not None else None
        ),
        "recommended_selection_score": (
            recommended["selection_score"]
            if recommended is not None
            else None
        ),
        "selected_candidate": candidate["candidate"],
        "selected_candidate_was_recommended": (
            recommended is not None
            and recommended["candidate"] == candidate["candidate"]
        ),
        "visual_selection": {
            "required": True,
            "reviewer": "CodeWarrior",
            "notes": visual_selection_notes.strip(),
            "satisfactory_candidates_compared": [
                item["candidate"]
                for item in candidates
                if item["quality_assessment"]["satisfactory"]
            ],
        },
        "method": candidate["method"],
        "quality_assessment": candidate["quality_assessment"],
        "output": final_evidence,
        "roundtrip_evidence": candidate["roundtrip"],
        "previews": {
            "before_linked": str(paths["stable_before_preview"]),
            "after_linear": str(paths["stable_after_preview"]),
        },
        "stable_paths": {
            "directory": str(paths["stable"]),
            "output": str(paths["stable_output"]),
            "before_preview": str(paths["stable_before_preview"]),
            "after_preview": str(paths["stable_after_preview"]),
            "manifest": str(paths["stable_manifest"]),
        },
        "previous_processing_ghs_pass1_preserved_at": (
            str(previous) if previous is not None else None
        ),
        "publication_method": (
            "generate all bounded candidates, require visual selection, "
            "validate the selected candidate, preserve the previous "
            "canonical directory, then atomically rename staging"
        ),
        "siril": siril,
        "visual_review_completed": True,
        "ghs_pass2_processing_permitted": True,
    }
    json_dump_atomic(
        publish_dir / "ghs-pass1-manifest.json",
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
    max_candidates: int,
) -> dict[str, Any]:
    if max_candidates < 1 or max_candidates > MAX_CANDIDATES_LIMIT:
        raise GhsStretchError(
            f"max-candidates must be between 1 and "
            f"{MAX_CANDIDATES_LIMIT}."
        )

    paths = project_paths(workspace, project_name)
    if paths["stable"].exists() and not fresh_run:
        raise GhsStretchError(
            f"Canonical output already exists: {paths['stable']}. "
            "This command did not reuse it. Use --fresh-run to generate a "
            "new adaptive candidate set while preserving the current result."
        )

    siril = siril_version()
    source_manifest, source_evidence = validate_source(paths)

    run_started_at = utc_now()
    run_root = paths["runs"] / f"ghs-pass1-{unique_id()}"
    run_root.mkdir(parents=True, exist_ok=False)

    candidates: list[dict[str, Any]] = []

    baseline = execute_candidate(
        paths["source"],
        run_root,
        timeout_seconds,
        candidate_index=0,
        parameters=baseline_parameters(),
        adaptation_reason=(
            "Proven manual M16 baseline: D=4.4, B=15, SP=0.004, "
            "LP=0, HP=0.86."
        ),
    )
    candidates.append(baseline)

    if max_candidates >= 2:
        second_parameters, second_reason = plan_second_candidate(
            baseline
        )
        second = execute_candidate(
            paths["source"],
            run_root,
            timeout_seconds,
            candidate_index=1,
            parameters=second_parameters,
            adaptation_reason=second_reason,
        )
        candidates.append(second)

    if max_candidates >= 3:
        third_parameters, third_reason = plan_third_candidate(
            baseline,
            candidates[1],
        )
        third = execute_candidate(
            paths["source"],
            run_root,
            timeout_seconds,
            candidate_index=2,
            parameters=third_parameters,
            adaptation_reason=third_reason,
        )
        candidates.append(third)

    recommended = recommended_candidate(candidates)
    satisfactory_candidates = [
        candidate["candidate"]
        for candidate in candidates
        if candidate["quality_assessment"]["satisfactory"]
    ]

    status = (
        "awaiting_visual_selection"
        if satisfactory_candidates
        else "needs_review"
    )
    run_record = {
        "schema_version": 2,
        "status": status,
        "created_at": utc_now(),
        "run_started_at": run_started_at,
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "project_name": project_name,
        "run_root": str(run_root),
        "fresh_run_requested": bool(fresh_run),
        "maximum_total_candidates": MAX_CANDIDATES_LIMIT,
        "requested_candidate_count": max_candidates,
        "completed_candidate_count": len(candidates),
        "parameter_bounds": PARAMETER_BOUNDS,
        "selection_targets": SELECTION_TARGETS,
        "source": asdict(source_evidence),
        "source_manifest": str(paths["source_manifest"]),
        "source_manifest_sha256": sha256_file(
            paths["source_manifest"]
        ),
        "siril": siril,
        "candidates": candidates,
        "satisfactory_candidates": satisfactory_candidates,
        "recommended_candidate": (
            recommended["candidate"] if recommended is not None else None
        ),
        "recommended_selection_score": (
            recommended["selection_score"]
            if recommended is not None
            else None
        ),
        "canonical_output_changed": False,
        "visual_selection_required": True,
        "publish_command_template": (
            f"{Path(__file__)} publish --project "
            f"{json.dumps(project_name)} --run-root "
            f"{json.dumps(str(run_root))} --candidate <candidate-XX> "
            f"--visual-notes <review-notes> --fresh-run"
        ),
        "ghs_pass2_processing_permitted": False,
        "message": (
            "Compare every satisfactory after-preview and publish exactly "
            "one candidate using the publish subcommand."
            if satisfactory_candidates
            else "No candidate passed all production safeguards."
        ),
    }
    json_dump_atomic(run_root / "run-manifest.json", run_record)
    return run_record


def publish_project(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
    candidate_name: str,
    visual_selection_notes: str,
    fresh_run: bool,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    run_manifest_path = run_root / "run-manifest.json"
    if not run_manifest_path.is_file():
        raise GhsStretchError(
            f"Adaptive run manifest does not exist: {run_manifest_path}"
        )

    run_record = json.loads(
        run_manifest_path.read_text(encoding="utf-8")
    )
    if run_record.get("helper_version") != VERSION:
        raise GhsStretchError(
            f"Run helper version {run_record.get('helper_version')} does "
            f"not match installed version {VERSION}."
        )
    if run_record.get("project_name") != project_name:
        raise GhsStretchError(
            "Run manifest project does not match the requested project."
        )
    if run_record.get("canonical_output_changed"):
        raise GhsStretchError(
            "This adaptive run has already been published."
        )

    candidates = run_record.get("candidates", [])
    matches = [
        candidate
        for candidate in candidates
        if candidate.get("candidate") == candidate_name
    ]
    if len(matches) != 1:
        raise GhsStretchError(
            f"Candidate {candidate_name!r} is not uniquely present in the "
            "adaptive run."
        )
    candidate = matches[0]
    if not candidate["quality_assessment"]["satisfactory"]:
        raise GhsStretchError(
            f"Candidate {candidate_name} is not satisfactory and cannot "
            "be published."
        )

    recommended = recommended_candidate(candidates)
    source_manifest, source_evidence = validate_source(paths)
    if source_evidence.sha256 != run_record["source"]["sha256"]:
        raise GhsStretchError(
            "Current denoised source no longer matches the adaptive run."
        )

    siril = siril_version()
    manifest = publish(
        paths=paths,
        run_root=run_root,
        candidate=candidate,
        candidates=candidates,
        recommended=recommended,
        fresh_run=fresh_run,
        source_manifest=source_manifest,
        source_evidence=source_evidence,
        siril=siril,
        visual_selection_notes=visual_selection_notes,
    )

    result = {
        "status": "ready",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "selected_candidate": candidate_name,
        "recommended_candidate": (
            recommended["candidate"] if recommended is not None else None
        ),
        "selected_candidate_was_recommended": (
            recommended is not None
            and recommended["candidate"] == candidate_name
        ),
        "visual_selection_notes": visual_selection_notes.strip(),
        "stable_directory": str(paths["stable"]),
        "stable_output": str(paths["stable_output"]),
        "stable_before_preview": str(paths["stable_before_preview"]),
        "stable_after_preview": str(paths["stable_after_preview"]),
        "stable_manifest": str(paths["stable_manifest"]),
        "previous_processing_ghs_pass1_preserved_at": manifest.get(
            "previous_processing_ghs_pass1_preserved_at"
        ),
        "canonical_output_changed": True,
        "visual_review_completed": True,
        "ghs_pass2_processing_permitted": True,
        "manifest": manifest,
    }

    run_record["status"] = "published"
    run_record["published_at"] = utc_now()
    run_record["canonical_output_changed"] = True
    run_record["selected_candidate"] = candidate_name
    run_record["visual_selection_notes"] = visual_selection_notes.strip()
    run_record["stable_manifest"] = str(paths["stable_manifest"])
    run_record["ghs_pass2_processing_permitted"] = True
    json_dump_atomic(run_manifest_path, run_record)
    json_dump_atomic(run_root / "publication-result.json", result)
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
            "ghs_pass2_processing_permitted": False,
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
        "adaptive_policy": manifest.get("adaptive_policy"),
        "candidate_count": len(manifest.get("candidates", [])),
        "recommended_candidate": manifest.get("recommended_candidate"),
        "selected_candidate": manifest.get("selected_candidate"),
        "selected_candidate_was_recommended": manifest.get(
            "selected_candidate_was_recommended"
        ),
        "visual_selection": manifest.get("visual_selection"),
        "source": manifest.get("source"),
        "method": manifest.get("method"),
        "quality_assessment": manifest.get("quality_assessment"),
        "roundtrip_evidence": manifest.get("roundtrip_evidence"),
        "output": output_evidence,
        "previews": manifest.get("previews"),
        "previous_processing_ghs_pass1_preserved_at": manifest.get(
            "previous_processing_ghs_pass1_preserved_at"
        ),
        "visual_review_completed": manifest.get(
            "visual_review_completed",
            False,
        ),
        "ghs_pass2_processing_permitted": ready,
    }

def write_synthetic_fits(path: Path) -> None:
    rng = np.random.default_rng(440015)
    height = width = 512
    yy, xx = np.mgrid[0:height, 0:width]
    image = np.empty((3, height, width), dtype=np.float32)

    nebula = (
        0.0026
        + 0.010 * np.exp(
            -(
                ((xx - 275.0) / 135.0) ** 2
                + ((yy - 250.0) / 105.0) ** 2
            )
        )
        + 0.0015 * np.sin(xx / 37.0) * np.cos(yy / 51.0)
    )
    for channel, scale in enumerate((1.0, 0.55, 0.32)):
        image[channel] = nebula * scale

    noise = rng.normal(0.0, 0.00008, image.shape).astype(np.float32)
    image += noise
    image = np.clip(image, 0.0002, 0.03).astype(np.float32)

    header = fits.Header()
    header["FILTER"] = "mixed_Starless"
    header["OBJECT"] = "Synthetic GHS self-test"
    fits.PrimaryHDU(data=image, header=header).writeto(
        path,
        overwrite=False,
        output_verify="fix",
    )


def self_test_execution_assessment(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    failed: list[dict[str, Any]] = []

    def fail(metric: str, value: Any, requirement: str) -> None:
        failed.append(
            {
                "metric": metric,
                "value": value,
                "requirement": requirement,
            }
        )

    if len(candidates) != MAX_CANDIDATES_LIMIT:
        fail(
            "candidate_count",
            len(candidates),
            f"must equal {MAX_CANDIDATES_LIMIT}",
        )

    parameter_keys: set[tuple[float, ...]] = set()
    for candidate in candidates:
        name = candidate["candidate"]
        parameters = candidate["parameters"]
        key = parameters_key(parameters)
        if key in parameter_keys:
            fail(
                f"{name}_duplicate_parameters",
                parameters,
                "each candidate must be unique",
            )
        parameter_keys.add(key)

        for parameter_name, bounds in PARAMETER_BOUNDS.items():
            value = parameters[parameter_name]
            if not (
                bounds["minimum"] <= value <= bounds["maximum"]
            ):
                fail(
                    f"{name}_{parameter_name}_bounds",
                    value,
                    (
                        f"must be between {bounds['minimum']} and "
                        f"{bounds['maximum']}"
                    ),
                )

        for record_name in ("stretch_run", "preview_run"):
            record = candidate[record_name]
            if record["exit_status"] != 0:
                fail(
                    f"{name}_{record_name}_exit_status",
                    record["exit_status"],
                    "must equal 0",
                )
            if record["timed_out"]:
                fail(
                    f"{name}_{record_name}_timed_out",
                    True,
                    "must be false",
                )
            if record["fatal_log_markers"]:
                fail(
                    f"{name}_{record_name}_fatal_log_markers",
                    record["fatal_log_markers"],
                    "must be empty",
                )

        source = candidate["source"]
        output = candidate["output"]
        roundtrip = candidate["roundtrip"]
        if source["sha256"] == output["sha256"]:
            fail(
                f"{name}_source_output_sha256",
                source["sha256"],
                "source and output must differ",
            )

        for field in ("channels", "width", "height", "bitpix", "dtype"):
            if source[field] != output[field]:
                fail(
                    f"{name}_output_preserve_{field}",
                    {
                        "source": source[field],
                        "output": output[field],
                    },
                    "source and output must match",
                )
            if source[field] != roundtrip[field]:
                fail(
                    f"{name}_roundtrip_preserve_{field}",
                    {
                        "source": source[field],
                        "roundtrip": roundtrip[field],
                    },
                    "source and roundtrip must match",
                )

        if output["finite_fraction"] != 1.0:
            fail(
                f"{name}_output_finite_fraction",
                output["finite_fraction"],
                "must equal 1.0",
            )
        if roundtrip["finite_fraction"] != 1.0:
            fail(
                f"{name}_roundtrip_finite_fraction",
                roundtrip["finite_fraction"],
                "must equal 1.0",
            )

        missing_previews = [
            path
            for path in candidate["previews"].values()
            if not Path(path).is_file()
        ]
        if missing_previews:
            fail(
                f"{name}_missing_previews",
                missing_previews,
                "all previews must exist",
            )

    satisfactory = not failed
    return {
        "status": "satisfactory" if satisfactory else "failed",
        "satisfactory": satisfactory,
        "failed_checks": failed,
        "interpretation": (
            "Siril executed all three bounded candidates, inverse GHT "
            "roundtrips, and previews correctly."
            if satisfactory
            else "The adaptive execution self-test failed one or more "
            "candidate, bounds, process, format, or preview safeguards."
        ),
    }

def self_test(timeout_seconds: int) -> dict[str, Any]:
    siril = siril_version()
    root = (
        WORKSPACE
        / ".skill-self-tests"
        / "siril-ghs-stretch"
        / unique_id()
    )
    root.mkdir(parents=True, exist_ok=False)
    source = root / "synthetic-starless-denoised.fit"
    write_synthetic_fits(source)

    candidates: list[dict[str, Any]] = []
    baseline = execute_candidate(
        source,
        root,
        timeout_seconds,
        candidate_index=0,
        parameters=baseline_parameters(),
        adaptation_reason="Synthetic baseline execution test.",
    )
    candidates.append(baseline)

    second_parameters, second_reason = plan_second_candidate(baseline)
    second = execute_candidate(
        source,
        root,
        timeout_seconds,
        candidate_index=1,
        parameters=second_parameters,
        adaptation_reason=second_reason,
    )
    candidates.append(second)

    third_parameters, third_reason = plan_third_candidate(
        baseline,
        second,
    )
    third = execute_candidate(
        source,
        root,
        timeout_seconds,
        candidate_index=2,
        parameters=third_parameters,
        adaptation_reason=third_reason,
    )
    candidates.append(third)

    execution = self_test_execution_assessment(candidates)
    if not execution["satisfactory"]:
        raise GhsStretchError(
            f"Adaptive GHS execution self-test failed "
            f"{execution['failed_checks']}; evidence is preserved at {root}"
        )

    recommended = recommended_candidate(candidates)
    return {
        "status": "success",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "self_test_directory": str(root),
        "siril": siril,
        "maximum_total_candidates": MAX_CANDIDATES_LIMIT,
        "parameter_bounds": PARAMETER_BOUNDS,
        "selection_targets": SELECTION_TARGETS,
        "candidates": candidates,
        "recommended_candidate": (
            recommended["candidate"] if recommended is not None else None
        ),
        "execution_assessment": execution,
        "tests": [
            "three real bounded Siril GHT executions",
            "three real inverse-GHT executions",
            "proven M16 baseline candidate",
            "metric-directed second candidate",
            "bounded third refinement",
            "unique parameter sets",
            "parameter range enforcement",
            "even-weighted luminance",
            "RGB Blend clipping mode",
            "32-bit RGB FITS preservation",
            "finite non-identical outputs",
            "roundtrip evidence for every candidate",
            "before and permanent-after previews for every candidate",
            "evidence preservation",
        ],
    }

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate, compare, and publish a bounded adaptive first M16 "
            "GHS stretch for the canonical denoised starless FITS."
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
        "--max-candidates",
        type=int,
        default=MAX_CANDIDATES_LIMIT,
        help=(
            "Total candidates to generate. The hard maximum is three; "
            "candidate-00 is always the proven baseline."
        ),
    )
    run_parser.add_argument(
        "--fresh-run",
        action="store_true",
        help=(
            "Generate a new candidate set. Existing processing/ghs-pass1 "
            "remains untouched until a later publish command succeeds."
        ),
    )

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--project", required=True)
    publish_parser.add_argument("--run-root", required=True, type=Path)
    publish_parser.add_argument("--candidate", required=True)
    publish_parser.add_argument(
        "--visual-notes",
        required=True,
        help=(
            "Concise comparison explaining why this satisfactory candidate "
            "was selected after viewing all satisfactory previews."
        ),
    )
    publish_parser.add_argument(
        "--fresh-run",
        action="store_true",
        help=(
            "Permit preservation-safe replacement of an existing canonical "
            "processing/ghs-pass1 directory."
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
                max_candidates=args.max_candidates,
            )
        elif args.command == "publish":
            payload = publish_project(
                workspace=WORKSPACE,
                project_name=args.project,
                run_root=args.run_root.resolve(),
                candidate_name=args.candidate,
                visual_selection_notes=args.visual_notes,
                fresh_run=args.fresh_run,
            )
        elif args.command == "status":
            payload = status_project(WORKSPACE, args.project)
        else:
            raise GhsStretchError(
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
    return (
        0
        if payload.get("status")
        in (
            "success",
            "ready",
            "awaiting_visual_selection",
        )
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
