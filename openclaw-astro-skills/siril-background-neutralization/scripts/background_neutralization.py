#!/usr/bin/env python3
"""Adaptive linear background neutralization for the canonical starless SHO image."""

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


VERSION = "1.0.0"
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

MAX_CANDIDATES = 3
MIN_BOX_SIZE = 256
MAX_BOX_SIZE = 512
BOX_FRACTION = 0.14
OUTER_BAND_FRACTION = 0.30
MIN_VALID_REGION_FRACTION = 0.35
MAX_REGION_OVERLAP = 0.15

# Robust clipping within a selected background region.
LOWER_MAD_LIMIT = -2.8
UPPER_MAD_LIMIT = 2.0

FATAL_LOG_MARKERS = (
    "script execution failed",
    "command execution failed",
    "cannot load",
    "could not load",
    "not enough memory",
    "segmentation fault",
)


class BackgroundNeutralizationError(RuntimeError):
    """Raised when the background-neutralization stage cannot continue safely."""


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


@dataclass(frozen=True)
class Region:
    x: int
    y: int
    width: int
    height: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


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
        raise BackgroundNeutralizationError(
            f"FITS file does not exist: {path}"
        )

    with fits.open(path, memmap=True) as hdul:
        data = hdul[0].data
        header = hdul[0].header
        if data is None:
            raise BackgroundNeutralizationError(
                f"FITS contains no image data: {path}"
            )
        array = np.asarray(data)
        if array.ndim != 3 or array.shape[0] != 3:
            raise BackgroundNeutralizationError(
                f"Expected a 3-channel RGB FITS, found {array.shape}: {path}"
            )
        if array.dtype.kind != "f" or array.dtype.itemsize != 4:
            raise BackgroundNeutralizationError(
                f"Expected 32-bit floating-point data, found "
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
            channels=3,
            width=int(array.shape[2]),
            height=int(array.shape[1]),
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
    stable = processing / "background-neutralization"
    return {
        "project": project,
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
        "runs": project / ".siril-background-neutralization",
        "stable": stable,
        "stable_output": (
            stable
            / "SHO-starless-linear-denoised-neutralized.fit"
        ),
        "stable_before_preview": (
            stable
            / "SHO-starless-linear-denoised-before-linked.png"
        ),
        "stable_after_preview": (
            stable
            / "SHO-starless-linear-denoised-neutralized-linked.png"
        ),
        "stable_manifest": (
            stable / "background-neutralization-manifest.json"
        ),
    }


def siril_version() -> dict[str, Any]:
    if not SIRIL_APP.is_file() or not os.access(SIRIL_APP, os.X_OK):
        raise BackgroundNeutralizationError(
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
        raise BackgroundNeutralizationError(
            f"Could not read Siril version (exit {completed.returncode}): "
            f"{combined}"
        )
    if REQUIRED_SIRIL_VERSION not in combined:
        raise BackgroundNeutralizationError(
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
            'load "SHO-starless-linear-denoised-neutralized.fit"',
            "autostretch -linked",
            (
                'savepng "../previews/'
                'SHO-starless-linear-denoised-neutralized-linked"'
            ),
            "close",
            "",
        )
    )


def validate_source(
    paths: dict[str, Path],
) -> tuple[dict[str, Any], FitsEvidence]:
    if not paths["project"].is_dir():
        raise BackgroundNeutralizationError(
            f"Project does not exist: {paths['project']}"
        )
    if not paths["source_manifest"].is_file():
        raise BackgroundNeutralizationError(
            f"Linear-denoise manifest is missing: "
            f"{paths['source_manifest']}"
        )

    manifest = json.loads(
        paths["source_manifest"].read_text(encoding="utf-8")
    )
    if manifest.get("status") != "ready":
        raise BackgroundNeutralizationError(
            "Linear-denoise manifest status is not ready."
        )
    if not manifest.get("downstream_linear_processing_permitted"):
        raise BackgroundNeutralizationError(
            "Linear-denoise manifest does not permit downstream processing."
        )
    if manifest.get("helper_version") != REQUIRED_DENOISE_VERSION:
        raise BackgroundNeutralizationError(
            f"Expected linear-denoise helper {REQUIRED_DENOISE_VERSION}; "
            f"manifest reports {manifest.get('helper_version')}."
        )

    source_evidence = inspect_fits(paths["source"])
    expected_hash = manifest.get("output", {}).get("sha256")
    if not expected_hash:
        raise BackgroundNeutralizationError(
            "Linear-denoise manifest does not record the output SHA-256."
        )
    if source_evidence.sha256 != expected_hash:
        raise BackgroundNeutralizationError(
            "Canonical denoised FITS checksum does not match its manifest."
        )

    return manifest, source_evidence


def robust_mad(values: np.ndarray) -> float:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return math.nan
    centre = float(np.median(finite))
    return 1.4826 * float(np.median(np.abs(finite - centre)))


def region_iou(a: Region, b: Region) -> float:
    left = max(a.x, b.x)
    top = max(a.y, b.y)
    right = min(a.x + a.width, b.x + b.width)
    bottom = min(a.y + a.height, b.y + b.height)
    intersection = max(0, right - left) * max(0, bottom - top)
    if intersection == 0:
        return 0.0
    union = a.width * a.height + b.width * b.height - intersection
    return float(intersection / union)


def region_metrics(
    image: np.ndarray,
    region: Region,
    global_span: float,
) -> dict[str, Any]:
    crop = image[
        :,
        region.y : region.y + region.height,
        region.x : region.x + region.width,
    ].astype(np.float64, copy=False)
    luma = np.mean(crop, axis=0)
    sample = luma[::3, ::3]
    finite = sample[np.isfinite(sample)]
    if finite.size < 100:
        raise BackgroundNeutralizationError(
            f"Too few finite values in region {region}."
        )

    median = float(np.median(finite))
    mad = robust_mad(finite)
    p10 = float(np.percentile(finite, 10.0))
    p90 = float(np.percentile(finite, 90.0))
    contrast = p90 - p10

    horizontal = np.diff(sample, axis=1)
    vertical = np.diff(sample, axis=0)
    gradients = np.concatenate(
        (
            np.abs(horizontal[np.isfinite(horizontal)]),
            np.abs(vertical[np.isfinite(vertical)]),
        )
    )
    gradient = (
        float(np.median(gradients))
        if gradients.size
        else math.inf
    )

    finite_fraction = float(np.mean(np.isfinite(crop)))
    score = (
        3.0 * mad / global_span
        + 2.0 * gradient / global_span
        + 1.5 * contrast / global_span
        + 0.25 * median / global_span
    )
    if finite_fraction < 1.0:
        score += 100.0 * (1.0 - finite_fraction)

    return {
        "region": region.as_dict(),
        "score": float(score),
        "luma_median": median,
        "luma_mad": mad,
        "luma_p10": p10,
        "luma_p90": p90,
        "luma_contrast": contrast,
        "gradient_proxy": gradient,
        "finite_fraction": finite_fraction,
    }


def discover_background_regions(
    source_path: Path,
    count: int = MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    if count < 1 or count > MAX_CANDIDATES:
        raise BackgroundNeutralizationError(
            f"Region count must be from 1 to {MAX_CANDIDATES}."
        )

    with fits.open(source_path, memmap=True) as hdul:
        image = np.asarray(hdul[0].data, dtype=np.float32)

    _, height, width = image.shape
    box_size = int(round(min(height, width) * BOX_FRACTION))
    box_size = max(MIN_BOX_SIZE, min(MAX_BOX_SIZE, box_size))
    box_size = min(box_size, width, height)
    step = max(64, box_size // 2)

    luma_sample = np.mean(
        image[:, ::5, ::5].astype(np.float64, copy=False),
        axis=0,
    )
    finite = luma_sample[np.isfinite(luma_sample)]
    if finite.size < 100:
        raise BackgroundNeutralizationError(
            "Could not compute global image statistics."
        )
    global_span = max(
        float(np.percentile(finite, 99.0))
        - float(np.percentile(finite, 1.0)),
        1.0e-12,
    )

    raw: list[dict[str, Any]] = []
    y_positions = list(range(0, max(1, height - box_size + 1), step))
    x_positions = list(range(0, max(1, width - box_size + 1), step))
    if y_positions[-1] != height - box_size:
        y_positions.append(height - box_size)
    if x_positions[-1] != width - box_size:
        x_positions.append(width - box_size)

    for y in y_positions:
        for x in x_positions:
            centre_x = (x + box_size / 2.0) / width
            centre_y = (y + box_size / 2.0) / height
            in_outer_band = (
                centre_x <= OUTER_BAND_FRACTION
                or centre_x >= 1.0 - OUTER_BAND_FRACTION
                or centre_y <= OUTER_BAND_FRACTION
                or centre_y >= 1.0 - OUTER_BAND_FRACTION
            )
            if not in_outer_band:
                continue

            region = Region(
                x=int(x),
                y=int(y),
                width=int(box_size),
                height=int(box_size),
            )
            metrics = region_metrics(image, region, global_span)
            if metrics["finite_fraction"] < MIN_VALID_REGION_FRACTION:
                continue
            raw.append(metrics)

    raw.sort(key=lambda item: (item["score"], item["region"]["y"], item["region"]["x"]))
    selected: list[dict[str, Any]] = []
    for item in raw:
        region = Region(**item["region"])
        if any(
            region_iou(region, Region(**other["region"]))
            > MAX_REGION_OVERLAP
            for other in selected
        ):
            continue
        selected.append(item)
        if len(selected) == count:
            break

    if len(selected) < count:
        raise BackgroundNeutralizationError(
            f"Only {len(selected)} suitably distinct background regions "
            f"were found; {count} were requested."
        )

    return selected


def parse_region(text: str) -> Region:
    try:
        values = [int(part.strip()) for part in text.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Region must contain integers: x,y,width,height"
        ) from exc
    if len(values) != 4:
        raise argparse.ArgumentTypeError(
            "Region must be x,y,width,height"
        )
    x, y, width, height = values
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        raise argparse.ArgumentTypeError(
            "Region coordinates must be nonnegative and dimensions positive."
        )
    return Region(x=x, y=y, width=width, height=height)


def clipped_region_statistics(
    image: np.ndarray,
    region: Region,
) -> dict[str, Any]:
    _, height, width = image.shape
    if (
        region.x + region.width > width
        or region.y + region.height > height
    ):
        raise BackgroundNeutralizationError(
            f"Region is outside the image: {region}"
        )

    crop = image[
        :,
        region.y : region.y + region.height,
        region.x : region.x + region.width,
    ].astype(np.float64, copy=False)
    luma = np.mean(crop, axis=0)
    finite_luma = luma[np.isfinite(luma)]
    if finite_luma.size < 100:
        raise BackgroundNeutralizationError(
            f"Region contains too few finite pixels: {region}"
        )

    centre = float(np.median(finite_luma))
    mad = robust_mad(finite_luma)
    if not math.isfinite(mad) or mad <= 0.0:
        lower = float(np.percentile(finite_luma, 10.0))
        upper = float(np.percentile(finite_luma, 80.0))
    else:
        lower = centre + LOWER_MAD_LIMIT * mad
        upper = centre + UPPER_MAD_LIMIT * mad

    mask = (
        np.isfinite(luma)
        & (luma >= lower)
        & (luma <= upper)
        & np.all(np.isfinite(crop), axis=0)
    )
    accepted_fraction = float(np.mean(mask))
    if accepted_fraction < MIN_VALID_REGION_FRACTION:
        lower = float(np.percentile(finite_luma, 10.0))
        upper = float(np.percentile(finite_luma, 80.0))
        mask = (
            np.isfinite(luma)
            & (luma >= lower)
            & (luma <= upper)
            & np.all(np.isfinite(crop), axis=0)
        )
        accepted_fraction = float(np.mean(mask))

    if accepted_fraction < MIN_VALID_REGION_FRACTION:
        raise BackgroundNeutralizationError(
            f"Too few accepted background pixels in region {region}: "
            f"{accepted_fraction:.3f}"
        )

    medians = [
        float(np.median(crop[channel][mask]))
        for channel in range(3)
    ]
    mads = [
        robust_mad(crop[channel][mask])
        for channel in range(3)
    ]
    return {
        "region": region.as_dict(),
        "clipping": {
            "lower_mad_limit": LOWER_MAD_LIMIT,
            "upper_mad_limit": UPPER_MAD_LIMIT,
            "effective_lower_luma": lower,
            "effective_upper_luma": upper,
            "accepted_fraction": accepted_fraction,
            "accepted_pixels": int(np.count_nonzero(mask)),
            "total_pixels": int(mask.size),
        },
        "channel_medians": {
            "red": medians[0],
            "green": medians[1],
            "blue": medians[2],
        },
        "channel_mads": {
            "red": mads[0],
            "green": mads[1],
            "blue": mads[2],
        },
        "median_spread": float(max(medians) - min(medians)),
        "_mask": mask,
    }


def write_neutralized_fits(
    source_path: Path,
    output_path: Path,
    region: Region,
) -> dict[str, Any]:
    with fits.open(source_path, memmap=True) as hdul:
        source = np.asarray(hdul[0].data, dtype=np.float32)
        header = hdul[0].header.copy()

    before = clipped_region_statistics(source, region)
    medians = np.array(
        [
            before["channel_medians"]["red"],
            before["channel_medians"]["green"],
            before["channel_medians"]["blue"],
        ],
        dtype=np.float64,
    )
    target = float(np.mean(medians))
    offsets = target - medians

    output = source.astype(np.float64) + offsets[:, None, None]
    output = output.astype(np.float32)

    header["FILTER"] = "mixed_StarlessNeutralized"
    header.add_history(
        "Background neutralization: additive RGB offsets to equalize "
        "robust background medians."
    )
    header.add_history(
        f"Region x={region.x} y={region.y} w={region.width} "
        f"h={region.height}; target={target:.12g}."
    )
    header.add_history(
        "Offsets R={:.12g} G={:.12g} B={:.12g}.".format(
            offsets[0],
            offsets[1],
            offsets[2],
        )
    )
    fits.PrimaryHDU(data=output, header=header).writeto(
        output_path,
        overwrite=False,
        output_verify="fix",
    )

    after = clipped_region_statistics(output, region)
    before.pop("_mask", None)
    after.pop("_mask", None)
    return {
        "method": "additive channel offsets",
        "target_background_median": target,
        "channel_offsets": {
            "red": float(offsets[0]),
            "green": float(offsets[1]),
            "blue": float(offsets[2]),
        },
        "region_statistics_before": before,
        "region_statistics_after": after,
    }


def quality_assessment(
    source_path: Path,
    output_path: Path,
    neutralization: dict[str, Any],
) -> dict[str, Any]:
    source_evidence = inspect_fits(source_path)
    output_evidence = inspect_fits(output_path)

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
        source_evidence.channels != output_evidence.channels
        or source_evidence.width != output_evidence.width
        or source_evidence.height != output_evidence.height
        or source_evidence.bitpix != output_evidence.bitpix
    ):
        fail(
            "format_preservation",
            {
                "source": asdict(source_evidence),
                "output": asdict(output_evidence),
            },
            "channels, dimensions, and BITPIX must be preserved",
        )
    if output_evidence.finite_fraction != 1.0:
        fail(
            "finite_fraction",
            output_evidence.finite_fraction,
            "must equal 1.0",
        )
    if source_evidence.sha256 == output_evidence.sha256:
        fail(
            "output_sha256",
            output_evidence.sha256,
            "must differ from the source",
        )

    with fits.open(source_path, memmap=True) as hdul:
        source = np.asarray(hdul[0].data, dtype=np.float32)
    with fits.open(output_path, memmap=True) as hdul:
        output = np.asarray(hdul[0].data, dtype=np.float32)

    low_clip_fraction = float(np.mean(output <= 0.0))
    high_clip_fraction = float(np.mean(output >= 1.0))

    source_sample = source[:, ::4, ::4].astype(np.float64, copy=False)
    output_sample = output[:, ::4, ::4].astype(np.float64, copy=False)
    valid = np.isfinite(source_sample) & np.isfinite(output_sample)
    delta = output_sample - source_sample

    constant_offset_residuals: dict[str, float] = {}
    names = ("red", "green", "blue")
    for channel, name in enumerate(names):
        values = delta[channel][valid[channel]]
        constant_offset_residuals[name] = float(np.std(values))

    source_luma = np.mean(source_sample, axis=0)
    output_luma = np.mean(output_sample, axis=0)
    valid_luma = np.isfinite(source_luma) & np.isfinite(output_luma)
    source_luma_values = source_luma[valid_luma]
    output_luma_values = output_luma[valid_luma]
    luma_median_shift = float(
        np.median(output_luma_values) - np.median(source_luma_values)
    )

    source_centre = source_luma_values - float(np.mean(source_luma_values))
    output_centre = output_luma_values - float(np.mean(output_luma_values))
    denominator = math.sqrt(
        float(np.dot(source_centre, source_centre))
        * float(np.dot(output_centre, output_centre))
    )
    luma_correlation = (
        float(np.dot(source_centre, output_centre) / denominator)
        if denominator > 0.0
        else math.nan
    )

    before_spread = float(
        neutralization["region_statistics_before"]["median_spread"]
    )
    after_spread = float(
        neutralization["region_statistics_after"]["median_spread"]
    )
    spread_reduction = (
        1.0 - after_spread / before_spread
        if before_spread > 0.0
        else 1.0
    )

    offsets = neutralization["channel_offsets"]
    maximum_absolute_offset = max(abs(float(value)) for value in offsets.values())

    thresholds = {
        "maximum_low_clip_fraction": 1.0e-7,
        "maximum_high_clip_fraction": 1.0e-7,
        "minimum_luma_correlation": 0.999999,
        "maximum_absolute_luma_median_shift": 1.0e-6,
        "minimum_background_spread_reduction": 0.999,
        "maximum_after_background_median_spread": 1.0e-7,
        "maximum_constant_offset_residual": 2.0e-8,
        "maximum_absolute_channel_offset": 0.05,
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
    if (
        abs(luma_median_shift)
        > thresholds["maximum_absolute_luma_median_shift"]
    ):
        fail(
            "luma_median_shift",
            luma_median_shift,
            (
                "absolute value must be <= "
                f"{thresholds['maximum_absolute_luma_median_shift']}"
            ),
        )
    if (
        spread_reduction
        < thresholds["minimum_background_spread_reduction"]
    ):
        fail(
            "background_spread_reduction",
            spread_reduction,
            (
                "must be >= "
                f"{thresholds['minimum_background_spread_reduction']}"
            ),
        )
    if (
        after_spread
        > thresholds["maximum_after_background_median_spread"]
    ):
        fail(
            "after_background_median_spread",
            after_spread,
            (
                "must be <= "
                f"{thresholds['maximum_after_background_median_spread']}"
            ),
        )
    if (
        max(constant_offset_residuals.values())
        > thresholds["maximum_constant_offset_residual"]
    ):
        fail(
            "constant_offset_residual",
            constant_offset_residuals,
            (
                "each channel must be <= "
                f"{thresholds['maximum_constant_offset_residual']}"
            ),
        )
    if (
        maximum_absolute_offset
        > thresholds["maximum_absolute_channel_offset"]
    ):
        fail(
            "maximum_absolute_channel_offset",
            maximum_absolute_offset,
            (
                "must be <= "
                f"{thresholds['maximum_absolute_channel_offset']}"
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
            "luma_correlation": luma_correlation,
            "luma_median_shift": luma_median_shift,
            "background_median_spread_before": before_spread,
            "background_median_spread_after": after_spread,
            "background_spread_reduction": spread_reduction,
            "constant_offset_residuals": constant_offset_residuals,
            "maximum_absolute_channel_offset": maximum_absolute_offset,
            "source_minimum": source_evidence.minimum,
            "source_maximum": source_evidence.maximum,
            "output_minimum": output_evidence.minimum,
            "output_maximum": output_evidence.maximum,
        },
        "thresholds": thresholds,
        "interpretation": (
            "The selected background medians were neutralized by constant "
            "additive RGB offsets while preserving linear luminance and "
            "image structure."
            if satisfactory
            else "The candidate requires review because one or more "
            "neutrality, clipping, linearity, or preservation safeguards "
            "did not pass."
        ),
    }


def candidate_selection_score(
    region_metrics_data: dict[str, Any],
    neutralization: dict[str, Any],
    quality: dict[str, Any],
) -> float:
    offsets = neutralization["channel_offsets"]
    offset_magnitude = math.sqrt(
        sum(float(value) ** 2 for value in offsets.values())
    )
    after_spread = float(
        neutralization["region_statistics_after"]["median_spread"]
    )
    score = (
        float(region_metrics_data["score"])
        + 1000.0 * offset_magnitude
        + 1.0e8 * after_spread
    )
    if not quality["satisfactory"]:
        score += 1000.0
    return float(score)


def execute_candidate(
    *,
    source_path: Path,
    run_root: Path,
    candidate_index: int,
    region_metrics_data: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    candidate_name = f"candidate-{candidate_index:02d}"
    candidate = run_root / candidate_name
    work = candidate / "work"
    logs = candidate / "logs"
    previews = candidate / "previews"
    work.mkdir(parents=True, exist_ok=False)
    logs.mkdir()
    previews.mkdir()

    working_source = work / "SHO-starless-linear-denoised.fit"
    output = (
        work / "SHO-starless-linear-denoised-neutralized.fit"
    )
    shutil.copy2(source_path, working_source)

    region = Region(**region_metrics_data["region"])
    neutralization = write_neutralized_fits(
        working_source,
        output,
        region,
    )
    source_evidence = inspect_fits(working_source)
    output_evidence = inspect_fits(output)
    quality = quality_assessment(
        working_source,
        output,
        neutralization,
    )

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
    after_preview = (
        previews
        / "SHO-starless-linear-denoised-neutralized-linked.png"
    )
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
        raise BackgroundNeutralizationError(
            f"Preview generation failed ({preview_failures}); evidence is "
            f"preserved at {candidate}"
        )

    score = candidate_selection_score(
        region_metrics_data,
        neutralization,
        quality,
    )
    return {
        "candidate": candidate_name,
        "candidate_directory": str(candidate),
        "created_at": utc_now(),
        "helper_version": VERSION,
        "background_region": region.as_dict(),
        "background_region_discovery": region_metrics_data,
        "neutralization": neutralization,
        "source": asdict(source_evidence),
        "output": asdict(output_evidence),
        "quality_assessment": quality,
        "selection_score": score,
        "preview_script": str(preview_script),
        "preview_script_sha256": sha256_file(preview_script),
        "preview_run": preview_run,
        "previews": {
            "before_linked": str(before_preview),
            "after_linked": str(after_preview),
        },
        "status": (
            "satisfactory"
            if quality["satisfactory"]
            else "needs_review"
        ),
    }


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
        key=lambda item: (
            float(item["selection_score"]),
            item["candidate"],
        ),
    )


def run_project(
    *,
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
    fresh_run: bool,
    regions: list[Region] | None,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    if paths["stable"].exists() and not fresh_run:
        raise BackgroundNeutralizationError(
            f"Canonical background-neutralization output already exists: "
            f"{paths['stable']}. Use --fresh-run to create a new candidate "
            "set while preserving the current canonical result."
        )

    siril = siril_version()
    source_manifest, source_evidence = validate_source(paths)

    if regions:
        if len(regions) > MAX_CANDIDATES:
            raise BackgroundNeutralizationError(
                f"At most {MAX_CANDIDATES} explicit regions are permitted."
            )
        with fits.open(paths["source"], memmap=True) as hdul:
            image = np.asarray(hdul[0].data, dtype=np.float32)
        luma = np.mean(image[:, ::5, ::5], axis=0)
        finite = luma[np.isfinite(luma)]
        global_span = max(
            float(np.percentile(finite, 99.0))
            - float(np.percentile(finite, 1.0)),
            1.0e-12,
        )
        region_records = [
            region_metrics(image, region, global_span)
            for region in regions
        ]
        region_source = "explicit"
    else:
        region_records = discover_background_regions(
            paths["source"],
            count=MAX_CANDIDATES,
        )
        region_source = "automatic outer-field search"

    run_started_at = utc_now()
    run_root = paths["runs"] / f"neutralize-{unique_id()}"
    run_root.mkdir(parents=True, exist_ok=False)

    candidates = [
        execute_candidate(
            source_path=paths["source"],
            run_root=run_root,
            candidate_index=index,
            region_metrics_data=record,
            timeout_seconds=timeout_seconds,
        )
        for index, record in enumerate(region_records)
    ]

    recommended = recommended_candidate(candidates)
    satisfactory = [
        candidate["candidate"]
        for candidate in candidates
        if candidate["quality_assessment"]["satisfactory"]
    ]
    status = (
        "awaiting_visual_selection"
        if satisfactory
        else "needs_review"
    )

    record = {
        "schema_version": 1,
        "status": status,
        "created_at": utc_now(),
        "run_started_at": run_started_at,
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "project_name": project_name,
        "run_root": str(run_root),
        "fresh_run_requested": bool(fresh_run),
        "maximum_total_candidates": MAX_CANDIDATES,
        "region_source": region_source,
        "source": asdict(source_evidence),
        "source_manifest": str(paths["source_manifest"]),
        "source_manifest_sha256": sha256_file(paths["source_manifest"]),
        "source_manifest_status": source_manifest.get("status"),
        "siril": siril,
        "candidates": candidates,
        "satisfactory_candidates": satisfactory,
        "recommended_candidate": (
            recommended["candidate"] if recommended is not None else None
        ),
        "recommended_selection_score": (
            recommended["selection_score"]
            if recommended is not None else None
        ),
        "canonical_output_changed": False,
        "visual_selection_required": True,
        "ghs_pass1_processing_permitted": False,
        "message": (
            "Compare all satisfactory linked after-previews and publish one "
            "candidate with visual-selection notes."
            if satisfactory
            else "No candidate passed the technical safeguards."
        ),
    }
    json_dump_atomic(run_root / "run-manifest.json", record)
    return record


def publish_candidate(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
    candidate_name: str,
    visual_notes: str,
    fresh_run: bool,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    run_manifest_path = run_root / "run-manifest.json"
    if not run_manifest_path.is_file():
        raise BackgroundNeutralizationError(
            f"Run manifest does not exist: {run_manifest_path}"
        )

    run_record = json.loads(
        run_manifest_path.read_text(encoding="utf-8")
    )
    if run_record.get("helper_version") != VERSION:
        raise BackgroundNeutralizationError(
            "Run helper version does not match the installed helper."
        )
    if run_record.get("project_name") != project_name:
        raise BackgroundNeutralizationError(
            "Run manifest project does not match the requested project."
        )
    if run_record.get("canonical_output_changed"):
        raise BackgroundNeutralizationError(
            "This run has already been published."
        )
    if not visual_notes.strip():
        raise BackgroundNeutralizationError(
            "Visual-selection notes are required."
        )

    matches = [
        item
        for item in run_record.get("candidates", [])
        if item.get("candidate") == candidate_name
    ]
    if len(matches) != 1:
        raise BackgroundNeutralizationError(
            f"Candidate {candidate_name!r} is not uniquely present."
        )
    candidate = matches[0]
    if not candidate["quality_assessment"]["satisfactory"]:
        raise BackgroundNeutralizationError(
            "An unsatisfactory candidate cannot be published."
        )

    source_manifest, source_evidence = validate_source(paths)
    if source_evidence.sha256 != run_record["source"]["sha256"]:
        raise BackgroundNeutralizationError(
            "Current denoised source no longer matches the candidate run."
        )

    recommended = recommended_candidate(run_record["candidates"])
    existing = paths["stable"].exists()
    if existing and not fresh_run:
        raise BackgroundNeutralizationError(
            f"Canonical directory already exists: {paths['stable']}. "
            "Use --fresh-run for preservation-safe replacement."
        )

    publish_dir = run_root / "publish-staging"
    if publish_dir.exists():
        raise BackgroundNeutralizationError(
            f"Publish staging already exists: {publish_dir}"
        )
    publish_dir.mkdir(parents=True, exist_ok=False)

    candidate_dir = Path(candidate["candidate_directory"])
    source_output = (
        candidate_dir
        / "work"
        / "SHO-starless-linear-denoised-neutralized.fit"
    )
    source_before = (
        candidate_dir
        / "previews"
        / "SHO-starless-linear-denoised-before-linked.png"
    )
    source_after = (
        candidate_dir
        / "previews"
        / "SHO-starless-linear-denoised-neutralized-linked.png"
    )

    staged_output = (
        publish_dir
        / "SHO-starless-linear-denoised-neutralized.fit"
    )
    staged_before = (
        publish_dir
        / "SHO-starless-linear-denoised-before-linked.png"
    )
    staged_after = (
        publish_dir
        / "SHO-starless-linear-denoised-neutralized-linked.png"
    )
    shutil.copy2(source_output, staged_output)
    shutil.copy2(source_before, staged_before)
    shutil.copy2(source_after, staged_after)

    staged_evidence = inspect_fits(staged_output)
    if staged_evidence.sha256 != candidate["output"]["sha256"]:
        raise BackgroundNeutralizationError(
            "Selected candidate checksum changed during staging."
        )
    final_output = asdict(staged_evidence)
    final_output["path"] = str(paths["stable_output"])

    previous = (
        run_root / "previous-processing-background-neutralization"
        if existing
        else None
    )
    if previous is not None and previous.exists():
        raise BackgroundNeutralizationError(
            f"Preservation destination already exists: {previous}"
        )

    manifest = {
        "schema_version": 1,
        "created_at": utc_now(),
        "status": "ready",
        "helper_version": VERSION,
        "project": project_name,
        "project_path": str(paths["project"]),
        "source": asdict(source_evidence),
        "source_linear_denoise_manifest": str(
            paths["source_manifest"]
        ),
        "source_linear_denoise_helper_version": source_manifest.get(
            "helper_version"
        ),
        "method": {
            "operation": "background neutralization",
            "domain": "linear starless RGB",
            "algorithm": (
                "robust selected-background medians equalized with "
                "constant additive channel offsets"
            ),
            "target": "arithmetic mean of selected R, G, and B medians",
            "luminance_preservation": (
                "the three channel offsets sum to zero"
            ),
            "lower_mad_limit": LOWER_MAD_LIMIT,
            "upper_mad_limit": UPPER_MAD_LIMIT,
        },
        "candidate_count": len(run_record["candidates"]),
        "candidates": run_record["candidates"],
        "recommended_candidate": (
            recommended["candidate"] if recommended is not None else None
        ),
        "selected_candidate": candidate_name,
        "selected_candidate_was_recommended": (
            recommended is not None
            and recommended["candidate"] == candidate_name
        ),
        "visual_selection": {
            "required": True,
            "reviewer": "CodeWarrior",
            "notes": visual_notes.strip(),
            "satisfactory_candidates_compared": run_record[
                "satisfactory_candidates"
            ],
        },
        "background_region": candidate["background_region"],
        "background_region_discovery": candidate[
            "background_region_discovery"
        ],
        "neutralization": candidate["neutralization"],
        "quality_assessment": candidate["quality_assessment"],
        "output": final_output,
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
        "previous_processing_background_neutralization_preserved_at": (
            str(previous) if previous is not None else None
        ),
        "publication_method": (
            "generate and preserve region candidates, require visual "
            "selection, validate the chosen candidate, preserve the previous "
            "canonical directory, then atomically rename staging"
        ),
        "siril": siril_version(),
        "visual_review_completed": True,
        "ghs_pass1_processing_permitted": True,
    }
    json_dump_atomic(
        publish_dir / "background-neutralization-manifest.json",
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

    run_record["status"] = "published"
    run_record["published_at"] = utc_now()
    run_record["canonical_output_changed"] = True
    run_record["selected_candidate"] = candidate_name
    run_record["visual_selection_notes"] = visual_notes.strip()
    run_record["ghs_pass1_processing_permitted"] = True
    json_dump_atomic(run_manifest_path, run_record)

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
        "stable_directory": str(paths["stable"]),
        "stable_output": str(paths["stable_output"]),
        "stable_before_preview": str(paths["stable_before_preview"]),
        "stable_after_preview": str(paths["stable_after_preview"]),
        "stable_manifest": str(paths["stable_manifest"]),
        "previous_processing_background_neutralization_preserved_at": (
            manifest[
                "previous_processing_background_neutralization_preserved_at"
            ]
        ),
        "canonical_output_changed": True,
        "visual_review_completed": True,
        "ghs_pass1_processing_permitted": True,
        "manifest": manifest,
    }
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
            "ghs_pass1_processing_permitted": False,
        }

    manifest = json.loads(
        paths["stable_manifest"].read_text(encoding="utf-8")
    )
    errors: list[str] = []
    if not paths["stable_output"].is_file():
        errors.append(f"Missing output: {paths['stable_output']}")
        output = None
    else:
        output = asdict(inspect_fits(paths["stable_output"]))
        expected = manifest.get("output", {}).get("sha256")
        if expected and output["sha256"] != expected:
            errors.append("Output checksum does not match the manifest.")

    for key in ("stable_before_preview", "stable_after_preview"):
        if not paths[key].is_file():
            errors.append(f"Missing preview: {paths[key]}")

    ready = manifest.get("status") == "ready" and not errors
    return {
        "status": "ready" if ready else "invalid",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "stable_directory": str(paths["stable"]),
        "manifest": str(paths["stable_manifest"]),
        "errors": errors,
        "source": manifest.get("source"),
        "selected_candidate": manifest.get("selected_candidate"),
        "recommended_candidate": manifest.get("recommended_candidate"),
        "selected_candidate_was_recommended": manifest.get(
            "selected_candidate_was_recommended"
        ),
        "background_region": manifest.get("background_region"),
        "neutralization": manifest.get("neutralization"),
        "quality_assessment": manifest.get("quality_assessment"),
        "visual_selection": manifest.get("visual_selection"),
        "output": output,
        "previews": manifest.get("previews"),
        "visual_review_completed": manifest.get(
            "visual_review_completed",
            False,
        ),
        "ghs_pass1_processing_permitted": ready,
    }


def write_synthetic_fits(path: Path) -> None:
    rng = np.random.default_rng(15082026)
    height = width = 768
    yy, xx = np.mgrid[0:height, 0:width]

    neutral_background = np.array(
        [0.0090, 0.0048, 0.0031],
        dtype=np.float32,
    )[:, None, None]
    image = np.broadcast_to(
        neutral_background,
        (3, height, width),
    ).copy()

    nebula = np.exp(
        -(
            ((xx - 390.0) / 150.0) ** 2
            + ((yy - 380.0) / 125.0) ** 2
        )
    ).astype(np.float32)
    image[0] += 0.0070 * nebula
    image[1] += 0.0100 * nebula
    image[2] += 0.0035 * nebula

    for cy, cx, amplitude in (
        (100, 120, 0.04),
        (650, 620, 0.05),
        (180, 610, 0.025),
    ):
        star = amplitude * np.exp(
            -(
                (xx - cx) ** 2 + (yy - cy) ** 2
            ) / (2.0 * 2.2**2)
        )
        image += star.astype(np.float32)

    image += rng.normal(
        0.0,
        0.00010,
        image.shape,
    ).astype(np.float32)
    image = np.clip(image, 0.0001, 0.2).astype(np.float32)

    header = fits.Header()
    header["FILTER"] = "mixed_Starless"
    header["OBJECT"] = "Synthetic background-neutralization self-test"
    fits.PrimaryHDU(data=image, header=header).writeto(
        path,
        overwrite=False,
        output_verify="fix",
    )


def self_test(timeout_seconds: int) -> dict[str, Any]:
    siril = siril_version()
    root = (
        WORKSPACE
        / ".skill-self-tests"
        / "siril-background-neutralization"
        / unique_id()
    )
    root.mkdir(parents=True, exist_ok=False)
    source = root / "synthetic-starless-linear-denoised.fit"
    write_synthetic_fits(source)

    region_records = discover_background_regions(
        source,
        count=MAX_CANDIDATES,
    )
    candidates = [
        execute_candidate(
            source_path=source,
            run_root=root,
            candidate_index=index,
            region_metrics_data=record,
            timeout_seconds=timeout_seconds,
        )
        for index, record in enumerate(region_records)
    ]

    failed: list[dict[str, Any]] = []
    if len(candidates) != MAX_CANDIDATES:
        failed.append(
            {
                "metric": "candidate_count",
                "value": len(candidates),
                "required": MAX_CANDIDATES,
            }
        )
    for candidate in candidates:
        if not candidate["quality_assessment"]["satisfactory"]:
            failed.append(
                {
                    "metric": (
                        f"{candidate['candidate']}_quality_assessment"
                    ),
                    "value": candidate["quality_assessment"],
                    "required": "satisfactory",
                }
            )
        if candidate["preview_run"]["exit_status"] != 0:
            failed.append(
                {
                    "metric": (
                        f"{candidate['candidate']}_preview_exit_status"
                    ),
                    "value": candidate["preview_run"]["exit_status"],
                    "required": 0,
                }
            )

    recommended = recommended_candidate(candidates)
    if recommended is None:
        failed.append(
            {
                "metric": "recommended_candidate",
                "value": None,
                "required": "one satisfactory candidate",
            }
        )

    if failed:
        raise BackgroundNeutralizationError(
            f"Background-neutralization self-test failed {failed}; "
            f"evidence is preserved at {root}"
        )

    return {
        "status": "success",
        "created_at": utc_now(),
        "helper_version": VERSION,
        "self_test_directory": str(root),
        "siril": siril,
        "candidate_count": len(candidates),
        "candidates": candidates,
        "recommended_candidate": recommended["candidate"],
        "tests": [
            "three automatically discovered outer-field regions",
            "robust luma rejection within each region",
            "additive RGB-median equalization",
            "zero-sum channel offsets",
            "linear-luminance preservation",
            "32-bit RGB FITS preservation",
            "finite output with no clipping",
            "constant per-channel offset verification",
            "linked Siril before and after previews",
            "all-candidate evidence preservation",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate, compare, and publish robust linear background-"
            "neutralization candidates for the denoised starless SHO image."
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
        "--region",
        action="append",
        type=parse_region,
        help=(
            "Optional explicit x,y,width,height background region. May be "
            "provided up to three times. Without this option, three flat "
            "outer-field regions are discovered automatically."
        ),
    )
    run_parser.add_argument(
        "--fresh-run",
        action="store_true",
        help=(
            "Generate a fresh candidate set. Existing canonical output "
            "remains untouched until publish succeeds."
        ),
    )

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--project", required=True)
    publish_parser.add_argument("--run-root", required=True, type=Path)
    publish_parser.add_argument("--candidate", required=True)
    publish_parser.add_argument("--visual-notes", required=True)
    publish_parser.add_argument("--fresh-run", action="store_true")

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
                regions=args.region,
            )
        elif args.command == "publish":
            payload = publish_candidate(
                workspace=WORKSPACE,
                project_name=args.project,
                run_root=args.run_root.resolve(),
                candidate_name=args.candidate,
                visual_notes=args.visual_notes,
                fresh_run=args.fresh_run,
            )
        elif args.command == "status":
            payload = status_project(
                WORKSPACE,
                args.project,
            )
        else:
            raise BackgroundNeutralizationError(
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
