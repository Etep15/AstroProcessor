#!/usr/bin/env python3
"""Bounded adaptive second-pass GHS stretch for the canonical pass-1 starless image."""

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


VERSION = "1.2.0"
PUBLISH_COMPATIBLE_RUN_HELPER_VERSIONS = frozenset(
    {"1.0.3", "1.0.4", "1.1.0", "1.1.1", "1.1.2", "1.2.0"}
)
CANONICAL_COMPATIBLE_MANIFEST_HELPER_VERSIONS = frozenset(
    {"1.0.4", "1.1.0", "1.1.1", "1.1.2", "1.2.0"}
)
CLI_SUCCESS_STATUSES = frozenset(
    {
        "success",
        "ready",
        "start_new_run",
        "awaiting_visual_selection",
        "ready_to_publish",
        "confirmation_required",
        "fresh_run_authorized",
    }
)
WORKSPACE = Path(
    "/home/peter/.openclaw/workspace/agents/codewarrior"
)
SIRIL_APP = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/"
    ".toolchain/siril/1.4.4/squashfs-root/AppRun"
)
SIRIL_APPDIR = SIRIL_APP.parent
REQUIRED_SIRIL_VERSION = "1.4.4"
REQUIRED_PASS1_VERSION = "1.3.1"
UPSTREAM_STAGE = "siril-ghs-stretch-pass1"
CURRENT_STAGE = "siril-ghs-stretch-pass2"
NEXT_STAGE = "siril-black-point"

# Exact first-pass M16 settings from the successful manual Siril 1.4.4 run.
GHS_D = 1.40
GHS_B = 3.00
GHS_SP = 0.090
GHS_LP = 0.0
GHS_HP = 0.950
GHS_CLIPMODE = "rgbblend"
GHS_COLOUR_MODEL = "even"

MAX_CANDIDATES_LIMIT = 3
PARAMETER_BOUNDS = {
    "D": {"minimum": 0.70, "maximum": 3.50},
    "B": {"minimum": 0.50, "maximum": 7.00},
    "SP": {"minimum": 0.040, "maximum": 0.180},
    "LP": {"minimum": 0.0, "maximum": 0.0},
    "HP": {"minimum": 0.86, "maximum": 0.99},
}
SELECTION_TARGETS = {
    "output_luma_median": 0.180,
    "balanced_median_minimum": 0.135,
    "balanced_median_maximum": 0.225,
    "maximum_output_luma_p99": 0.80,
    "maximum_output_value": 0.97,
    "minimum_preferred_luma_correlation": 0.97,
    "target_p90_to_median_ratio": 1.12,
    "target_p99_to_median_ratio": 1.55,
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
    stable = processing / "ghs-pass2"
    return {
        "project": project,
        "processing": processing,
        "source": processing / "ghs-pass1" / "SHO-starless-ghs-pass1.fit",
        "source_manifest": processing / "ghs-pass1" / "ghs-pass1-manifest.json",
        "runs": project / ".siril-ghs-stretch-pass2",
        "intents": (
            project
            / ".siril-ghs-stretch-pass2"
            / "stage-intents"
        ),
        "stable": stable,
        "stable_output": stable / "SHO-starless-ghs-pass2.fit",
        "stable_before_preview": stable / "SHO-starless-ghs-pass1-before-linear.png",
        "stable_after_preview": stable / "SHO-starless-ghs-pass2-linear.png",
        "stable_manifest": stable / "ghs-pass2-manifest.json",
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
            'load "SHO-starless-ghs-pass1.fit"',
            ght_arguments("ght", parameters),
            'save "SHO-starless-ghs-pass2.fit"',
            ght_arguments("invght", parameters),
            'save "SHO-starless-ghs-pass2-roundtrip.fit"',
            "close",
            "",
        )
    )



def preview_script_text() -> str:
    return "\n".join(
        (
            f"requires {REQUIRED_SIRIL_VERSION}",
            'load "SHO-starless-ghs-pass1.fit"',
            'savepng "../previews/SHO-starless-ghs-pass1-before-linear"',
            "close",
            'load "SHO-starless-ghs-pass2.fit"',
            'savepng "../previews/SHO-starless-ghs-pass2-linear"',
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
            "The second GHS pass lifted the reviewed pass-1 starless image without "
            "channel clipping, and inverse GHT recovered the pass-1 source "
            "within the accepted numerical tolerance."
            if satisfactory
            else "The second GHS pass candidate requires review because one or "
            "more clipping, histogram, structure, or inverse-roundtrip "
            "safeguards did not pass."
        ),
    }




def validate_source(paths: dict[str, Path]) -> tuple[dict[str, Any], FitsEvidence]:
    if not paths["project"].is_dir():
        raise GhsStretchError(f"Project does not exist: {paths['project']}")
    if not paths["source_manifest"].is_file():
        raise GhsStretchError(
            f"GHS pass-2 manifest is missing: {paths['source_manifest']}"
        )

    manifest = json.loads(
        paths["source_manifest"].read_text(encoding="utf-8")
    )

    if manifest.get("project") != paths["project"].name:
        raise GhsStretchError("GHS pass-2 manifest project does not match.")
    if Path(str(manifest.get("project_path", ""))).resolve() != paths["project"].resolve():
        raise GhsStretchError("GHS pass-2 manifest project path does not match.")
    if manifest.get("status") != "ready":
        raise GhsStretchError("GHS pass-2 manifest status is not ready.")
    if manifest.get("helper_version") != REQUIRED_PASS1_VERSION:
        raise GhsStretchError(
            f"Expected GHS pass-2 helper {REQUIRED_PASS1_VERSION}; "
            f"manifest reports {manifest.get('helper_version')!r}."
        )
    if manifest.get("visual_review_completed") is not True:
        raise GhsStretchError("GHS pass-2 visual review is incomplete.")
    if manifest.get("ghs_pass2_processing_permitted") is not True:
        raise GhsStretchError(
            "GHS pass-2 manifest does not permit GHS pass 2."
        )
    if manifest.get("stage_order") != {
        "upstream": "siril-ghs-stretch-pass1",
        "current": "siril-ghs-stretch-pass1",
        "downstream": "siril-ghs-stretch-pass2",
    }:
        # Pass-1 1.3.1 uses upstream StarNet, current pass1, downstream pass2.
        expected = {
            "upstream": "siril-sho-channel-balance",
            "current": "siril-ghs-stretch-pass1",
            "downstream": "siril-ghs-stretch-pass2",
        }
        if manifest.get("stage_order") != expected:
            raise GhsStretchError(
                "GHS pass-2 stage order does not match the current pipeline."
            )

    output = manifest.get("output", {})
    if Path(str(output.get("path", ""))).resolve() != paths["source"].resolve():
        raise GhsStretchError(
            "GHS pass-2 manifest does not reference canonical "
            "SHO-starless-ghs-pass2.fit."
        )

    evidence = inspect_fits(paths["source"])
    if evidence.bitpix != -32 or evidence.finite_fraction != 1.0:
        raise GhsStretchError(
            "GHS pass 2 requires finite BITPIX -32 pass-1 output."
        )
    if evidence.sha256 != output.get("sha256"):
        raise GhsStretchError(
            "Canonical GHS pass-2 checksum does not match its manifest."
        )

    return manifest, evidence





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
    # Center the second pass on the actual current source brightness instead of
    # carrying forward pass-1's near-zero SP.
    return normalize_parameters(
        {
            "D": 1.40,
            "B": 3.00,
            "SP": GHS_SP,
            "LP": 0.0,
            "HP": 0.950,
        }
    )





