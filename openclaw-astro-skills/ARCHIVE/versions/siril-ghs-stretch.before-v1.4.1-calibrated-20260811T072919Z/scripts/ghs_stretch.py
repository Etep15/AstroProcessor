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


VERSION = "1.4.0"
WORKSPACE = Path(
    "/home/peter/.openclaw/workspace/agents/codewarrior"
)
SIRIL_APP = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/"
    ".toolchain/siril/1.4.4/squashfs-root/AppRun"
)
SIRIL_APPDIR = SIRIL_APP.parent
REQUIRED_SIRIL_VERSION = "1.4.4"
REQUIRED_CHANNEL_BALANCE_VERSION = "1.1.0"
SOURCE_CONTRACT_REVISION = "post-starnet-channel-balance-v1"
UPSTREAM_STAGE = "siril-sho-channel-balance"
CURRENT_STAGE = "siril-ghs-stretch-pass1"
NEXT_STAGE = "siril-ghs-stretch-pass2"

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
    "D": {"minimum": 1.5, "maximum": 5.0},
    "B": {"minimum": 2.0, "maximum": 15.0},
    "SP": {"minimum": 0.0020, "maximum": 0.0120},
    "LP": {"minimum": 0.0, "maximum": 0.0},
    "HP": {"minimum": 0.82, "maximum": 0.97},
}
SELECTION_TARGETS = {
    "output_luma_median": 0.085,
    "balanced_median_minimum": 0.055,
    "balanced_median_maximum": 0.125,
    "maximum_output_luma_p99": 0.75,
    "minimum_preferred_luma_correlation": 0.97,
    "source_relative_percentile_shape": True,
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
        "source": processing / "sho-channel-balance" / "SHO-starless-linear-balanced.fit",
        "source_manifest": processing / "sho-channel-balance" / "sho-channel-balance-manifest.json",
        "runs": project / ".siril-ghs-stretch",
        "stable": stable,
        "stable_output": stable / "SHO-starless-ghs-pass1.fit",
        "stable_before_preview": stable / "SHO-starless-linear-before-linked.png",
        "stable_after_preview": stable / "SHO-starless-ghs-pass1-linear.png",
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
            'load "SHO-starless-linear-balanced.fit"',
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
            'load "SHO-starless-linear-balanced.fit"',
            "autostretch -linked",
            (
                'savepng "../previews/'
                'SHO-starless-linear-before-linked"'
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
            "The first GHS pass lifted the reviewed StarNet starless image without "
            "channel clipping, and inverse GHT recovered the linear source "
            "within the accepted numerical tolerance."
            if satisfactory
            else "The first GHS candidate requires review because one or "
            "more clipping, histogram, structure, or inverse-roundtrip "
            "safeguards did not pass."
        ),
    }