def histogram_classification(metrics: dict[str, Any]) -> str:
    median = float(metrics["output_luma_median"])
    p99 = float(metrics["output_luma_p99"])
    maximum = float(metrics["output_maximum"])
    low_clip = float(metrics["low_clip_fraction"])
    high_clip = float(metrics["high_clip_fraction"])

    if (
        low_clip > 0.0
        or high_clip > 0.0
        or median > SELECTION_TARGETS["balanced_median_maximum"]
        or p99 > SELECTION_TARGETS["maximum_output_luma_p99"]
        or maximum > SELECTION_TARGETS["maximum_output_value"]
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
    source_median = float(baseline["source"]["median"])
    classification = baseline["histogram_classification"]

    if classification == "too_strong":
        proposed = {
            "D": 1.05,
            "B": 2.00,
            "SP": min(0.180, source_median * 1.08),
            "LP": 0.0,
            "HP": 0.970,
        }
        reason = (
            "Pass-2 baseline was too strong; move to the predefined gentler "
            "second-pass tier."
        )
    else:
        proposed = {
            "D": 2.00,
            "B": 4.20,
            "SP": max(0.040, source_median * 0.95),
            "LP": 0.0,
            "HP": 0.925,
        }
        reason = (
            "Generate the predefined stronger pass-2 comparison around the "
            "current pass-1 histogram peak."
        )

    return ensure_unique_parameters(
        proposed,
        [baseline["parameters"]],
        preferred_direction=(
            "gentler" if classification == "too_strong" else "stronger"
        ),
    ), reason






def plan_third_candidate(
    baseline: dict[str, Any],
    second: dict[str, Any],
) -> tuple[dict[str, float], str]:
    target = float(SELECTION_TARGETS["output_luma_median"])
    m0 = float(baseline["quality_assessment"]["metrics"]["output_luma_median"])
    m1 = float(second["quality_assessment"]["metrics"]["output_luma_median"])
    p0 = baseline["parameters"]
    p1 = second["parameters"]

    if abs(m1 - m0) < 1.0e-6:
        fraction = 0.5
    else:
        fraction = (target - m0) / (m1 - m0)

    # Allow bounded extrapolation when the first two comparisons do not quite
    # bracket the target, but do not make an uncontrolled jump.
    fraction = max(-0.75, min(1.75, fraction))

    proposed = {}
    for key in ("D", "B", "SP", "HP"):
        proposed[key] = p0[key] + fraction * (p1[key] - p0[key])
    proposed["LP"] = 0.0

    direction = (
        "stronger"
        if target > min(m0, m1)
        else "gentler"
    )
    parameters = ensure_unique_parameters(
        proposed,
        [p0, p1],
        preferred_direction=direction,
    )
    return parameters, (
        "Use measured pass-2 response from candidates 00/01 to interpolate "
        "or bounded-extrapolate one final candidate toward median 0.180."
    )







def candidate_selection_score(
    quality_assessment: dict[str, Any],
) -> float:
    metrics = quality_assessment["metrics"]
    median = max(float(metrics["output_luma_median"]), 1.0e-9)
    p90 = max(float(metrics["output_luma_p90"]), 1.0e-9)
    p99 = max(float(metrics["output_luma_p99"]), 1.0e-9)
    correlation = float(metrics["luma_correlation"])
    low_clip = float(metrics["low_clip_fraction"])
    high_clip = float(metrics["high_clip_fraction"])
    maximum = float(metrics["output_maximum"])
    roundtrip = float(metrics["roundtrip_relative_rms"])

    score = (
        3.0 * abs(math.log(median / SELECTION_TARGETS["output_luma_median"]))
        + 0.45 * abs(
            math.log(
                (p90 / median)
                / SELECTION_TARGETS["target_p90_to_median_ratio"]
            )
        )
        + 0.55 * abs(
            math.log(
                (p99 / median)
                / SELECTION_TARGETS["target_p99_to_median_ratio"]
            )
        )
        + 15.0 * max(
            0.0,
            SELECTION_TARGETS["minimum_preferred_luma_correlation"]
            - correlation,
        )
        + 30.0 * roundtrip
        + 1.0e6 * (low_clip + high_clip)
        + 50.0 * max(0.0, maximum - SELECTION_TARGETS["maximum_output_value"])
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
    working_source = work / "SHO-starless-ghs-pass1.fit"
    shutil.copy2(source_path, working_source)

    stretch_script = candidate / "ghs-pass2.ssf"
    stretch_script.write_text(stretch_script_text(parameters), encoding="utf-8")
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
            f"Siril GHS pass 2 failed for {candidate_name}; evidence is "
            f"preserved at {candidate}"
        )

    output = work / "SHO-starless-ghs-pass2.fit"
    roundtrip = work / "SHO-starless-ghs-pass2-roundtrip.fit"
    source_evidence = inspect_fits(working_source)
    output_evidence = inspect_fits(output)
    roundtrip_evidence = inspect_fits(roundtrip)

    preview_script = candidate / "previews.ssf"
    preview_script.write_text(preview_script_text(), encoding="utf-8")
    preview_run = run_siril_script(
        directory=work,
        script=preview_script,
        stdout_log=logs / "preview-stdout.log",
        stderr_log=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 600),
    )

    before_preview = previews / "SHO-starless-ghs-pass1-before-linear.png"
    after_preview = previews / "SHO-starless-ghs-pass2-linear.png"
    failures = []
    if preview_run["exit_status"] != 0:
        failures.append(f"preview exit status {preview_run['exit_status']}")
    if preview_run["timed_out"]:
        failures.append("preview timed out")
    if preview_run["fatal_log_markers"]:
        failures.append(f"preview fatal markers {preview_run['fatal_log_markers']}")
    for preview in (before_preview, after_preview):
        if not preview.is_file():
            failures.append(f"missing preview {preview}")

    before_preview_sha256 = (
        sha256_file(before_preview) if before_preview.is_file() else None
    )
    after_preview_sha256 = (
        sha256_file(after_preview) if after_preview.is_file() else None
    )
    if (
        before_preview_sha256 is not None
        and after_preview_sha256 is not None
        and before_preview_sha256 == after_preview_sha256
    ):
        failures.append(
            "before/after previews are byte-identical; visual-review "
            "provenance is invalid"
        )

    if failures:
        raise GhsStretchError(
            f"Preview generation failed ({failures}); evidence preserved at "
            f"{candidate}"
        )

    quality = production_quality_assessment(
        working_source, output, roundtrip
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
            "before_linear": str(before_preview),
            "after_linear": str(after_preview),
        },
        "preview_provenance": {
            "before_source_fits": str(working_source),
            "before_source_fits_sha256": source_evidence.sha256,
            "after_source_fits": str(output),
            "after_source_fits_sha256": output_evidence.sha256,
            "before_png_sha256": before_preview_sha256,
            "after_png_sha256": after_preview_sha256,
            "before_after_pngs_distinct": (
                before_preview_sha256 != after_preview_sha256
            ),
        },
        "quality_assessment": quality,
        "status": (
            "satisfactory" if quality["satisfactory"] else "needs_review"
        ),
    }