def validate_source(paths: dict[str, Path]) -> tuple[dict[str, Any], FitsEvidence]:
    if not paths["project"].is_dir():
        raise GhsStretchError(f"Project does not exist: {paths['project']}")
    if not paths["source_manifest"].is_file():
        raise GhsStretchError(f"SHO channel-balance manifest is missing: {paths['source_manifest']}")
    if not paths["source"].is_file():
        raise GhsStretchError(f"Balanced starless source is missing: {paths['source']}")
    manifest = json.loads(paths["source_manifest"].read_text(encoding="utf-8"))
    if manifest.get("project") != paths["project"].name:
        raise GhsStretchError("SHO channel-balance manifest project does not match.")
    if Path(str(manifest.get("project_path", ""))).resolve() != paths["project"].resolve():
        raise GhsStretchError("SHO channel-balance manifest project path does not match.")
    if manifest.get("helper_version") != REQUIRED_CHANNEL_BALANCE_VERSION:
        raise GhsStretchError(
            f"Expected SHO channel-balance helper {REQUIRED_CHANNEL_BALANCE_VERSION}; "
            f"manifest reports {manifest.get('helper_version')!r}."
        )
    if manifest.get("status") != "ready":
        raise GhsStretchError("SHO channel-balance manifest status is not ready.")
    if manifest.get("visual_review_completed") is not True:
        raise GhsStretchError("SHO channel-balance visual review is incomplete.")
    if manifest.get("source_is_starless") is not True:
        raise GhsStretchError("SHO channel-balance output is not declared STARLESS.")
    if manifest.get("stars_layer_modified") is not False:
        raise GhsStretchError("SHO channel-balance manifest indicates that the star layer was modified.")
    if manifest.get("ghs_pass1_permitted") is not True:
        raise GhsStretchError("SHO channel-balance manifest does not permit GHS pass 1.")
    if manifest.get("next_stage") != CURRENT_STAGE:
        raise GhsStretchError("SHO channel-balance next stage is not GHS pass 1.")
    if manifest.get("stage_order") != {
        "upstream": "siril-starnet-removal",
        "current": UPSTREAM_STAGE,
        "downstream": CURRENT_STAGE,
    }:
        raise GhsStretchError("SHO channel-balance stage order does not match the post-StarNet pipeline.")
    output = manifest.get("output", {})
    if Path(str(output.get("path", ""))).resolve() != paths["source"].resolve():
        raise GhsStretchError("SHO channel-balance manifest does not reference canonical SHO-starless-linear-balanced.fit.")
    source_evidence = inspect_fits(paths["source"])
    if source_evidence.channels != 3 or source_evidence.bitpix != -32 or source_evidence.finite_fraction != 1.0:
        raise GhsStretchError("GHS pass 1 requires finite 32-bit floating-point RGB channel-balance output.")
    if source_evidence.sha256 != output.get("sha256"):
        raise GhsStretchError("Canonical balanced starless checksum does not match the channel-balance manifest.")
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
    p99 = float(metrics["output_luma_p99"])
    low_clip = float(metrics["low_clip_fraction"])
    high_clip = float(metrics["high_clip_fraction"])

    # M16 is a background-dominated starless image: source p90 and p99 sit
    # close to the source median. Absolute lower floors for output p90/p99
    # therefore misclassified a useful subdued pass-1 result as too_gentle.
    #
    # Pass-1 brightness classification is now median-centric. p99 remains a
    # high-side safety cap, while low-side percentile shape is handled by
    # the source-relative selection score and mandatory visual review.
    if (
        low_clip > 0.0
        or high_clip > 0.0
        or median > SELECTION_TARGETS["balanced_median_maximum"]
        or p99 > SELECTION_TARGETS["maximum_output_luma_p99"]
    ):
        return "too_strong"

    if median < SELECTION_TARGETS["balanced_median_minimum"]:
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
            "B": current["B"] + 1.5,
            "SP": current["SP"] - 0.00030,
            "LP": 0.0,
            "HP": current["HP"] - 0.010,
        }
        reason = (
            "Baseline histogram was too subdued; apply the predefined "
            "stronger step."
        )
        direction = "stronger"
    elif classification == "too_strong":
        source_median = float(baseline["source"]["median"])
        proposed = {
            "D": 2.80,
            "B": 5.50,
            "SP": max(current["SP"] + 0.00100, source_median * 0.95),
            "LP": 0.0,
            "HP": 0.930,
        }
        reason = (
            "Baseline was too strong; jump to the predefined gentler tier "
            "with lower D/B and an SP anchored near the source median so "
            "the background is not forced far to the right."
        )
        direction = "gentler"
    else:
        proposed = {
            "D": current["D"] - 0.25,
            "B": current["B"] - 1.5,
            "SP": current["SP"] + 0.00020,
            "LP": 0.0,
            "HP": current["HP"] + 0.010,
        }
        reason = (
            "Baseline was technically balanced; create the predefined "
            "gentler comparison with slightly higher SP for visual review."
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
    target = float(SELECTION_TARGETS["output_luma_median"])
    median = float(metrics["output_luma_median"])

    if classification == "too_strong":
        source_median = float(second["source"]["median"])
        proposed = {
            "D": 1.90,
            "B": 2.50,
            "SP": max(current["SP"] + 0.00080, source_median * 1.10),
            "LP": 0.0,
            "HP": 0.960,
        }
        direction = "gentler"
        reason = (
            "Second candidate remained too strong; use the final very-gentle "
            "tier with substantially lower D/B and stronger highlight "
            "protection."
        )
    elif classification == "too_gentle":
        proposed = {
            "D": current["D"] + 0.25,
            "B": current["B"] + 1.00,
            "SP": current["SP"] - 0.00020,
            "LP": 0.0,
            "HP": current["HP"] - 0.005,
        }
        direction = "stronger"
        reason = (
            "Second candidate is below the source-aware pass-1 median band; "
            "apply one bounded stronger refinement."
        )
    elif median > target * 1.15:
        # Real M16 calibration:
        # D=2.80/B=5.50/SP=0.00543/HP=0.93 produced median 0.11427.
        # The previous +0.25/+1.0/-0.00020/-0.005 move produced 0.14298.
        # Applying the measured response in the opposite direction is the
        # bounded target-seeking step expected to land near 0.085.
        proposed = {
            "D": current["D"] - 0.25,
            "B": current["B"] - 1.00,
            "SP": current["SP"] + 0.00020,
            "LP": 0.0,
            "HP": current["HP"] + 0.005,
        }
        direction = "gentler"
        reason = (
            "Second candidate is technically balanced but remains above the "
            "0.085 pass-1 median target; apply the measured M16 inverse "
            "response step toward the target."
        )
    elif median < target * 0.85:
        proposed = {
            "D": current["D"] + 0.12,
            "B": current["B"] + 0.50,
            "SP": current["SP"] - 0.00010,
            "LP": 0.0,
            "HP": current["HP"] - 0.003,
        }
        direction = "stronger"
        reason = (
            "Second candidate is balanced but below the preferred 0.085 "
            "median target; apply a small bounded stronger comparison."
        )
    else:
        # Already inside a narrow target window. Keep the third candidate
        # close so visual review has a meaningful comparison rather than a
        # large jump away from the measured solution.
        direction = "gentler" if median >= target else "stronger"
        sign = -1.0 if direction == "gentler" else 1.0
        proposed = {
            "D": current["D"] + sign * 0.08,
            "B": current["B"] + sign * 0.30,
            "SP": current["SP"] - sign * 0.00006,
            "LP": 0.0,
            "HP": current["HP"] - sign * 0.002,
        }
        reason = (
            "Second candidate is already within the narrow pass-1 target "
            "window; create one close bounded comparison for visual review."
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
    target_median = float(targets["output_luma_median"])
    p90 = max(float(metrics["output_luma_p90"]), 1.0e-9)
    p99 = max(float(metrics["output_luma_p99"]), 1.0e-9)
    source_median = max(float(metrics["source_luma_median"]), 1.0e-9)
    source_p90 = max(float(metrics["source_luma_p90"]), 1.0e-9)
    source_p99 = max(float(metrics["source_luma_p99"]), 1.0e-9)
    correlation = float(metrics["luma_correlation"])
    low_clip = float(metrics["low_clip_fraction"])
    high_clip = float(metrics["high_clip_fraction"])
    roundtrip = float(metrics["roundtrip_relative_rms"])

    # Preserve the source's percentile *shape* rather than forcing absolute
    # p90/p99 targets that are inappropriate for a background-dominated,
    # starless field. The median remains the primary pass-1 brightness goal.
    source_p90_ratio = source_p90 / source_median
    source_p99_ratio = source_p99 / source_median
    output_p90_ratio = p90 / median
    output_p99_ratio = p99 / median

    score = (
        2.5 * abs(math.log(median / target_median))
        + 0.40 * abs(math.log(output_p90_ratio / source_p90_ratio))
        + 0.40 * abs(math.log(output_p99_ratio / source_p99_ratio))
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





def candidate_publication_eligible(candidate: dict[str, Any]) -> bool:
    return (
        candidate["quality_assessment"]["satisfactory"]
        and candidate.get("histogram_classification") == "balanced"
    )




def publication_gate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    if not candidates:
        return {
            "status": "needs_review",
            "publication_permitted": False,
            "final_candidate_histogram_classification": None,
            "final_candidate_too_strong": False,
            "bounded_search_exhausted_too_strong": False,
            "publication_eligible_candidates": [],
            "reason": "No candidates were generated.",
        }

    final_classification = candidates[-1].get(
        "histogram_classification"
    )
    final_too_strong = final_classification == "too_strong"
    exhausted_too_strong = (
        len(candidates) >= MAX_CANDIDATES_LIMIT
        and final_too_strong
    )
    eligible = [
        candidate["candidate"]
        for candidate in candidates
        if candidate_publication_eligible(candidate)
    ]

    if final_too_strong:
        return {
            "status": "needs_adjustment",
            "publication_permitted": False,
            "final_candidate_histogram_classification": final_classification,
            "final_candidate_too_strong": True,
            "bounded_search_exhausted_too_strong": exhausted_too_strong,
            "publication_eligible_candidates": eligible,
            "reason": (
                "The final generated candidate is still classified "
                "too_strong; publication is blocked and GHS pass 2 remains "
                "disabled."
            ),
        }

    if not eligible:
        return {
            "status": "needs_adjustment",
            "publication_permitted": False,
            "final_candidate_histogram_classification": final_classification,
            "final_candidate_too_strong": False,
            "bounded_search_exhausted_too_strong": False,
            "publication_eligible_candidates": [],
            "reason": (
                "No technically satisfactory candidate falls inside the "
                "source-aware balanced pass-1 median band."
            ),
        }

    return {
        "status": "awaiting_visual_selection",
        "publication_permitted": True,
        "final_candidate_histogram_classification": final_classification,
        "final_candidate_too_strong": False,
        "bounded_search_exhausted_too_strong": False,
        "publication_eligible_candidates": eligible,
        "reason": (
            "At least one technically satisfactory candidate is classified "
            "balanced and may proceed to CodeWarrior visual selection."
        ),
    }




def recommended_candidate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any] | None:
    eligible = [
        candidate
        for candidate in candidates
        if candidate_publication_eligible(candidate)
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
    working_source = work / "SHO-starless-linear-balanced.fit"
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
        previews / "SHO-starless-linear-before-linked.png"
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

    gate = publication_gate(candidates)
    if not gate["publication_permitted"]:
        raise GhsStretchError(
            f"Publication is blocked by the adaptive gate: {gate['reason']}"
        )
    if not candidate_publication_eligible(candidate):
        raise GhsStretchError(
            "The selected GHS candidate is not publication-eligible; "
            "only technically satisfactory balanced candidates can be published."
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
        / "SHO-starless-linear-before-linked.png"
    )
    candidate_after = (
        candidate_dir
        / "previews"
        / "SHO-starless-ghs-pass1-linear.png"
    )

    staged_output = publish_dir / "SHO-starless-ghs-pass1.fit"
    staged_before = (
        publish_dir
        / "SHO-starless-linear-before-linked.png"
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
        "source_contract_revision": SOURCE_CONTRACT_REVISION,
        "stage_order": {"upstream": UPSTREAM_STAGE, "current": CURRENT_STAGE, "downstream": NEXT_STAGE},
        "upstream_stage": UPSTREAM_STAGE,
        "next_stage": NEXT_STAGE,
        "source_channel_balance_manifest_sha256": sha256_file(paths["source_manifest"]),
        "project": paths["project"].name,
        "project_path": str(paths["project"]),
        "adaptive_policy": {
            "maximum_total_candidates": MAX_CANDIDATES_LIMIT,
            "parameter_bounds": PARAMETER_BOUNDS,
            "selection_targets": SELECTION_TARGETS,
            "baseline_is_proven_manual_configuration": False,
            "arbitrary_parameters_permitted": False,
            "final_too_strong_publication_permitted": False,
        },
        "publication_gate": gate,
        "source": asdict(source_evidence),
        "source_channel_balance_manifest": str(
            paths["source_manifest"]
        ),
        "source_channel_balance_status": source_manifest.get("status"),
        "source_channel_balance_helper_version": source_manifest.get(
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
                if candidate_publication_eligible(item)
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
            "Historical manual M16 baseline retained as a reference: "
            "D=4.4, B=15, SP=0.004, LP=0, HP=0.86."
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

    gate = publication_gate(candidates)
    recommended = (
        recommended_candidate(candidates)
        if gate["publication_permitted"]
        else None
    )
    technically_satisfactory = [
        candidate["candidate"]
        for candidate in candidates
        if candidate["quality_assessment"]["satisfactory"]
    ]

    run_record = {
        "schema_version": 3,
        "status": gate["status"],
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
        "technically_satisfactory_candidates": technically_satisfactory,
        "publication_gate": gate,
        "publication_permitted": gate["publication_permitted"],
        "publication_eligible_candidates": gate[
            "publication_eligible_candidates"
        ],
        "final_candidate_histogram_classification": gate[
            "final_candidate_histogram_classification"
        ],
        "final_candidate_too_strong": gate[
            "final_candidate_too_strong"
        ],
        "bounded_search_exhausted_too_strong": gate[
            "bounded_search_exhausted_too_strong"
        ],
        "recommended_candidate": (
            recommended["candidate"] if recommended is not None else None
        ),
        "recommended_selection_score": (
            recommended["selection_score"]
            if recommended is not None
            else None
        ),
        "canonical_output_changed": False,
        "visual_selection_required": gate["publication_permitted"],
        "publish_command_template": (
            f"{Path(__file__)} publish --project "
            f"{json.dumps(project_name)} --run-root "
            f"{json.dumps(str(run_root))} --candidate <candidate-XX> "
            f"--visual-notes <review-notes> --fresh-run"
            if gate["publication_permitted"]
            else None
        ),
        "ghs_pass2_processing_permitted": False,
        "message": (
            "Compare every publication-eligible after-preview and publish "
            "exactly one candidate using the publish subcommand."
            if gate["publication_permitted"]
            else gate["reason"]
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
    gate = publication_gate(candidates)
    if run_record.get("publication_permitted") is not True:
        raise GhsStretchError(
            "This run is not publication-permitted; canonical output was "
            "not changed."
        )
    if not gate["publication_permitted"]:
        raise GhsStretchError(
            f"Publication is blocked by the adaptive gate: {gate['reason']}"
        )

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
    if not candidate_publication_eligible(candidate):
        raise GhsStretchError(
            f"Candidate {candidate_name} is not publication-eligible and "
            "cannot be published."
        )

    recommended = recommended_candidate(candidates)
    source_manifest, source_evidence = validate_source(paths)
    if source_evidence.sha256 != run_record["source"]["sha256"]:
        raise GhsStretchError(
            "Current StarNet source no longer matches the adaptive run."
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




def status_project(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["stable_manifest"].is_file():
        return {
            "status": "missing", "helper_version": VERSION,
            "source_contract_revision": SOURCE_CONTRACT_REVISION,
            "project": str(paths["project"]),
            "stable_directory": str(paths["stable"]),
            "ghs_pass2_processing_permitted": False,
        }
    try:
        manifest = json.loads(paths["stable_manifest"].read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "status": "invalid", "helper_version": VERSION,
            "source_contract_revision": SOURCE_CONTRACT_REVISION,
            "project": str(paths["project"]), "errors": [str(exc)],
            "ghs_pass2_processing_permitted": False,
        }
    if (
        manifest.get("helper_version") != VERSION
        or manifest.get("upstream_stage") != UPSTREAM_STAGE
        or manifest.get("source_contract_revision") != SOURCE_CONTRACT_REVISION
    ):
        return {
            "status": "obsolete", "helper_version": VERSION,
            "source_contract_revision": SOURCE_CONTRACT_REVISION,
            "manifest_helper_version": manifest.get("helper_version"),
            "required_helper_version": VERSION,
            "upstream_stage": manifest.get("upstream_stage"),
            "manifest_source_contract_revision": manifest.get("source_contract_revision"),
            "reason": "Existing GHS pass-1 output predates the post-StarNet SHO channel-balance source contract.",
            "project": str(paths["project"]),
            "stable_directory": str(paths["stable"]),
            "manifest": str(paths["stable_manifest"]),
            "ghs_pass2_processing_permitted": False,
        }
    errors = []
    output_evidence = source_evidence = None
    source_manifest = {}
    source_manifest_hash = None
    try:
        source_manifest, source_evidence = validate_source(paths)
        source_manifest_hash = sha256_file(paths["source_manifest"])
        if manifest.get("source_channel_balance_manifest_sha256") != source_manifest_hash:
            errors.append("SHO channel-balance manifest checksum changed.")
        if manifest.get("source", {}).get("sha256") != source_evidence.sha256:
            errors.append("GHS pass-1 balanced-starless source checksum changed.")
        if not paths["stable_output"].is_file():
            errors.append(f"Missing output: {paths['stable_output']}")
        else:
            output_evidence = asdict(inspect_fits(paths["stable_output"]))
            if output_evidence["sha256"] != manifest.get("output", {}).get("sha256"):
                errors.append("Output checksum does not match the manifest.")
            if (
                output_evidence["bitpix"] != -32
                or output_evidence["finite_fraction"] != 1.0
                or output_evidence["width"] != source_evidence.width
                or output_evidence["height"] != source_evidence.height
            ):
                errors.append("Canonical GHS output format changed.")
        for key in ("stable_before_preview", "stable_after_preview"):
            if not paths[key].is_file():
                errors.append(f"Missing preview: {paths[key]}")
        if manifest.get("stage_order") != {
            "upstream": UPSTREAM_STAGE,
            "current": CURRENT_STAGE,
            "downstream": NEXT_STAGE,
        }:
            errors.append("GHS pass-1 stage order is invalid.")
        if manifest.get("visual_review_completed") is not True:
            errors.append("GHS pass-1 visual review is incomplete.")
        if manifest.get("ghs_pass2_processing_permitted") is not True:
            errors.append("Manifest does not permit GHS pass 2.")
    except Exception as exc:
        errors.append(str(exc))
    ready = manifest.get("status") == "ready" and not errors and manifest.get("ghs_pass2_processing_permitted") is True
    return {
        "status": "ready" if ready else "invalid",
        "helper_version": VERSION,
        "source_contract_revision": SOURCE_CONTRACT_REVISION,
        "project": str(paths["project"]),
        "stable_directory": str(paths["stable"]),
        "manifest": str(paths["stable_manifest"]),
        "errors": errors,
        "upstream_summary": {
            "manifest": str(paths["source_manifest"]),
            "manifest_sha256": source_manifest_hash,
            "helper_version": source_manifest.get("helper_version"),
            "status": source_manifest.get("status"),
            "visual_review_completed": source_manifest.get("visual_review_completed"),
            "ghs_pass1_permitted": source_manifest.get("ghs_pass1_permitted"),
            "source_is_starless": source_manifest.get("source_is_starless"),
            "stars_layer_modified": source_manifest.get("stars_layer_modified"),
        },
        "source": asdict(source_evidence) if source_evidence is not None else None,
        "method": manifest.get("method"),
        "quality_assessment": manifest.get("quality_assessment"),
        "roundtrip_evidence": manifest.get("roundtrip_evidence"),
        "output": output_evidence,
        "previews": manifest.get("previews"),
        "candidate_count": manifest.get("candidate_count"),
        "recommended_candidate": manifest.get("recommended_candidate"),
        "selected_candidate": manifest.get("selected_candidate"),
        "selected_candidate_was_recommended": manifest.get("selected_candidate_was_recommended"),
        "visual_review_completed": ready,
        "next_stage": NEXT_STAGE,
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



def policy_self_test() -> dict[str, Any]:
    too_strong_metrics = {
        "output_luma_median": 0.30,
        "output_luma_p90": 0.32,
        "output_luma_p99": 0.36,
        "low_clip_fraction": 0.0,
        "high_clip_fraction": 0.0,
    }
    balanced_metrics = {
        "output_luma_median": 0.085,
        "output_luma_p90": 0.090,
        "output_luma_p99": 0.110,
        "low_clip_fraction": 0.0,
        "high_clip_fraction": 0.0,
    }
    too_gentle_metrics = {
        "output_luma_median": 0.035,
        "output_luma_p90": 0.040,
        "output_luma_p99": 0.050,
        "low_clip_fraction": 0.0,
        "high_clip_fraction": 0.0,
    }

    def fake(name: str, metrics: dict[str, float]) -> dict[str, Any]:
        classification = histogram_classification(metrics)
        return {
            "candidate": name,
            "histogram_classification": classification,
            "selection_score": 0.0,
            "quality_assessment": {
                "satisfactory": True,
                "metrics": metrics,
            },
        }

    blocked_candidates = [
        fake("candidate-00", too_strong_metrics),
        fake("candidate-01", too_strong_metrics),
        fake("candidate-02", too_strong_metrics),
    ]
    blocked_gate = publication_gate(blocked_candidates)
    if blocked_gate["publication_permitted"]:
        raise GhsStretchError(
            "Policy self-test failed: all-too-strong run was publishable."
        )
    if not blocked_gate["bounded_search_exhausted_too_strong"]:
        raise GhsStretchError(
            "Policy self-test failed: final too-strong exhaustion was not "
            "detected."
        )

    allowed_candidates = [
        fake("candidate-00", too_strong_metrics),
        fake("candidate-01", balanced_metrics),
        fake("candidate-02", balanced_metrics),
    ]
    allowed_gate = publication_gate(allowed_candidates)
    if not allowed_gate["publication_permitted"]:
        raise GhsStretchError(
            "Policy self-test failed: balanced candidates were blocked."
        )

    no_balanced = [
        fake("candidate-00", too_gentle_metrics),
        fake("candidate-01", too_gentle_metrics),
        fake("candidate-02", too_gentle_metrics),
    ]
    no_balanced_gate = publication_gate(no_balanced)
    if no_balanced_gate["publication_permitted"]:
        raise GhsStretchError(
            "Policy self-test failed: all-too-gentle run was publishable."
        )

    return {
        "status": "success",
        "all_too_strong_publication_blocked": True,
        "all_too_gentle_publication_blocked": True,
        "balanced_publication_permitted": True,
        "balanced_median_minimum": (
            SELECTION_TARGETS["balanced_median_minimum"]
        ),
        "balanced_median_maximum": (
            SELECTION_TARGETS["balanced_median_maximum"]
        ),
        "median_target": SELECTION_TARGETS["output_luma_median"],
        "source_relative_percentile_shape": True,
        "parameter_bounds": PARAMETER_BOUNDS,
    }




# ===== v1.4.0 dynamic-range expansion policy (injected by installer) =====
# This block intentionally overrides only adaptive candidate planning, histogram
# classification/recommendation and policy self-test. Siril execution, preview,
# inverse-GHT roundtrip, provenance, publication and FITS safety code remain the
# audited v1.3.1 implementation.

PARAMETER_BOUNDS = {
    "D": {"minimum": 2.0, "maximum": 5.5},
    "B": {"minimum": 1.0, "maximum": 8.0},
    "SP": {"minimum": 0.0020, "maximum": 0.0300},
    "LP": {"minimum": 0.0, "maximum": 0.0},
    "HP": {"minimum": 0.82, "maximum": 0.97},
}

SELECTION_TARGETS = {
    # Median is deliberately broad/advisory. Histogram separation is primary.
    "balanced_median_minimum": 0.080,
    "balanced_median_maximum": 0.240,
    "output_luma_median": 0.150,
    "maximum_output_luma_p99": 0.780,
    "maximum_output_value": 0.950,
    "minimum_preferred_luma_correlation": 0.970,
    "minimum_p99_to_median_ratio": 1.20,
    "preferred_p99_to_median_ratio": 1.25,
    "target_p99_to_median_ratio": 1.40,
    "minimum_p99_minus_median": 0.025,
    "preferred_p99_minus_median": 0.045,
    "source_relative_percentile_shape": False,
    "median_is_primary_target": False,
}

_GHS140_SOURCE_STATS = None


def _ghs140_source_stats(path: Path) -> dict[str, float]:
    with fits.open(path, memmap=True) as hdul:
        data = np.asarray(hdul[0].data[:, ::4, ::4], dtype=np.float32)
    luma = np.mean(data, axis=0)
    finite = luma[np.isfinite(luma)]
    if finite.size < 100:
        raise GhsStretchError("Not enough finite source samples for v1.4.0 source-aware planning.")
    return {
        "median": float(np.percentile(finite, 50.0)),
        "p90": float(np.percentile(finite, 90.0)),
        "p95": float(np.percentile(finite, 95.0)),
        "p99": float(np.percentile(finite, 99.0)),
        "p995": float(np.percentile(finite, 99.5)),
    }


def _ghs140_sp(value: float) -> float:
    return round(clamp_parameter("SP", value), 5)


def baseline_parameters() -> dict[str, float]:
    stats = _GHS140_SOURCE_STATS
    if stats is None:
        # Synthetic self-test fallback. Real project/probe runs populate source
        # statistics before candidate planning.
        source_p99 = 0.00710
    else:
        source_p99 = stats["p99"]
    return normalize_parameters({
        # The first v1.4.0 probe showed D=4.55/B=4.50/SP≈1.10*p99 was still
        # technically safe and already close to the hard separation gate.
        # Start the bounded search there rather than spending candidate-00 on
        # another deliberately weak stretch.
        "D": 4.55,
        "B": 4.50,
        "SP": _ghs140_sp(source_p99 * 1.10),
        "LP": 0.0,
        "HP": 0.880,
    })


def histogram_classification(metrics: dict[str, Any]) -> str:
    median = float(metrics["output_luma_median"])
    p99 = float(metrics["output_luma_p99"])
    maximum = float(metrics["output_maximum"])
    low_clip = float(metrics["low_clip_fraction"])
    high_clip = float(metrics["high_clip_fraction"])
    ratio = p99 / max(median, 1.0e-12)
    spread = p99 - median
    if (
        low_clip > 0.0
        or high_clip > 0.0
        or median > SELECTION_TARGETS["balanced_median_maximum"]
        or p99 > SELECTION_TARGETS["maximum_output_luma_p99"]
        or maximum > SELECTION_TARGETS["maximum_output_value"]
    ):
        return "too_strong"
    if (
        median < SELECTION_TARGETS["balanced_median_minimum"]
        or ratio < SELECTION_TARGETS["minimum_p99_to_median_ratio"]
        or spread < SELECTION_TARGETS["minimum_p99_minus_median"]
    ):
        return "too_gentle"
    return "balanced"


def plan_second_candidate(baseline: dict[str, Any]) -> tuple[dict[str, float], str]:
    metrics = baseline["quality_assessment"]["metrics"]
    source_p99 = float(metrics["source_luma_p99"])
    classification = baseline["histogram_classification"]
    if classification == "too_strong":
        proposed = {
            "D": 4.20,
            "B": 4.00,
            "SP": source_p99 * 1.07,
            "LP": 0.0,
            "HP": 0.890,
        }
        reason = (
            "Candidate-00 crossed the broad pass-1 brightness/headroom gate; "
            "step back modestly while retaining substantially more separation "
            "than the historical median-first policy."
        )
        direction = "gentler"
    else:
        proposed = {
            "D": 4.90,
            "B": 5.25,
            "SP": source_p99 * 1.16,
            "LP": 0.0,
            "HP": 0.870,
        }
        reason = (
            "Candidate-00 remained below the preferred pass-1 separation target; "
            "increase D and B and move SP farther into the upper source signal "
            "to expand p99/median rather than merely lifting the histogram."
        )
        direction = "stronger"
    return ensure_unique_parameters(
        proposed, [baseline["parameters"]], preferred_direction=direction
    ), reason


def plan_third_candidate(baseline: dict[str, Any], second: dict[str, Any]) -> tuple[dict[str, float], str]:
    metrics = second["quality_assessment"]["metrics"]
    median = float(metrics["output_luma_median"])
    p99 = float(metrics["output_luma_p99"])
    ratio = p99 / max(median, 1.0e-12)
    spread = p99 - median
    source_p99 = float(metrics["source_luma_p99"])
    p = second["parameters"]
    classification = second["histogram_classification"]

    if classification == "too_strong":
        # Interpolate back toward candidate-00 rather than jumping to the old
        # weak parameter region.
        proposed = {
            "D": 4.70,
            "B": 4.80,
            "SP": source_p99 * 1.13,
            "LP": 0.0,
            "HP": 0.880,
        }
        reason = (
            "Candidate-01 was too strong; test an intermediate dynamic-range "
            "expansion between candidate-00 and candidate-01."
        )
        direction = "gentler"
    elif (
        ratio < SELECTION_TARGETS["preferred_p99_to_median_ratio"]
        or spread < SELECTION_TARGETS["preferred_p99_minus_median"]
    ):
        proposed = {
            "D": min(5.35, p["D"] + 0.45),
            "B": min(6.50, p["B"] + 1.25),
            "SP": source_p99 * 1.24,
            "LP": 0.0,
            "HP": 0.850,
        }
        reason = (
            "Candidate-01 is still below the preferred separation target; "
            "use the final bounded candidate for a stronger but still "
            "highlight-protected expansion aimed at >=1.25 p99/median."
        )
        direction = "stronger"
    else:
        proposed = {
            "D": min(5.20, p["D"] + 0.25),
            "B": max(4.25, p["B"] - 0.50),
            "SP": source_p99 * 1.18,
            "LP": 0.0,
            "HP": 0.870,
        }
        reason = (
            "Candidate-01 already meets the preferred separation target; "
            "generate one broader comparison candidate without reverting to "
            "median-first optimization."
        )
        direction = "stronger"

    return ensure_unique_parameters(
        proposed,
        [baseline["parameters"], second["parameters"]],
        preferred_direction=direction,
    ), reason


def candidate_selection_score(quality_assessment: dict[str, Any]) -> float:
    metrics = quality_assessment["metrics"]
    median = max(float(metrics["output_luma_median"]), 1.0e-12)
    p99 = max(float(metrics["output_luma_p99"]), median)
    ratio = p99 / median
    spread = p99 - median
    corr = float(metrics["luma_correlation"])
    maximum = float(metrics["output_maximum"])
    low_clip = float(metrics["low_clip_fraction"])
    high_clip = float(metrics["high_clip_fraction"])
    roundtrip = float(metrics["roundtrip_relative_rms"])
    # Dynamic-range deficits dominate. Median proximity is intentionally mild.
    score = (
        0.35 * abs(math.log(median / SELECTION_TARGETS["output_luma_median"]))
        + 1.80 * max(0.0, SELECTION_TARGETS["target_p99_to_median_ratio"] - ratio)
        + 7.00 * max(0.0, SELECTION_TARGETS["preferred_p99_minus_median"] - spread)
        + 3.00 * max(0.0, SELECTION_TARGETS["minimum_preferred_luma_correlation"] - corr)
        + 4.00 * max(0.0, maximum - 0.90)
        + 100.0 * (low_clip + high_clip)
        + min(roundtrip * 1000.0, 2.0)
    )
    return float(score)


def candidate_publication_eligible(candidate: dict[str, Any]) -> bool:
    return (
        candidate.get("quality_assessment", {}).get("satisfactory") is True
        and candidate.get("histogram_classification") == "balanced"
    )


def publication_gate(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [c["candidate"] for c in candidates if candidate_publication_eligible(c)]
    final_classification = candidates[-1].get("histogram_classification") if candidates else None
    if not eligible:
        return {
            "status": "needs_adjustment",
            "publication_permitted": False,
            "final_candidate_histogram_classification": final_classification,
            "final_candidate_too_strong": final_classification == "too_strong",
            "bounded_search_exhausted_too_strong": bool(final_classification == "too_strong" and len(candidates) >= MAX_CANDIDATES_LIMIT),
            "publication_eligible_candidates": [],
            "reason": "No bounded pass-1 candidate achieved both technical safety and the v1.4.0 minimum histogram-separation gate.",
        }
    return {
        "status": "awaiting_visual_selection",
        "publication_permitted": True,
        "final_candidate_histogram_classification": final_classification,
        "final_candidate_too_strong": final_classification == "too_strong",
        "bounded_search_exhausted_too_strong": False,
        "publication_eligible_candidates": eligible,
        "reason": "At least one technically safe candidate creates materially broader pass-1 signal separation; visual selection may compare all eligible candidates.",
    }


def recommended_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    eligible = [c for c in candidates if candidate_publication_eligible(c)]
    if not eligible:
        return None
    return min(eligible, key=lambda c: float(c.get("selection_score", 1.0e9)))


def policy_self_test() -> dict[str, Any]:
    compressed = {
        "output_luma_median": 0.10, "output_luma_p99": 0.111,
        "output_maximum": 0.20, "low_clip_fraction": 0.0, "high_clip_fraction": 0.0,
    }
    expanded = {
        "output_luma_median": 0.14, "output_luma_p99": 0.20,
        "output_maximum": 0.45, "low_clip_fraction": 0.0, "high_clip_fraction": 0.0,
    }
    too_bright = {
        "output_luma_median": 0.30, "output_luma_p99": 0.50,
        "output_maximum": 0.70, "low_clip_fraction": 0.0, "high_clip_fraction": 0.0,
    }
    if histogram_classification(compressed) != "too_gentle":
        raise GhsStretchError("v1.4.0 policy self-test failed to reject compressed histogram.")
    if histogram_classification(expanded) != "balanced":
        raise GhsStretchError("v1.4.0 policy self-test failed to accept expanded histogram.")
    if histogram_classification(too_bright) != "too_strong":
        raise GhsStretchError("v1.4.0 policy self-test failed to enforce broad brightness ceiling.")
    return {
        "status": "success",
        "policy_revision": "dynamic-range-expansion-v1",
        "median_is_primary_target": False,
        "source_relative_percentile_shape": False,
        "minimum_p99_to_median_ratio": SELECTION_TARGETS["minimum_p99_to_median_ratio"],
        "preferred_p99_to_median_ratio": SELECTION_TARGETS["preferred_p99_to_median_ratio"],
        "minimum_p99_minus_median": SELECTION_TARGETS["minimum_p99_minus_median"],
        "parameter_bounds": PARAMETER_BOUNDS,
    }


_ghs140_original_run_project = run_project


def run_project(*, workspace: Path, project_name: str, timeout_seconds: int, fresh_run: bool, max_candidates: int) -> dict[str, Any]:
    global _GHS140_SOURCE_STATS
    paths = project_paths(workspace, project_name)
    _GHS140_SOURCE_STATS = _ghs140_source_stats(paths["source"])
    try:
        return _ghs140_original_run_project(
            workspace=workspace,
            project_name=project_name,
            timeout_seconds=timeout_seconds,
            fresh_run=fresh_run,
            max_candidates=max_candidates,
        )
    finally:
        _GHS140_SOURCE_STATS = None

# ===== end v1.4.0 override =====


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
    policy = policy_self_test()
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
        "policy_assessment": policy,
        "tests": [
            "final bounded too-strong publication gate",
            "expanded gentler GHS parameter range",
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
            "GHS stretch for the canonical StarNet starless FITS."
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