def preserve_failed_publish_staging(run_root: Path) -> Path | None:
    """Move an existing failed publish-staging aside without deleting it."""
    publish_dir = run_root / "publish-staging"
    if not publish_dir.exists():
        return None

    destination = run_root / f"failed-publish-staging-{unique_id()}"
    if destination.exists():
        raise GhsStretchError(
            f"Failed-publication preservation destination exists: "
            f"{destination}"
        )

    try:
        publish_dir.rename(destination)
    except Exception as exc:
        raise GhsStretchError(
            "Could not preserve existing failed publish-staging evidence: "
            f"{publish_dir} -> {destination}: {exc}"
        ) from exc

    return destination


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
    source_run_helper_version: str,
) -> dict[str, Any]:
    existing = paths["stable"].exists()
    if existing and not fresh_run:
        raise GhsStretchError(
            f"Canonical GHS pass-2 directory already exists: {paths['stable']}. "
            "Use --fresh-run to preserve and replace it safely."
        )

    gate = publication_gate(candidates)
    if not gate["publication_permitted"]:
        raise GhsStretchError(
            f"Publication blocked by adaptive gate: {gate['reason']}"
        )
    if not candidate_publication_eligible(candidate):
        raise GhsStretchError(
            "Only technically satisfactory balanced pass-2 candidates "
            "can be published."
        )
    if not visual_selection_notes.strip():
        raise GhsStretchError("Visual selection notes are required.")

    cdir = Path(candidate["candidate_directory"])
    candidate_output = cdir / "work" / "SHO-starless-ghs-pass2.fit"
    candidate_before = (
        cdir / "previews" / "SHO-starless-ghs-pass1-before-linear.png"
    )
    candidate_after = (
        cdir / "previews" / "SHO-starless-ghs-pass2-linear.png"
    )

    required_candidate_files = (
        candidate_output,
        candidate_before,
        candidate_after,
    )
    for required in required_candidate_files:
        if not required.is_file():
            raise GhsStretchError(
                f"Selected candidate publication asset is missing: "
                f"{required}"
            )

    provenance = candidate.get("preview_provenance", {})
    actual_before_png_sha = sha256_file(candidate_before)
    actual_after_png_sha = sha256_file(candidate_after)

    if (
        provenance.get("before_source_fits_sha256")
        != source_evidence.sha256
    ):
        raise GhsStretchError(
            "Selected candidate before-preview FITS provenance does not "
            "match the current canonical GHS pass-1 source."
        )
    if (
        provenance.get("after_source_fits_sha256")
        != candidate.get("output", {}).get("sha256")
    ):
        raise GhsStretchError(
            "Selected candidate after-preview FITS provenance does not "
            "match the candidate GHS pass-2 output."
        )
    if (
        provenance.get("before_png_sha256")
        != actual_before_png_sha
    ):
        raise GhsStretchError(
            "Selected candidate before-preview PNG checksum changed."
        )
    if (
        provenance.get("after_png_sha256")
        != actual_after_png_sha
    ):
        raise GhsStretchError(
            "Selected candidate after-preview PNG checksum changed."
        )
    if (
        provenance.get("before_after_pngs_distinct") is not True
        or actual_before_png_sha == actual_after_png_sha
    ):
        raise GhsStretchError(
            "Selected candidate before/after preview provenance is invalid."
        )

    preserved_failed_publish_staging = (
        preserve_failed_publish_staging(run_root)
    )

    publish_dir = run_root / "publish-staging"
    publish_dir.mkdir(parents=True, exist_ok=False)

    staged_output = publish_dir / "SHO-starless-ghs-pass2.fit"
    staged_before = (
        publish_dir / "SHO-starless-ghs-pass1-before-linear.png"
    )
    staged_after = publish_dir / "SHO-starless-ghs-pass2-linear.png"
    shutil.copy2(candidate_output, staged_output)
    shutil.copy2(candidate_before, staged_before)
    shutil.copy2(candidate_after, staged_after)

    staged_evidence = inspect_fits(staged_output)
    if staged_evidence.sha256 != candidate["output"]["sha256"]:
        raise GhsStretchError(
            "Selected pass-2 candidate checksum changed during staging."
        )

    final_evidence = asdict(staged_evidence)
    final_evidence["path"] = str(paths["stable_output"])

    previous = (
        run_root / "previous-processing-ghs-pass2" if existing else None
    )
    if previous is not None and previous.exists():
        raise GhsStretchError(
            f"Preservation destination already exists: {previous}"
        )

    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "status": "ready",
        "helper_version": VERSION,
        "source_run_helper_version": source_run_helper_version,
        "publication_recovery_from_compatible_run": (
            source_run_helper_version != VERSION
        ),
        "failed_publish_staging_preserved_at": (
            str(preserved_failed_publish_staging)
            if preserved_failed_publish_staging is not None
            else None
        ),
        "stage_order": {
            "upstream": UPSTREAM_STAGE,
            "current": CURRENT_STAGE,
            "downstream": NEXT_STAGE,
        },
        "upstream_stage": UPSTREAM_STAGE,
        "next_stage": NEXT_STAGE,
        "project": paths["project"].name,
        "project_path": str(paths["project"]),
        "source_ghs_pass1_manifest": str(paths["source_manifest"]),
        "source_ghs_pass1_manifest_sha256": sha256_file(
            paths["source_manifest"]
        ),
        "source_ghs_pass1_status": source_manifest.get("status"),
        "source_ghs_pass1_helper_version": source_manifest.get(
            "helper_version"
        ),
        "source": asdict(source_evidence),
        "adaptive_policy": {
            "maximum_total_candidates": MAX_CANDIDATES_LIMIT,
            "parameter_bounds": PARAMETER_BOUNDS,
            "selection_targets": SELECTION_TARGETS,
            "arbitrary_parameters_permitted": False,
        },
        "publication_gate": gate,
        "candidates": candidates,
        "recommended_candidate": (
            recommended["candidate"] if recommended else None
        ),
        "recommended_selection_score": (
            recommended["selection_score"] if recommended else None
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
            "before_linear": str(paths["stable_before_preview"]),
            "after_linear": str(paths["stable_after_preview"]),
        },
        "stable_paths": {
            "directory": str(paths["stable"]),
            "output": str(paths["stable_output"]),
            "before_preview": str(paths["stable_before_preview"]),
            "after_preview": str(paths["stable_after_preview"]),
            "manifest": str(paths["stable_manifest"]),
        },
        "previous_processing_ghs_pass2_preserved_at": (
            str(previous) if previous is not None else None
        ),
        "siril": siril,
        "visual_review_completed": True,
        "black_point_processing_permitted": True,
    }
    json_dump_atomic(
        publish_dir / "ghs-pass2-manifest.json",
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



def load_run_record(run_root: Path) -> tuple[Path, dict[str, Any]]:
    manifest_path = run_root / "run-manifest.json"
    if not manifest_path.is_file():
        raise GhsStretchError(
            f"Adaptive run manifest does not exist: {manifest_path}"
        )
    try:
        record = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GhsStretchError(
            f"Could not read adaptive run manifest {manifest_path}: {exc}"
        ) from exc
    return manifest_path, record


def validate_compatible_run_record(
    *,
    run_record: dict[str, Any],
    project_name: str,
    source_sha256: str,
) -> str:
    run_version = str(run_record.get("helper_version", ""))
    if run_version not in PUBLISH_COMPATIBLE_RUN_HELPER_VERSIONS:
        raise GhsStretchError(
            f"Run helper version {run_version!r} is not compatible with "
            f"installed helper {VERSION}."
        )
    if run_record.get("project_name") != project_name:
        raise GhsStretchError(
            "Run manifest project does not match the requested project."
        )
    if run_record.get("source", {}).get("sha256") != source_sha256:
        raise GhsStretchError(
            "Run source checksum does not match the current canonical "
            "GHS pass-1 source."
        )
    return run_version


def record_visual_selection(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
    candidate_name: str,
    compared_candidates: list[str],
    visual_selection_notes: str,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    _, source_evidence = validate_source(paths)
    run_manifest_path, run_record = load_run_record(run_root)

    run_version = validate_compatible_run_record(
        run_record=run_record,
        project_name=project_name,
        source_sha256=source_evidence.sha256,
    )

    if run_record.get("canonical_output_changed") is True:
        raise GhsStretchError(
            "This adaptive run has already been published."
        )
    if run_record.get("publication_permitted") is not True:
        raise GhsStretchError(
            "This adaptive run is not publication-permitted."
        )

    candidates = run_record.get("candidates", [])
    gate = publication_gate(candidates)
    if not gate["publication_permitted"]:
        raise GhsStretchError(
            f"Publication is blocked by the adaptive gate: {gate['reason']}"
        )

    eligible = list(gate["publication_eligible_candidates"])
    normalized_compared = sorted(set(compared_candidates))
    if normalized_compared != sorted(eligible):
        raise GhsStretchError(
            "CodeWarrior must visually compare every publication-eligible "
            f"candidate before selection. Expected {sorted(eligible)}, "
            f"recorded {normalized_compared}."
        )

    matches = [
        item for item in candidates
        if item.get("candidate") == candidate_name
    ]
    if len(matches) != 1:
        raise GhsStretchError(
            f"Candidate {candidate_name!r} is not uniquely present."
        )
    candidate = matches[0]
    if not candidate_publication_eligible(candidate):
        raise GhsStretchError(
            f"Candidate {candidate_name} is not publication-eligible."
        )

    notes = visual_selection_notes.strip()
    if not notes:
        raise GhsStretchError("Visual selection notes are required.")

    existing = run_record.get("visual_selection")
    if isinstance(existing, dict) and existing.get("completed") is True:
        existing_candidate = existing.get("selected_candidate")
        existing_compared = sorted(
            set(existing.get("satisfactory_candidates_compared", []))
        )
        if (
            existing_candidate != candidate_name
            or existing_compared != normalized_compared
        ):
            raise GhsStretchError(
                "A different completed visual selection is already recorded "
                "for this run. Do not silently replace reviewed evidence."
            )
        return {
            "status": "ready_to_publish",
            "helper_version": VERSION,
            "source_run_helper_version": run_version,
            "project": str(paths["project"]),
            "run_root": str(run_root),
            "selected_candidate": existing_candidate,
            "satisfactory_candidates_compared": existing_compared,
            "visual_review_completed": True,
            "publication_permitted": True,
            "black_point_processing_permitted": False,
            "message": "Existing durable visual selection is ready to publish.",
        }

    recommended = recommended_candidate(candidates)
    selection = {
        "required": True,
        "completed": True,
        "reviewer": "CodeWarrior",
        "recorded_at": utc_now(),
        "selected_candidate": candidate_name,
        "selected_candidate_was_recommended": (
            recommended is not None
            and recommended.get("candidate") == candidate_name
        ),
        "satisfactory_candidates_compared": normalized_compared,
        "notes": notes,
        "selected_output_sha256": candidate.get("output", {}).get("sha256"),
        "selected_before_preview_sha256": candidate.get(
            "preview_provenance", {}
        ).get("before_png_sha256"),
        "selected_after_preview_sha256": candidate.get(
            "preview_provenance", {}
        ).get("after_png_sha256"),
    }

    run_record["status"] = "ready_to_publish"
    run_record["visual_selection"] = selection
    run_record["selected_candidate"] = candidate_name
    run_record["visual_selection_notes"] = notes
    run_record["visual_review_completed"] = True
    run_record["black_point_processing_permitted"] = False
    json_dump_atomic(run_manifest_path, run_record)

    return {
        "status": "ready_to_publish",
        "helper_version": VERSION,
        "source_run_helper_version": run_version,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "selected_candidate": candidate_name,
        "recommended_candidate": (
            recommended.get("candidate") if recommended else None
        ),
        "selected_candidate_was_recommended": selection[
            "selected_candidate_was_recommended"
        ],
        "satisfactory_candidates_compared": normalized_compared,
        "visual_review_completed": True,
        "publication_permitted": True,
        "black_point_processing_permitted": False,
        "message": (
            "Visual selection is durably recorded. Publication may proceed "
            "without another user prompt."
        ),
    }


def load_stage_intents(
    paths: dict[str, Path],
) -> list[tuple[float, Path, dict[str, Any]]]:
    results: list[tuple[float, Path, dict[str, Any]]] = []
    intent_dir = paths["intents"]
    if not intent_dir.is_dir():
        return results

    for intent_path in intent_dir.glob("fresh-run-*.json"):
        if not intent_path.is_file():
            continue
        try:
            record = json.loads(intent_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        results.append(
            (
                intent_path.stat().st_mtime,
                intent_path,
                record,
            )
        )

    return sorted(results, key=lambda item: item[0], reverse=True)


def matching_stage_intent(
    *,
    paths: dict[str, Path],
    project_name: str,
    source_sha256: str,
    canonical_sha256: str,
    statuses: set[str] | frozenset[str],
) -> tuple[Path, dict[str, Any]] | None:
    for _, intent_path, record in load_stage_intents(paths):
        if record.get("project_name") != project_name:
            continue
        if record.get("source_sha256") != source_sha256:
            continue
        if record.get("canonical_output_sha256") != canonical_sha256:
            continue
        if record.get("status") not in statuses:
            continue
        return intent_path, record
    return None


def current_canonical_sha(
    paths: dict[str, Path],
    canonical_status: dict[str, Any],
) -> str:
    if canonical_status.get("status") != "ready":
        raise GhsStretchError(
            "Fresh-run confirmation requires a ready canonical result."
        )
    if canonical_status.get("canonical_manifest_compatible") is not True:
        raise GhsStretchError(
            "Fresh-run confirmation requires a compatible canonical manifest."
        )

    expected_sha = str(
        canonical_status.get("output", {}).get("sha256", "")
    )
    if not expected_sha:
        raise GhsStretchError(
            "Ready canonical status does not provide an output checksum."
        )
    if not paths["stable_output"].is_file():
        raise GhsStretchError(
            f"Canonical pass-2 FITS is missing: {paths['stable_output']}"
        )
    actual_sha = sha256_file(paths["stable_output"])
    if actual_sha != expected_sha:
        raise GhsStretchError(
            "Canonical pass-2 FITS checksum changed since status validation."
        )
    return actual_sha


def begin_stage(
    workspace: Path,
    project_name: str,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    _, source_evidence = validate_source(paths)
    state = workflow_state(workspace, project_name)
    action = state.get("action")

    if action in (
        "review_select_publish",
        "publish_recorded_selection",
        "stop",
    ):
        return {
            **state,
            "stage_entry": "begin",
            "confirmation_required": False,
        }

    if action != "run_review_select_publish":
        raise GhsStretchError(
            f"Unsupported begin-stage workflow action: {action!r}"
        )

    canonical_status = state.get("canonical_status", {})
    if canonical_status.get("status") != "ready":
        return {
            **state,
            "stage_entry": "begin",
            "confirmation_required": False,
            "message": (
                "No completed canonical GHS pass-2 result exists. "
                "Proceed with the full stage now."
            ),
        }

    canonical_sha = current_canonical_sha(paths, canonical_status)

    authorized = matching_stage_intent(
        paths=paths,
        project_name=project_name,
        source_sha256=source_evidence.sha256,
        canonical_sha256=canonical_sha,
        statuses={"fresh_run_authorized"},
    )
    if authorized is not None:
        intent_path, intent = authorized
        return {
            "status": "fresh_run_authorized",
            "action": "run_review_select_publish",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "stage_entry": "begin",
            "confirmation_required": False,
            "fresh_run_authorized": True,
            "fresh_run_request_id": intent.get("request_id"),
            "fresh_run_intent": str(intent_path),
            "authorized_at": intent.get("authorized_at"),
            "canonical_status": canonical_status,
            "black_point_processing_permitted": False,
            "message": (
                "Fresh-run confirmation is already durably recorded for "
                "this canonical result. Continue the complete GHS pass-2 "
                "stage without asking the user again."
            ),
        }

    pending = matching_stage_intent(
        paths=paths,
        project_name=project_name,
        source_sha256=source_evidence.sha256,
        canonical_sha256=canonical_sha,
        statuses={"confirmation_required"},
    )

    if pending is None:
        paths["intents"].mkdir(parents=True, exist_ok=True)
        request_id = unique_id()
        intent_path = paths["intents"] / f"fresh-run-{request_id}.json"
        requested_at = utc_now()
        intent = {
            "schema_version": 1,
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "project_name": project_name,
            "request_id": request_id,
            "status": "confirmation_required",
            "requested_at": requested_at,
            "authorized_at": None,
            "consumed_at": None,
            "consumed_by_run_root": None,
            "source_sha256": source_evidence.sha256,
            "canonical_output_sha256": canonical_sha,
            "canonical_manifest_sha256": sha256_file(
                paths["stable_manifest"]
            ),
            "history": [
                {
                    "at": requested_at,
                    "status": "confirmation_required",
                    "reason": (
                        "Explicit processing request encountered an already-"
                        "completed compatible canonical GHS pass-2 result."
                    ),
                }
            ],
        }
        json_dump_atomic(intent_path, intent)
    else:
        intent_path, intent = pending

    return {
        "status": "confirmation_required",
        "action": "confirm_fresh_run",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "stage_entry": "begin",
        "confirmation_required": True,
        "fresh_run_authorized": False,
        "fresh_run_request_id": intent.get("request_id"),
        "fresh_run_intent": str(intent_path),
        "canonical_status": canonical_status,
        "black_point_processing_permitted": False,
        "question": (
            f"GHS pass 2 for {project_name} has already completed "
            "successfully. Do you want me to run it again as a fresh run?"
        ),
        "message": (
            "Stop and ask the user the returned question. Do not create "
            "new candidates until the user confirms."
        ),
    }


def confirm_fresh_run(
    workspace: Path,
    project_name: str,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    _, source_evidence = validate_source(paths)
    state = workflow_state(workspace, project_name)

    if state.get("action") in (
        "review_select_publish",
        "publish_recorded_selection",
    ):
        return {
            **state,
            "confirmation_required": False,
            "fresh_run_authorized": False,
            "message": (
                "A compatible incomplete run already exists; resume it "
                "instead of authorizing another fresh run."
            ),
        }

    if state.get("action") == "stop":
        return {
            **state,
            "confirmation_required": False,
            "fresh_run_authorized": False,
        }

    canonical_status = state.get("canonical_status", {})
    canonical_sha = current_canonical_sha(paths, canonical_status)

    existing_authorized = matching_stage_intent(
        paths=paths,
        project_name=project_name,
        source_sha256=source_evidence.sha256,
        canonical_sha256=canonical_sha,
        statuses={"fresh_run_authorized"},
    )
    if existing_authorized is not None:
        intent_path, intent = existing_authorized
        return {
            "status": "fresh_run_authorized",
            "action": "run_review_select_publish",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "confirmation_required": False,
            "fresh_run_authorized": True,
            "fresh_run_request_id": intent.get("request_id"),
            "fresh_run_intent": str(intent_path),
            "authorized_at": intent.get("authorized_at"),
            "canonical_status": canonical_status,
            "black_point_processing_permitted": False,
            "message": (
                "Fresh-run confirmation was already recorded. Continue "
                "the complete stage without asking again."
            ),
        }

    pending = matching_stage_intent(
        paths=paths,
        project_name=project_name,
        source_sha256=source_evidence.sha256,
        canonical_sha256=canonical_sha,
        statuses={"confirmation_required"},
    )
    if pending is None:
        raise GhsStretchError(
            "No pending fresh-run confirmation exists for the current "
            "canonical result. Begin the stage first."
        )

    intent_path, intent = pending
    authorized_at = utc_now()
    intent["status"] = "fresh_run_authorized"
    intent["authorized_at"] = authorized_at
    history = list(intent.get("history", []))
    history.append(
        {
            "at": authorized_at,
            "status": "fresh_run_authorized",
            "reason": "User explicitly confirmed a fresh rerun.",
        }
    )
    intent["history"] = history
    json_dump_atomic(intent_path, intent)

    return {
        "status": "fresh_run_authorized",
        "action": "run_review_select_publish",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "confirmation_required": False,
        "fresh_run_authorized": True,
        "fresh_run_request_id": intent.get("request_id"),
        "fresh_run_intent": str(intent_path),
        "authorized_at": authorized_at,
        "canonical_status": canonical_status,
        "black_point_processing_permitted": False,
        "message": (
            "Fresh rerun is durably authorized. Continue the complete GHS "
            "pass-2 stage now. If interrupted before candidate generation "
            "finishes, do not ask the user to confirm again."
        ),
    }


def consume_fresh_run_authorization(
    *,
    intent_path: Path,
    intent: dict[str, Any],
    run_root: Path,
) -> None:
    consumed_at = utc_now()
    intent["status"] = "consumed"
    intent["consumed_at"] = consumed_at
    intent["consumed_by_run_root"] = str(run_root)
    history = list(intent.get("history", []))
    history.append(
        {
            "at": consumed_at,
            "status": "consumed",
            "reason": (
                "Fresh candidate run completed durable run-manifest creation."
            ),
            "run_root": str(run_root),
        }
    )
    intent["history"] = history
    json_dump_atomic(intent_path, intent)


def workflow_state(
    workspace: Path,
    project_name: str,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    source_manifest, source_evidence = validate_source(paths)
    canonical_status = status_project(workspace, project_name)

    compatible_runs: list[tuple[float, Path, dict[str, Any]]] = []
    blocked_runs: list[tuple[float, Path, dict[str, Any]]] = []

    if paths["runs"].is_dir():
        for run_root in paths["runs"].iterdir():
            if not run_root.is_dir():
                continue
            manifest_path = run_root / "run-manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                record = json.loads(
                    manifest_path.read_text(encoding="utf-8")
                )
            except Exception:
                continue

            if record.get("project_name") != project_name:
                continue
            if (
                str(record.get("helper_version", ""))
                not in PUBLISH_COMPATIBLE_RUN_HELPER_VERSIONS
            ):
                continue
            if (
                record.get("source", {}).get("sha256")
                != source_evidence.sha256
            ):
                continue
            if record.get("canonical_output_changed") is True:
                continue

            mtime = manifest_path.stat().st_mtime
            if record.get("publication_permitted") is True:
                compatible_runs.append((mtime, run_root, record))
            elif record.get("completed_candidate_count", 0):
                blocked_runs.append((mtime, run_root, record))

    if compatible_runs:
        _, run_root, record = sorted(
            compatible_runs,
            key=lambda item: item[0],
            reverse=True,
        )[0]
        candidates = record.get("candidates", [])
        gate = publication_gate(candidates)
        selection = record.get("visual_selection")
        eligible = list(gate["publication_eligible_candidates"])

        if (
            isinstance(selection, dict)
            and selection.get("completed") is True
        ):
            return {
                "status": "ready_to_publish",
                "action": "publish_recorded_selection",
                "helper_version": VERSION,
                "project": str(paths["project"]),
                "run_root": str(run_root),
                "source_run_helper_version": record.get("helper_version"),
                "selected_candidate": selection.get(
                    "selected_candidate"
                ),
                "visual_review_completed": True,
                "publication_permitted": True,
                "publication_eligible_candidates": eligible,
                "recommended_candidate": record.get(
                    "recommended_candidate"
                ),
                "publish_staging_exists": (
                    run_root / "publish-staging"
                ).exists(),
                "canonical_status": canonical_status,
                "black_point_processing_permitted": False,
                "message": (
                    "Resume this run by publishing the already-recorded "
                    "CodeWarrior visual selection. Do not regenerate or "
                    "re-review candidates."
                ),
            }

        return {
            "status": "awaiting_visual_selection",
            "action": "review_select_publish",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "run_root": str(run_root),
            "source_run_helper_version": record.get("helper_version"),
            "visual_review_completed": False,
            "publication_permitted": True,
            "publication_eligible_candidates": eligible,
            "recommended_candidate": record.get("recommended_candidate"),
            "candidate_previews": {
                item["candidate"]: item.get("previews")
                for item in candidates
                if item.get("candidate") in eligible
            },
            "canonical_status": canonical_status,
            "black_point_processing_permitted": False,
            "message": (
                "Resume this existing run: CodeWarrior must inspect every "
                "eligible preview, record one durable selection, publish it, "
                "and verify status. Do not regenerate candidates."
            ),
        }

    if blocked_runs:
        _, run_root, record = sorted(
            blocked_runs,
            key=lambda item: item[0],
            reverse=True,
        )[0]
        return {
            "status": "blocked",
            "action": "stop",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "run_root": str(run_root),
            "reason": record.get(
                "publication_gate", {}
            ).get("reason", "Latest compatible run is not publishable."),
            "canonical_status": canonical_status,
            "black_point_processing_permitted": False,
            "message": (
                "Do not automatically create another candidate set after a "
                "bounded run failed its publication gate."
            ),
        }

    return {
        "status": "start_new_run",
        "action": "run_review_select_publish",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "source_sha256": source_evidence.sha256,
        "source_pass1_helper_version": source_manifest.get(
            "helper_version"
        ),
        "canonical_status": canonical_status,
        "black_point_processing_permitted": False,
        "message": (
            "No compatible incomplete run exists. Start one fresh three-"
            "candidate pass-2 run, then review, select, publish, and verify "
            "within this same stage invocation."
        ),
    }


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
            f"max-candidates must be between 1 and {MAX_CANDIDATES_LIMIT}."
        )

    paths = project_paths(workspace, project_name)
    fresh_run_requested = bool(fresh_run)
    fresh_run_effective = bool(fresh_run)
    fresh_run_inferred = False
    fresh_run_inference_reason = None
    fresh_run_authorized = False
    fresh_run_request_id = None
    fresh_run_intent_path = None
    fresh_run_intent_record = None

    if paths["stable"].exists():
        state = workflow_state(workspace, project_name)
        action = state.get("action")
        canonical_status = state.get("canonical_status", {})

        if action in (
            "review_select_publish",
            "publish_recorded_selection",
        ):
            raise GhsStretchError(
                "A compatible incomplete GHS pass-2 run already exists. "
                f"Resume it via begin/workflow-state action {action!r}; "
                "do not create a duplicate candidate run."
            )
        if action == "stop":
            raise GhsStretchError(
                "The latest compatible GHS pass-2 run is blocked by its "
                "bounded publication gate. Preserve that evidence and do "
                "not automatically create another candidate set."
            )
        if action != "run_review_select_publish":
            raise GhsStretchError(
                f"Canonical output exists at {paths['stable']}, but "
                f"workflow-state returned unsupported action {action!r}."
            )

        _, source_for_auth = validate_source(paths)
        canonical_sha = current_canonical_sha(paths, canonical_status)
        authorized = matching_stage_intent(
            paths=paths,
            project_name=project_name,
            source_sha256=source_for_auth.sha256,
            canonical_sha256=canonical_sha,
            statuses={"fresh_run_authorized"},
        )
        if authorized is None:
            raise GhsStretchError(
                "GHS pass 2 already has a completed canonical result. "
                "Fresh-run confirmation is required before generating new "
                "candidates. Run begin, ask the user whether they want a "
                "fresh run, and after an affirmative reply run "
                "confirm-fresh. The --fresh-run flag cannot bypass this "
                "confirmation gate."
            )

        fresh_run_intent_path, fresh_run_intent_record = authorized
        fresh_run_authorized = True
        fresh_run_request_id = fresh_run_intent_record.get("request_id")
        fresh_run_effective = True
        fresh_run_inferred = not fresh_run_requested
        fresh_run_inference_reason = (
            "Fresh rerun was explicitly confirmed by the user and durably "
            "authorized for the current canonical result."
        )

    siril = siril_version()
    source_manifest, source_evidence = validate_source(paths)

    run_started_at = utc_now()
    run_root = paths["runs"] / f"ghs-pass2-{unique_id()}"
    run_root.mkdir(parents=True, exist_ok=False)

    # Re-anchor baseline SP to the current pass-1 median.
    source_median = float(source_evidence.median)
    baseline_params = normalize_parameters({
        "D": 1.40,
        "B": 3.00,
        "SP": max(0.040, min(0.180, source_median)),
        "LP": 0.0,
        "HP": 0.950,
    })

    candidates = []
    c0 = execute_candidate(
        paths["source"], run_root, timeout_seconds,
        candidate_index=0,
        parameters=baseline_params,
        adaptation_reason=(
            "Moderate source-anchored pass-2 baseline centered on the "
            "published pass-1 histogram peak."
        ),
    )
    candidates.append(c0)

    if max_candidates >= 2:
        p1, r1 = plan_second_candidate(c0)
        c1 = execute_candidate(
            paths["source"], run_root, timeout_seconds,
            candidate_index=1, parameters=p1, adaptation_reason=r1,
        )
        candidates.append(c1)

    if max_candidates >= 3:
        p2, r2 = plan_third_candidate(c0, candidates[1])
        c2 = execute_candidate(
            paths["source"], run_root, timeout_seconds,
            candidate_index=2, parameters=p2, adaptation_reason=r2,
        )
        candidates.append(c2)

    gate = publication_gate(candidates)
    recommended = (
        recommended_candidate(candidates)
        if gate["publication_permitted"] else None
    )

    record = {
        "schema_version": 1,
        "status": gate["status"],
        "created_at": utc_now(),
        "run_started_at": run_started_at,
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "project_name": project_name,
        "run_root": str(run_root),
        "fresh_run_requested": fresh_run_requested,
        "fresh_run_effective": fresh_run_effective,
        "fresh_run_inferred": fresh_run_inferred,
        "fresh_run_inference_reason": fresh_run_inference_reason,
        "fresh_run_authorized": fresh_run_authorized,
        "fresh_run_request_id": fresh_run_request_id,
        "fresh_run_intent": (
            str(fresh_run_intent_path)
            if fresh_run_intent_path is not None
            else None
        ),
        "maximum_total_candidates": MAX_CANDIDATES_LIMIT,
        "requested_candidate_count": max_candidates,
        "completed_candidate_count": len(candidates),
        "parameter_bounds": PARAMETER_BOUNDS,
        "selection_targets": SELECTION_TARGETS,
        "source": asdict(source_evidence),
        "source_manifest": str(paths["source_manifest"]),
        "source_manifest_sha256": sha256_file(paths["source_manifest"]),
        "source_pass1_helper_version": source_manifest.get("helper_version"),
        "siril": siril,
        "candidates": candidates,
        "publication_gate": gate,
        "publication_permitted": gate["publication_permitted"],
        "publication_eligible_candidates": gate[
            "publication_eligible_candidates"
        ],
        "recommended_candidate": (
            recommended["candidate"] if recommended else None
        ),
        "recommended_selection_score": (
            recommended["selection_score"] if recommended else None
        ),
        "canonical_output_changed": False,
        "visual_selection_required": gate["publication_permitted"],
        "visual_review_completed": False,
        "visual_selection": None,
        "selected_candidate": None,
        "publish_command_template": (
            f"{Path(__file__)} publish --project "
            f"{json.dumps(project_name)} --run-root "
            f"{json.dumps(str(run_root))} --candidate <candidate-XX> "
            f"--visual-notes <review-notes> --fresh-run"
            if gate["publication_permitted"] else None
        ),
        "black_point_processing_permitted": False,
        "message": (
            "Compare every publication-eligible pass-2 preview and publish "
            "exactly one candidate."
            if gate["publication_permitted"] else gate["reason"]
        ),
    }
    json_dump_atomic(run_root / "run-manifest.json", record)

    if (
        fresh_run_authorized
        and fresh_run_intent_path is not None
        and fresh_run_intent_record is not None
    ):
        consume_fresh_run_authorization(
            intent_path=fresh_run_intent_path,
            intent=fresh_run_intent_record,
            run_root=run_root,
        )

    return record




def publish_project(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
    candidate_name: str | None,
    visual_selection_notes: str | None,
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
    source_run_helper_version = str(
        run_record.get("helper_version", "")
    )
    if (
        source_run_helper_version
        not in PUBLISH_COMPATIBLE_RUN_HELPER_VERSIONS
    ):
        raise GhsStretchError(
            f"Run helper version {source_run_helper_version!r} is not "
            f"publication-compatible with installed helper {VERSION}. "
            f"Accepted run versions: "
            f"{sorted(PUBLISH_COMPATIBLE_RUN_HELPER_VERSIONS)}"
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

    selection = run_record.get("visual_selection")
    if (
        source_run_helper_version == VERSION
        and not (
            isinstance(selection, dict)
            and selection.get("completed") is True
        )
    ):
        raise GhsStretchError(
            "A v1.1.0 run must record CodeWarrior visual selection with "
            "the select command before publication."
        )

    if isinstance(selection, dict) and selection.get("completed") is True:
        recorded_candidate = selection.get("selected_candidate")
        recorded_notes = str(selection.get("notes", "")).strip()
        recorded_compared = sorted(
            set(selection.get("satisfactory_candidates_compared", []))
        )
        expected_compared = sorted(
            gate["publication_eligible_candidates"]
        )
        if recorded_compared != expected_compared:
            raise GhsStretchError(
                "Recorded visual selection does not prove comparison of "
                "every currently eligible candidate."
            )
        if candidate_name is not None and candidate_name != recorded_candidate:
            raise GhsStretchError(
                "Requested candidate conflicts with the durable visual "
                "selection already recorded for this run."
            )
        if (
            visual_selection_notes is not None
            and visual_selection_notes.strip()
            and visual_selection_notes.strip() != recorded_notes
        ):
            raise GhsStretchError(
                "Requested visual notes conflict with the durable visual "
                "selection already recorded for this run."
            )
        candidate_name = str(recorded_candidate)
        visual_selection_notes = recorded_notes
    else:
        # Legacy 1.0.3/1.0.4 recovery remains supported.
        if candidate_name is None or not str(candidate_name).strip():
            raise GhsStretchError(
                "Legacy compatible run publication requires --candidate."
            )
        if (
            visual_selection_notes is None
            or not visual_selection_notes.strip()
        ):
            raise GhsStretchError(
                "Legacy compatible run publication requires --visual-notes."
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
            "Current GHS pass-1 source no longer matches the adaptive run."
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
        visual_selection_notes=str(visual_selection_notes),
        source_run_helper_version=source_run_helper_version,
    )

    result = {
        "status": "ready",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "source_run_helper_version": source_run_helper_version,
        "publication_recovery_from_compatible_run": (
            source_run_helper_version != VERSION
        ),
        "failed_publish_staging_preserved_at": manifest.get(
            "failed_publish_staging_preserved_at"
        ),
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
        "visual_selection_notes": str(
            visual_selection_notes
        ).strip(),
        "durable_visual_selection": run_record.get("visual_selection"),
        "stable_directory": str(paths["stable"]),
        "stable_output": str(paths["stable_output"]),
        "stable_before_preview": str(paths["stable_before_preview"]),
        "stable_after_preview": str(paths["stable_after_preview"]),
        "stable_manifest": str(paths["stable_manifest"]),
        "previous_processing_ghs_pass2_preserved_at": manifest.get(
            "previous_processing_ghs_pass2_preserved_at"
        ),
        "canonical_output_changed": True,
        "visual_review_completed": True,
        "black_point_processing_permitted": True,
        "manifest": manifest,
    }

    run_record["status"] = "published"
    run_record["published_at"] = utc_now()
    run_record["published_by_helper_version"] = VERSION
    run_record["publication_recovery_from_compatible_run"] = (
        source_run_helper_version != VERSION
    )
    run_record["failed_publish_staging_preserved_at"] = manifest.get(
        "failed_publish_staging_preserved_at"
    )
    run_record["canonical_output_changed"] = True
    run_record["selected_candidate"] = candidate_name
    run_record["visual_selection_notes"] = str(
        visual_selection_notes
    ).strip()
    run_record["stable_manifest"] = str(paths["stable_manifest"])
    run_record["visual_review_completed"] = True
    run_record["black_point_processing_permitted"] = True
    json_dump_atomic(run_manifest_path, run_record)
    json_dump_atomic(run_root / "publication-result.json", result)
    return result




def status_project(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if not paths["stable_manifest"].is_file():
        return {
            "status": "missing",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "stable_directory": str(paths["stable"]),
            "black_point_processing_permitted": False,
        }

    try:
        manifest = json.loads(
            paths["stable_manifest"].read_text(encoding="utf-8")
        )
    except Exception as exc:
        return {
            "status": "invalid",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "errors": [str(exc)],
            "black_point_processing_permitted": False,
        }

    manifest_helper_version = str(
        manifest.get("helper_version", "")
    )
    if (
        manifest_helper_version
        not in CANONICAL_COMPATIBLE_MANIFEST_HELPER_VERSIONS
    ):
        return {
            "status": "obsolete",
            "helper_version": VERSION,
            "manifest_helper_version": manifest_helper_version,
            "compatible_manifest_helper_versions": sorted(
                CANONICAL_COMPATIBLE_MANIFEST_HELPER_VERSIONS
            ),
            "project": str(paths["project"]),
            "manifest": str(paths["stable_manifest"]),
            "reason": (
                "Existing canonical GHS pass-2 output is not compatible "
                "with the installed helper."
            ),
            "black_point_processing_permitted": False,
        }

    errors = []
    source_manifest = {}
    source_evidence = output_evidence = None
    source_manifest_hash = None
    try:
        source_manifest, source_evidence = validate_source(paths)
        source_manifest_hash = sha256_file(paths["source_manifest"])

        if (
            manifest.get("source_ghs_pass1_manifest_sha256")
            != source_manifest_hash
        ):
            errors.append("GHS pass-2 manifest checksum changed.")
        if (
            manifest.get("source", {}).get("sha256")
            != source_evidence.sha256
        ):
            errors.append("GHS pass-2 source checksum changed.")

        if not paths["stable_output"].is_file():
            errors.append(f"Missing output: {paths['stable_output']}")
        else:
            output_evidence = asdict(inspect_fits(paths["stable_output"]))
            if (
                output_evidence["sha256"]
                != manifest.get("output", {}).get("sha256")
            ):
                errors.append("Output checksum does not match manifest.")
            if (
                output_evidence["bitpix"] != -32
                or output_evidence["finite_fraction"] != 1.0
                or output_evidence["width"] != source_evidence.width
                or output_evidence["height"] != source_evidence.height
            ):
                errors.append("Canonical GHS pass-2 format changed.")

        for key in ("stable_before_preview", "stable_after_preview"):
            if not paths[key].is_file():
                errors.append(f"Missing preview: {paths[key]}")

        if manifest.get("stage_order") != {
            "upstream": UPSTREAM_STAGE,
            "current": CURRENT_STAGE,
            "downstream": NEXT_STAGE,
        }:
            errors.append("GHS pass-2 stage order is invalid.")
        if manifest.get("visual_review_completed") is not True:
            errors.append("GHS pass-2 visual review is incomplete.")
        if manifest.get("black_point_processing_permitted") is not True:
            errors.append("Manifest does not permit black-point processing.")
    except Exception as exc:
        errors.append(str(exc))

    ready = (
        manifest.get("status") == "ready"
        and not errors
        and manifest.get("black_point_processing_permitted") is True
    )
    return {
        "status": "ready" if ready else "invalid",
        "helper_version": VERSION,
        "manifest_helper_version": manifest_helper_version,
        "canonical_manifest_compatible": (
            manifest_helper_version
            in CANONICAL_COMPATIBLE_MANIFEST_HELPER_VERSIONS
        ),
        "project": str(paths["project"]),
        "stable_directory": str(paths["stable"]),
        "manifest": str(paths["stable_manifest"]),
        "errors": errors,
        "upstream_summary": {
            "manifest": str(paths["source_manifest"]),
            "manifest_sha256": source_manifest_hash,
            "helper_version": source_manifest.get("helper_version"),
            "status": source_manifest.get("status"),
            "visual_review_completed": source_manifest.get(
                "visual_review_completed"
            ),
            "ghs_pass2_processing_permitted": source_manifest.get(
                "ghs_pass2_processing_permitted"
            ),
        },
        "source": (
            asdict(source_evidence) if source_evidence is not None else None
        ),
        "method": manifest.get("method"),
        "quality_assessment": manifest.get("quality_assessment"),
        "roundtrip_evidence": manifest.get("roundtrip_evidence"),
        "output": output_evidence,
        "previews": manifest.get("previews"),
        "recommended_candidate": manifest.get("recommended_candidate"),
        "selected_candidate": manifest.get("selected_candidate"),
        "selected_candidate_was_recommended": manifest.get(
            "selected_candidate_was_recommended"
        ),
        "visual_review_completed": ready,
        "next_stage": NEXT_STAGE,
        "black_point_processing_permitted": ready,
    }




def write_synthetic_fits(path: Path) -> None:
    rng = np.random.default_rng(180225)
    height = width = 512
    yy, xx = np.mgrid[0:height, 0:width]
    radial = np.exp(
        -(
            ((xx - 260.0) / 125.0) ** 2
            + ((yy - 250.0) / 105.0) ** 2
        )
    )
    pillar = np.exp(
        -(
            ((xx - 250.0) / 28.0) ** 2
            + ((yy - 275.0) / 85.0) ** 2
        )
    )
    base = 0.085 + 0.060 * radial - 0.018 * pillar
    image = np.empty((3, height, width), dtype=np.float32)
    image[0] = base * 0.92 + rng.normal(0.0, 0.0015, (height, width))
    image[1] = base * 1.08 + rng.normal(0.0, 0.0015, (height, width))
    image[2] = base * 0.88 + rng.normal(0.0, 0.0015, (height, width))
    image = np.clip(image, 0.02, 0.35).astype(np.float32)
    hdu = fits.PrimaryHDU(image)
    hdu.header["FILTER"] = "mixed_Starless"
    hdu.writeto(path)



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
    def fake(name: str, median: float, p99: float, maximum: float):
        metrics = {
            "output_luma_median": median,
            "output_luma_p90": median * 1.10,
            "output_luma_p99": p99,
            "output_maximum": maximum,
            "low_clip_fraction": 0.0,
            "high_clip_fraction": 0.0,
        }
        return {
            "candidate": name,
            "histogram_classification": histogram_classification(metrics),
            "selection_score": 0.0,
            "quality_assessment": {
                "satisfactory": True,
                "metrics": metrics,
            },
        }

    too_strong = [
        fake("candidate-00", 0.28, 0.60, 0.90),
        fake("candidate-01", 0.26, 0.58, 0.88),
        fake("candidate-02", 0.24, 0.55, 0.85),
    ]
    if publication_gate(too_strong)["publication_permitted"]:
        raise GhsStretchError(
            "Policy test failed: all-too-strong pass-2 run publishable."
        )

    balanced = [
        fake("candidate-00", 0.12, 0.25, 0.60),
        fake("candidate-01", 0.18, 0.35, 0.75),
        fake("candidate-02", 0.20, 0.40, 0.80),
    ]
    if not publication_gate(balanced)["publication_permitted"]:
        raise GhsStretchError(
            "Policy test failed: balanced pass-2 candidates blocked."
        )

    return {
        "status": "success",
        "median_target": SELECTION_TARGETS["output_luma_median"],
        "balanced_median_minimum": SELECTION_TARGETS[
            "balanced_median_minimum"
        ],
        "balanced_median_maximum": SELECTION_TARGETS[
            "balanced_median_maximum"
        ],
        "all_too_strong_publication_blocked": True,
        "balanced_publication_permitted": True,
        "parameter_bounds": PARAMETER_BOUNDS,
    }





def self_test(timeout_seconds: int) -> dict[str, Any]:
    siril = siril_version()
    root = (
        WORKSPACE
        / ".skill-self-tests"
        / "siril-ghs-stretch-pass2"
        / unique_id()
    )
    root.mkdir(parents=True, exist_ok=False)
    source = root / "synthetic-ghs-pass2.fit"
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
            "Generate, compare, and publish a bounded adaptive second GHS "
            "stretch from the reviewed GHS pass-2 FITS."
        )
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command")

    self_test_parser = subparsers.add_parser("self-test")
    self_test_parser.add_argument("--timeout", type=int, default=1800)

    begin_parser = subparsers.add_parser("begin")
    begin_parser.add_argument("--project", required=True)

    confirm_parser = subparsers.add_parser("confirm-fresh")
    confirm_parser.add_argument("--project", required=True)

    workflow_parser = subparsers.add_parser("workflow-state")
    workflow_parser.add_argument("--project", required=True)

    select_parser = subparsers.add_parser("select")
    select_parser.add_argument("--project", required=True)
    select_parser.add_argument("--run-root", required=True, type=Path)
    select_parser.add_argument("--candidate", required=True)
    select_parser.add_argument(
        "--compared",
        action="append",
        required=True,
        help=(
            "Publication-eligible candidate visually compared by CodeWarrior. "
            "Repeat once for every eligible candidate."
        ),
    )
    select_parser.add_argument(
        "--visual-notes",
        required=True,
        help=(
            "Concise visual comparison explaining why the selected "
            "candidate is preferred."
        ),
    )

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--project", required=True)
    run_parser.add_argument("--timeout", type=int, default=7200)
    run_parser.add_argument(
        "--max-candidates",
        type=int,
        default=MAX_CANDIDATES_LIMIT,
        help=(
            "Total candidates to generate. The hard maximum is three; "
            "candidate-00 is the moderate source-anchored pass-2 baseline."
        ),
    )
    run_parser.add_argument(
        "--fresh-run",
        action="store_true",
        help=(
            "Low-level run-mode flag. When a completed compatible canonical "
            "result already exists, this flag cannot bypass the mandatory "
            "fresh-run confirmation recorded by begin/confirm-fresh."
        ),
    )

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--project", required=True)
    publish_parser.add_argument("--run-root", required=True, type=Path)
    publish_parser.add_argument("--candidate")
    publish_parser.add_argument(
        "--visual-notes",
        help=(
            "Legacy compatible-run visual notes. For v1.1.0 runs, use "
            "the select command first; publish automatically uses the "
            "durable recorded selection."
        ),
    )
    publish_parser.add_argument(
        "--fresh-run",
        action="store_true",
        help=(
            "Permit preservation-safe replacement of an existing canonical "
            "processing/ghs-pass2 directory."
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
        elif args.command == "begin":
            payload = begin_stage(WORKSPACE, args.project)
        elif args.command == "confirm-fresh":
            payload = confirm_fresh_run(WORKSPACE, args.project)
        elif args.command == "workflow-state":
            payload = workflow_state(WORKSPACE, args.project)
        elif args.command == "select":
            payload = record_visual_selection(
                workspace=WORKSPACE,
                project_name=args.project,
                run_root=args.run_root.resolve(),
                candidate_name=args.candidate,
                compared_candidates=args.compared,
                visual_selection_notes=args.visual_notes,
            )
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
        if payload.get("status") in CLI_SUCCESS_STATUSES
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
