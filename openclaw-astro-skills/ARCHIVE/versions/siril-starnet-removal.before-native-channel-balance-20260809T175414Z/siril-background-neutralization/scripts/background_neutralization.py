#!/usr/bin/env python3
"""Safety-first linear background neutralization for canonical SHO output."""

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
import zipfile

import numpy as np
from astropy.io import fits


VERSION = "1.1.0"
WORKSPACE = Path("/home/peter/.openclaw/workspace/agents/codewarrior")
SIRIL_APP = Path(
    "/home/peter/.openclaw/runtime/siril-processor/toolchain/.toolchain/"
    "siril/1.4.4/squashfs-root/AppRun"
)
SIRIL_APPDIR = SIRIL_APP.parent
REQUIRED_SIRIL_VERSION = "1.4.4"
REQUIRED_SHO_HELPER_VERSION = "1.1.1"
UPSTREAM_STAGE = "siril-sho-combination"
CURRENT_STAGE = "siril-background-neutralization"
NEXT_STAGE = "siril-starnet-removal"
REVIEW_SCHEMA_VERSION = 1

MAX_REGIONS = 3
MIN_BOX_SIZE = 256
MAX_BOX_SIZE = 512
BOX_FRACTION = 0.14
OUTER_BAND_FRACTION = 0.30
MIN_ACCEPTED_REGION_FRACTION = 0.35
MAX_REGION_OVERLAP = 0.15
LOWER_MAD_LIMIT = -2.8
UPPER_MAD_LIMIT = 2.0
FULL_PREVIEW_DOWNSAMPLE = 3
DETAIL_CROP_SIZE = 700
CONTACT_PREVIEW_MAXDIM = 2600

SEVERE_ARTIFACT_FLAGS = {
    "new_colour_cast",
    "faint_nebula_loss",
    "star_halo_colour_shift",
    "posterization",
    "hard_boundary",
    "missing_area",
    "structure_loss",
    "nonfinite_pixels",
}
ALLOWED_ARTIFACT_FLAGS = SEVERE_ARTIFACT_FLAGS | {
    "none",
    "minimal_change",
    "background_improved",
    "stars_acceptable",
    "detail_preserved",
    "region_may_contain_nebulosity",
}
FATAL_LOG_MARKERS = (
    "script execution failed",
    "command execution failed",
    "cannot load",
    "could not load",
    "not enough memory",
    "segmentation fault",
)


class BackgroundNeutralizationError(RuntimeError):
    """Raised when background neutralization cannot continue safely."""


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
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
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
                f"Expected 3-channel RGB FITS, found {array.shape}: {path}"
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


def read_rgb(path: Path) -> tuple[np.ndarray, fits.Header]:
    with fits.open(path, memmap=True) as hdul:
        array = np.asarray(hdul[0].data, dtype=np.float32)
        header = hdul[0].header.copy()
    if array.ndim != 3 or array.shape[0] != 3:
        raise BackgroundNeutralizationError(
            f"Expected RGB FITS at {path}; found {array.shape}"
        )
    return array, header


def project_paths(workspace: Path, project_name: str) -> dict[str, Path]:
    project = workspace / "Projects" / project_name
    processing = project / "processing"
    stable = processing / "background-neutralization"
    return {
        "project": project,
        "source": processing / "sho" / "SHO-linear.fit",
        "source_manifest": (
            processing / "sho" / "sho-combination-manifest.json"
        ),
        "runs": project / ".siril-background-neutralization",
        "stable": stable,
        "stable_output": stable / "SHO-linear-neutralized.fit",
        "stable_before_preview": stable / "SHO-linear-before-linked.png",
        "stable_after_preview": stable / "SHO-linear-neutralized-linked.png",
        "stable_full_contact": stable / "full-contact.png",
        "stable_regions_contact": stable / "background-regions-contact.png",
        "stable_detail_contact": stable / "detail-contact.png",
        "stable_review": stable / "visual-review-record.json",
        "stable_manifest": stable / "background-neutralization-manifest.json",
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
    if (
        completed.returncode != 0
        or REQUIRED_SIRIL_VERSION not in combined
    ):
        raise BackgroundNeutralizationError(
            f"Expected Siril {REQUIRED_SIRIL_VERSION}; received: {combined}"
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
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = completed.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        returncode = 124
        timed_out = True

    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    stdout_log.write_text(stdout, encoding="utf-8")
    stderr_log.write_text(stderr, encoding="utf-8")
    combined_lower = f"{stdout}\n{stderr}".lower()
    return {
        "command": command,
        "display_command": (
            f'env APPDIR="{SIRIL_APPDIR}" '
            + " ".join(f'"{part}"' for part in command)
        ),
        "exit_status": int(returncode),
        "duration_seconds": round(time.monotonic() - started, 3),
        "timed_out": timed_out,
        "timeout_seconds": int(timeout_seconds),
        "fatal_log_markers": [
            marker
            for marker in FATAL_LOG_MARKERS
            if marker in combined_lower
        ],
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
    }


def validate_source(
    paths: dict[str, Path],
) -> tuple[dict[str, Any], str, FitsEvidence]:
    if not paths["project"].is_dir():
        raise BackgroundNeutralizationError(
            f"Project does not exist: {paths['project']}"
        )
    if not paths["source_manifest"].is_file():
        raise BackgroundNeutralizationError(
            f"SHO-combination manifest is missing: "
            f"{paths['source_manifest']}"
        )
    manifest_hash = sha256_file(paths["source_manifest"])
    manifest = json.loads(
        paths["source_manifest"].read_text(encoding="utf-8")
    )
    if manifest.get("project") != paths["project"].name:
        raise BackgroundNeutralizationError(
            "SHO-combination manifest project does not match."
        )
    if Path(str(manifest.get("project_path", ""))).resolve() != paths[
        "project"
    ].resolve():
        raise BackgroundNeutralizationError(
            "SHO-combination manifest project path does not match."
        )
    if manifest.get("status") != "ready":
        raise BackgroundNeutralizationError(
            "SHO-combination manifest status is not ready."
        )
    if manifest.get("helper_version") != REQUIRED_SHO_HELPER_VERSION:
        raise BackgroundNeutralizationError(
            f"Expected SHO helper {REQUIRED_SHO_HELPER_VERSION}; "
            f"manifest reports {manifest.get('helper_version')!r}."
        )
    if (
        manifest.get("background_neutralization_permitted")
        is not True
    ):
        raise BackgroundNeutralizationError(
            "SHO manifest does not permit background neutralization."
        )
    if manifest.get("star_removal_permitted") is not False:
        raise BackgroundNeutralizationError(
            "SHO manifest incorrectly permits StarNet directly."
        )
    stage_order = manifest.get("stage_order", {})
    if stage_order != {
        "upstream": "siril-mono-linear-denoise",
        "current": UPSTREAM_STAGE,
        "downstream": CURRENT_STAGE,
    }:
        raise BackgroundNeutralizationError(
            "SHO manifest stage order does not match the current pipeline."
        )

    output = manifest.get("output", {})
    recorded_path = Path(str(output.get("path", ""))).resolve()
    if recorded_path != paths["source"].resolve():
        raise BackgroundNeutralizationError(
            "SHO manifest does not reference the canonical SHO-linear FITS."
        )
    evidence = inspect_fits(paths["source"])
    if evidence.bitpix != -32 or evidence.finite_fraction != 1.0:
        raise BackgroundNeutralizationError(
            "SHO input must be finite BITPIX -32 RGB data."
        )
    if evidence.sha256 != output.get("sha256"):
        raise BackgroundNeutralizationError(
            "SHO input checksum does not match its manifest."
        )
    return manifest, manifest_hash, evidence


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
        + 0.25 * max(median, 0.0) / global_span
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
    count: int = MAX_REGIONS,
) -> list[dict[str, Any]]:
    image, _ = read_rgb(source_path)
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
    global_span = max(
        float(np.percentile(finite, 99.0))
        - float(np.percentile(finite, 1.0)),
        1.0e-12,
    )

    y_positions = list(range(0, max(1, height - box_size + 1), step))
    x_positions = list(range(0, max(1, width - box_size + 1), step))
    if y_positions[-1] != height - box_size:
        y_positions.append(height - box_size)
    if x_positions[-1] != width - box_size:
        x_positions.append(width - box_size)

    raw: list[dict[str, Any]] = []
    for y in y_positions:
        for x in x_positions:
            centre_x = (x + box_size / 2.0) / width
            centre_y = (y + box_size / 2.0) / height
            if not (
                centre_x <= OUTER_BAND_FRACTION
                or centre_x >= 1.0 - OUTER_BAND_FRACTION
                or centre_y <= OUTER_BAND_FRACTION
                or centre_y >= 1.0 - OUTER_BAND_FRACTION
            ):
                continue
            region = Region(x, y, box_size, box_size)
            item = region_metrics(image, region, global_span)
            if item["finite_fraction"] == 1.0:
                raw.append(item)

    raw.sort(
        key=lambda item: (
            item["score"],
            item["region"]["y"],
            item["region"]["x"],
        )
    )
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
    if len(selected) != count:
        raise BackgroundNeutralizationError(
            f"Only {len(selected)} distinct outer-field regions were found."
        )
    return selected


def parse_region(text: str) -> Region:
    try:
        values = [int(part.strip()) for part in text.split(",")]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "Region must be x,y,width,height"
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
    return Region(x, y, width, height)


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
    centre = float(np.median(finite_luma))
    mad = robust_mad(finite_luma)
    if not math.isfinite(mad) or mad <= 0.0:
        lower = float(np.percentile(finite_luma, 10.0))
        upper = float(np.percentile(finite_luma, 75.0))
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
    if accepted_fraction < MIN_ACCEPTED_REGION_FRACTION:
        lower = float(np.percentile(finite_luma, 10.0))
        upper = float(np.percentile(finite_luma, 75.0))
        mask = (
            np.isfinite(luma)
            & (luma >= lower)
            & (luma <= upper)
            & np.all(np.isfinite(crop), axis=0)
        )
        accepted_fraction = float(np.mean(mask))
    if accepted_fraction < MIN_ACCEPTED_REGION_FRACTION:
        raise BackgroundNeutralizationError(
            f"Too few accepted background pixels: {accepted_fraction:.3f}"
        )

    medians = np.array(
        [
            float(np.median(crop[channel][mask]))
            for channel in range(3)
        ],
        dtype=np.float64,
    )
    mads = np.array(
        [
            robust_mad(crop[channel][mask])
            for channel in range(3)
        ],
        dtype=np.float64,
    )
    return {
        "region": region.as_dict(),
        "clipping": {
            "lower_mad_limit": LOWER_MAD_LIMIT,
            "upper_mad_limit": UPPER_MAD_LIMIT,
            "effective_lower_luma": lower,
            "effective_upper_luma": upper,
            "accepted_fraction": accepted_fraction,
            "rejected_fraction": 1.0 - accepted_fraction,
            "accepted_pixels": int(np.count_nonzero(mask)),
            "total_pixels": int(mask.size),
        },
        "channel_medians": {
            "red": float(medians[0]),
            "green": float(medians[1]),
            "blue": float(medians[2]),
        },
        "channel_mads": {
            "red": float(mads[0]),
            "green": float(mads[1]),
            "blue": float(mads[2]),
        },
        "median_spread": float(np.max(medians) - np.min(medians)),
        "median_spread_significance": float(
            (np.max(medians) - np.min(medians))
            / max(float(np.mean(mads)), 1.0e-12)
        ),
    }


def write_neutralized_fits(
    source_path: Path,
    output_path: Path,
    region: Region,
) -> dict[str, Any]:
    source, header = read_rgb(source_path)
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
    output = (
        source.astype(np.float64)
        + offsets[:, None, None]
    ).astype(np.float32)

    header["FILTER"] = "mixed_Neutralized"
    header.add_history(
        "Linear background neutralization by zero-sum additive RGB offsets."
    )
    header.add_history(
        f"Region x={region.x} y={region.y} w={region.width} "
        f"h={region.height}; target={target:.12g}."
    )
    header.add_history(
        "Offsets R={:.12g} G={:.12g} B={:.12g}.".format(*offsets)
    )
    fits.PrimaryHDU(data=output, header=header).writeto(
        output_path,
        overwrite=False,
        output_verify="fix",
    )
    after = clipped_region_statistics(output, region)
    return {
        "method": "zero-sum additive channel offsets",
        "target_background_median": target,
        "channel_offsets": {
            "red": float(offsets[0]),
            "green": float(offsets[1]),
            "blue": float(offsets[2]),
        },
        "offset_sum": float(np.sum(offsets)),
        "region_statistics_before": before,
        "region_statistics_after": after,
    }


def correlation(a: np.ndarray, b: np.ndarray) -> float:
    valid = np.isfinite(a) & np.isfinite(b)
    av = a[valid].astype(np.float64, copy=False)
    bv = b[valid].astype(np.float64, copy=False)
    if av.size < 100:
        return math.nan
    av -= float(np.mean(av))
    bv -= float(np.mean(bv))
    denominator = math.sqrt(
        float(np.dot(av, av)) * float(np.dot(bv, bv))
    )
    return (
        float(np.dot(av, bv) / denominator)
        if denominator > 0.0
        else math.nan
    )


def quality_assessment(
    source_path: Path,
    output_path: Path,
    neutralization: dict[str, Any] | None,
    *,
    pass_through: bool,
) -> dict[str, Any]:
    source_evidence = inspect_fits(source_path)
    output_evidence = inspect_fits(output_path)
    source, _ = read_rgb(source_path)
    output, _ = read_rgb(output_path)
    failed: list[dict[str, Any]] = []

    def fail(metric: str, value: Any, requirement: str) -> None:
        failed.append(
            {"metric": metric, "value": value, "requirement": requirement}
        )

    if (
        source.shape != output.shape
        or source_evidence.bitpix != output_evidence.bitpix
    ):
        fail(
            "format_preservation",
            {"source": list(source.shape), "output": list(output.shape)},
            "shape and BITPIX must match",
        )
    if output_evidence.finite_fraction != 1.0:
        fail(
            "finite_fraction",
            output_evidence.finite_fraction,
            "must equal 1.0",
        )

    sample_source = source[:, ::4, ::4].astype(np.float64, copy=False)
    sample_output = output[:, ::4, ::4].astype(np.float64, copy=False)
    delta = sample_output - sample_source
    residual_std = {
        name: float(np.std(delta[index]))
        for index, name in enumerate(("red", "green", "blue"))
    }
    source_luma = np.mean(sample_source, axis=0)
    output_luma = np.mean(sample_output, axis=0)
    luma_correlation = correlation(source_luma, output_luma)
    luma_median_shift = float(
        np.median(output_luma) - np.median(source_luma)
    )
    below_zero_fraction = float(np.mean(output < 0.0))
    above_one_fraction = float(np.mean(output > 1.0))

    if pass_through:
        if source_evidence.sha256 != output_evidence.sha256:
            fail(
                "pass_through_sha256",
                output_evidence.sha256,
                "must exactly match source",
            )
        before_spread = after_spread = spread_reduction = 0.0
        offsets = {"red": 0.0, "green": 0.0, "blue": 0.0}
        offset_sum = 0.0
        maximum_absolute_offset = 0.0
    else:
        if source_evidence.sha256 == output_evidence.sha256:
            fail(
                "output_sha256",
                output_evidence.sha256,
                "must differ from source",
            )
        if neutralization is None:
            raise BackgroundNeutralizationError(
                "Neutralization record is missing."
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
        offset_sum = float(neutralization["offset_sum"])
        maximum_absolute_offset = max(
            abs(float(value)) for value in offsets.values()
        )

        if after_spread > max(2.0e-7, before_spread * 0.002):
            fail(
                "background_median_spread_after",
                after_spread,
                "must be near zero",
            )
        if spread_reduction < 0.995:
            fail(
                "background_spread_reduction",
                spread_reduction,
                "must be at least 0.995",
            )
        if abs(offset_sum) > 1.0e-10:
            fail(
                "offset_sum",
                offset_sum,
                "must be effectively zero",
            )
        if maximum_absolute_offset > 0.05:
            fail(
                "maximum_absolute_offset",
                maximum_absolute_offset,
                "must be <= 0.05",
            )
        if max(residual_std.values()) > 2.0e-7:
            fail(
                "constant_offset_residual",
                residual_std,
                "each channel difference must be constant",
            )
        if not math.isfinite(luma_correlation) or luma_correlation < 0.999999:
            fail(
                "luma_correlation",
                luma_correlation,
                "must be >= 0.999999",
            )
        if abs(luma_median_shift) > 2.0e-7:
            fail(
                "luma_median_shift",
                luma_median_shift,
                "must be near zero",
            )

    return {
        "status": "satisfactory" if not failed else "rejected",
        "satisfactory": not failed,
        "pass_through": pass_through,
        "failed_checks": failed,
        "metrics": {
            "background_median_spread_before": before_spread,
            "background_median_spread_after": after_spread,
            "background_spread_reduction": spread_reduction,
            "channel_offsets": offsets,
            "offset_sum": offset_sum,
            "maximum_absolute_offset": maximum_absolute_offset,
            "constant_offset_residual_std": residual_std,
            "luma_correlation": luma_correlation,
            "luma_median_shift": luma_median_shift,
            "below_zero_fraction_diagnostic": below_zero_fraction,
            "above_one_fraction_diagnostic": above_one_fraction,
        },
        "linear_range_note": (
            "Negative or greater-than-one values are valid in an unclipped "
            "linear floating-point FITS. They are diagnostics, not failures."
        ),
    }


def individual_preview_script() -> str:
    return "\n".join(
        (
            f"requires {REQUIRED_SIRIL_VERSION}",
            'load "SHO-linear.fit"',
            "autostretch -linked",
            'savepng "../previews/SHO-linear-before-linked"',
            "close",
            'load "SHO-linear-neutralized.fit"',
            "autostretch -linked",
            'savepng "../previews/SHO-linear-neutralized-linked"',
            "close",
            "",
        )
    )


def execute_candidate(
    *,
    source_path: Path,
    run_root: Path,
    candidate_name: str,
    region_record: dict[str, Any] | None,
    timeout_seconds: int,
) -> dict[str, Any]:
    candidate_dir = run_root / candidate_name
    work = candidate_dir / "work"
    logs = candidate_dir / "logs"
    previews = candidate_dir / "previews"
    for directory in (work, logs, previews):
        directory.mkdir(parents=True, exist_ok=False)

    working_source = work / "SHO-linear.fit"
    output = work / "SHO-linear-neutralized.fit"
    shutil.copy2(source_path, working_source)

    pass_through = candidate_name == "candidate-00"
    if pass_through:
        shutil.copy2(working_source, output)
        neutralization = None
        region = None
    else:
        if region_record is None:
            raise BackgroundNeutralizationError(
                f"{candidate_name} requires a region."
            )
        region = Region(**region_record["region"])
        neutralization = write_neutralized_fits(
            working_source,
            output,
            region,
        )

    quality = quality_assessment(
        working_source,
        output,
        neutralization,
        pass_through=pass_through,
    )
    script = candidate_dir / "previews.ssf"
    script.write_text(individual_preview_script(), encoding="utf-8")
    preview_run = run_siril_script(
        directory=work,
        script=script,
        stdout_log=logs / "preview-stdout.log",
        stderr_log=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 1200),
    )
    before_preview = previews / "SHO-linear-before-linked.png"
    after_preview = previews / "SHO-linear-neutralized-linked.png"
    if (
        preview_run["exit_status"] != 0
        or preview_run["timed_out"]
        or preview_run["fatal_log_markers"]
        or not before_preview.is_file()
        or not after_preview.is_file()
    ):
        raise BackgroundNeutralizationError(
            f"Preview generation failed for {candidate_name}; "
            f"evidence preserved at {candidate_dir}"
        )

    if pass_through:
        selection_score = 0.0
        imbalance_significance = 0.0
    else:
        before = neutralization["region_statistics_before"]
        imbalance_significance = float(
            before["median_spread_significance"]
        )
        selection_score = float(
            region_record["score"]
            + 2.0
            * quality["metrics"]["maximum_absolute_offset"]
            + 0.25
            / max(imbalance_significance, 1.0e-6)
        )

    return {
        "candidate": candidate_name,
        "candidate_directory": str(candidate_dir),
        "created_at": utc_now(),
        "helper_version": VERSION,
        "operation": (
            "pass-through"
            if pass_through
            else "zero-sum additive RGB background equalization"
        ),
        "background_region": (
            region.as_dict() if region is not None else None
        ),
        "background_region_discovery": region_record,
        "neutralization": neutralization,
        "source": asdict(inspect_fits(working_source)),
        "output": asdict(inspect_fits(output)),
        "quality_assessment": quality,
        "background_imbalance_significance": imbalance_significance,
        "selection_score": selection_score,
        "preview_script": str(script),
        "preview_script_sha256": sha256_file(script),
        "preview_run": preview_run,
        "before_preview": {
            "path": str(before_preview),
            "sha256": sha256_file(before_preview),
        },
        "after_preview": {
            "path": str(after_preview),
            "sha256": sha256_file(after_preview),
        },
        "status": quality["status"],
    }


def recommended_candidate(
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    nonpass = [
        item
        for item in candidates
        if (
            item["candidate"] != "candidate-00"
            and item["quality_assessment"]["satisfactory"]
        )
    ]
    if not nonpass:
        return candidates[0]
    best = min(
        nonpass,
        key=lambda item: (
            item["selection_score"],
            item["candidate"],
        ),
    )
    if best["background_imbalance_significance"] < 3.0:
        return candidates[0]
    return best


def choose_detail_region(source: np.ndarray) -> Region:
    _, height, width = source.shape
    size = min(DETAIL_CROP_SIZE, height, width)
    stride = max(size // 2, 192)
    luma = np.mean(source.astype(np.float64), axis=0)
    candidates: list[tuple[float, int, int]] = []
    for y in range(0, max(1, height - size + 1), stride):
        for x in range(0, max(1, width - size + 1), stride):
            tile = luma[y : y + size : 4, x : x + size : 4]
            finite = tile[np.isfinite(tile)]
            if finite.size < 100:
                continue
            score = float(
                np.percentile(finite, 90.0)
                + 0.5
                * (
                    np.percentile(finite, 90.0)
                    - np.percentile(finite, 10.0)
                )
            )
            candidates.append((score, x, y))
    if not candidates:
        return Region(
            max(0, (width - size) // 2),
            max(0, (height - size) // 2),
            size,
            size,
        )
    _, x, y = max(candidates)
    return Region(x, y, size, size)


def separator_for(
    arrays: list[np.ndarray],
    height: int,
    width: int,
) -> np.ndarray:
    finite = arrays[0][np.isfinite(arrays[0])]
    value = float(np.median(finite)) if finite.size else 0.0
    return np.full((3, height, width), value, dtype=np.float32)


def horizontal_contact(
    arrays: list[np.ndarray],
    *,
    separator_width: int = 10,
) -> np.ndarray:
    height = arrays[0].shape[1]
    separator = separator_for(
        arrays,
        height,
        separator_width,
    )
    pieces: list[np.ndarray] = []
    for index, array in enumerate(arrays):
        if index:
            pieces.append(separator)
        pieces.append(array.astype(np.float32, copy=False))
    return np.concatenate(pieces, axis=2)


def create_contact_previews(
    *,
    source_path: Path,
    candidates: list[dict[str, Any]],
    review_root: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    review_root.mkdir(parents=True, exist_ok=False)
    logs = review_root / "logs"
    logs.mkdir()
    source, source_header = read_rgb(source_path)
    outputs = [
        read_rgb(
            Path(item["candidate_directory"])
            / "work"
            / "SHO-linear-neutralized.fit"
        )[0]
        for item in candidates
    ]

    full_arrays = [
        array[:, ::FULL_PREVIEW_DOWNSAMPLE, ::FULL_PREVIEW_DOWNSAMPLE]
        for array in outputs
    ]
    full_contact = horizontal_contact(full_arrays)

    detail_region = choose_detail_region(source)
    detail_arrays = [
        array[
            :,
            detail_region.y : detail_region.y + detail_region.height,
            detail_region.x : detail_region.x + detail_region.width,
        ]
        for array in outputs
    ]
    detail_contact = horizontal_contact(detail_arrays)

    region_arrays: list[np.ndarray] = []
    region_order: list[str] = []
    for item in candidates[1:]:
        region = Region(**item["background_region"])
        source_crop = source[
            :,
            region.y : region.y + region.height,
            region.x : region.x + region.width,
        ]
        output = read_rgb(
            Path(item["candidate_directory"])
            / "work"
            / "SHO-linear-neutralized.fit"
        )[0]
        output_crop = output[
            :,
            region.y : region.y + region.height,
            region.x : region.x + region.width,
        ]
        region_arrays.extend((source_crop, output_crop))
        region_order.extend(
            (
                f"{item['candidate']}-source-region",
                f"{item['candidate']}-neutralized-region",
            )
        )
    dimensions = {
        (array.shape[1], array.shape[2]) for array in region_arrays
    }
    if len(dimensions) != 1:
        raise BackgroundNeutralizationError(
            "All candidate regions must have matching dimensions."
        )
    regions_contact = horizontal_contact(region_arrays)

    contacts = {
        "full": full_contact,
        "background_regions": regions_contact,
        "detail": detail_contact,
    }
    contact_fits: dict[str, Path] = {}
    contact_pngs: dict[str, Path] = {}
    for kind, array in contacts.items():
        path = review_root / f"{kind}-contact.fit"
        header = source_header.copy()
        header["FILTER"] = "mixed_BackgroundNeutralizationReview"
        fits.PrimaryHDU(data=array, header=header).writeto(
            path,
            overwrite=False,
            output_verify="fix",
        )
        contact_fits[kind] = path
        contact_pngs[kind] = review_root / f"{kind}-contact.png"

    script = review_root / "contact-previews.ssf"
    lines = [f"requires {REQUIRED_SIRIL_VERSION}"]
    for kind in ("full", "background_regions", "detail"):
        lines.extend(
            (
                f'load "{kind}-contact.fit"',
                "autostretch -linked",
                (
                    f"resample -maxdim={CONTACT_PREVIEW_MAXDIM} "
                    "-interp=area"
                ),
                f'savepng "{kind}-contact"',
                "close",
            )
        )
    lines.append("")
    script.write_text("\n".join(lines), encoding="utf-8")
    run = run_siril_script(
        directory=review_root,
        script=script,
        stdout_log=logs / "stdout.log",
        stderr_log=logs / "stderr.log",
        timeout_seconds=min(timeout_seconds, 1200),
    )
    missing = [
        str(path)
        for path in contact_pngs.values()
        if not path.is_file()
    ]
    if (
        run["exit_status"] != 0
        or run["timed_out"]
        or run["fatal_log_markers"]
        or missing
    ):
        raise BackgroundNeutralizationError(
            f"Contact-preview generation failed; missing={missing}"
        )
    return {
        "candidate_panel_order": [
            item["candidate"] for item in candidates
        ],
        "background_region_panel_order": region_order,
        "detail_region": detail_region.as_dict(),
        "contact_fits": {
            kind: {
                "path": str(path),
                "sha256": sha256_file(path),
            }
            for kind, path in contact_fits.items()
        },
        "contact_previews": {
            kind: {
                "path": str(path),
                "sha256": sha256_file(path),
            }
            for kind, path in contact_pngs.items()
        },
        "preview_script": str(script),
        "preview_script_sha256": sha256_file(script),
        "preview_run": run,
    }


def compact_candidate(item: dict[str, Any]) -> dict[str, Any]:
    quality = item["quality_assessment"]
    return {
        "candidate": item["candidate"],
        "operation": item["operation"],
        "status": item["status"],
        "background_region": item["background_region"],
        "region_score": (
            item["background_region_discovery"].get("score")
            if item["background_region_discovery"]
            else None
        ),
        "background_imbalance_significance": item[
            "background_imbalance_significance"
        ],
        "selection_score": item["selection_score"],
        "channel_offsets": quality["metrics"]["channel_offsets"],
        "background_median_spread_before": quality["metrics"][
            "background_median_spread_before"
        ],
        "background_median_spread_after": quality["metrics"][
            "background_median_spread_after"
        ],
        "luma_correlation": quality["metrics"]["luma_correlation"],
        "output_sha256": item["output"]["sha256"],
        "after_preview": item["after_preview"],
    }


def build_review_template(
    *,
    project_name: str,
    run_root: Path,
    candidates: list[dict[str, Any]],
    contacts: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "project": project_name,
        "run_root": str(run_root),
        "reviewer": "CodeWarrior",
        "reviewed_at": "",
        "instructions": (
            "Open all three contact PNGs with an image-capable tool. "
            "Candidate-00 is pass-through. Select a correction only when "
            "the background is clearly more neutral without harming faint "
            "nebula, stars, or structure."
        ),
        "previews": {
            kind: {
                **record,
                "inspected": False,
            }
            for kind, record in contacts["contact_previews"].items()
        },
        "candidate_panel_order": contacts["candidate_panel_order"],
        "background_region_panel_order": contacts[
            "background_region_panel_order"
        ],
        "candidates": [
            {
                "candidate": item["candidate"],
                "technical_status": item["status"],
                "accepted": item["candidate"] == "candidate-00",
                "artifact_flags": ["none"] if item["candidate"] == "candidate-00" else [],
                "background_naturalness": (
                    "same" if item["candidate"] == "candidate-00" else ""
                ),
                "faint_nebula_preservation": (
                    "same" if item["candidate"] == "candidate-00" else ""
                ),
                "star_and_halo_impact": (
                    "same" if item["candidate"] == "candidate-00" else ""
                ),
                "observations": "",
            }
            for item in candidates
        ],
        "selected_candidate": "candidate-00",
        "comparison_outcome": "pass_through_or_undecided",
        "selection_rationale": "",
    }


def write_review_files(
    *,
    run_record: dict[str, Any],
    review_root: Path,
) -> dict[str, str]:
    summary = review_root / "decision-summary.json"
    json_dump_atomic(
        summary,
        {
            "schema_version": 2,
            "helper_version": VERSION,
            "project": run_record["project_name"],
            "run_root": run_record["run_root"],
            "status": run_record["status"],
            "recommended_candidate": run_record[
                "recommended_candidate"
            ],
            "satisfactory_candidates": run_record[
                "satisfactory_candidates"
            ],
            "candidate_summaries": run_record[
                "candidate_summaries"
            ],
            "review_evidence": run_record["review_evidence"],
        },
    )
    brief_lines = [
        "# Background-neutralization decision brief",
        "",
        f"Project: `{run_record['project_name']}`  ",
        f"Run root: `{run_record['run_root']}`  ",
        f"Helper: `{VERSION}`",
        "",
        "Open all three contact PNGs:",
        "",
        "- full-contact.png: candidate-00 through candidate-03",
        "- background_regions-contact.png: source/output pairs for each region",
        "- detail-contact.png: candidate-00 through candidate-03",
        "",
        "Candidate-00 is exact pass-through. Negative linear FITS values are",
        "valid and are not evidence of clipping.",
        "",
        f"Numerical recommendation: `{run_record['recommended_candidate']}`",
        "",
        "| Candidate | Status | Region | Imbalance significance | "
        "Max offset | Luma correlation | Score |",
        "|---|---|---|---:|---:|---:|---:|",
    ]
    for item in run_record["candidates"]:
        metrics = item["quality_assessment"]["metrics"]
        brief_lines.append(
            "| {candidate} | {status} | {region} | {significance:.6g} | "
            "{offset:.6g} | {corr:.9g} | {score:.6g} |".format(
                candidate=item["candidate"],
                status=item["status"],
                region=item["background_region"],
                significance=item["background_imbalance_significance"],
                offset=metrics["maximum_absolute_offset"],
                corr=metrics["luma_correlation"],
                score=item["selection_score"],
            )
        )
    brief_lines.extend(
        (
            "",
            "Select a neutralized candidate only when empty sky improves",
            "without introducing a new cast, suppressing faint nebulosity,",
            "or changing star halos objectionably. Otherwise select",
            "candidate-00.",
            "",
            "Do not ask Peter or ChatGPT to choose.",
        )
    )
    brief = review_root / "decision-brief.md"
    brief.write_text("\n".join(brief_lines) + "\n", encoding="utf-8")

    template = review_root / "visual-review-template.json"
    json_dump_atomic(
        template,
        build_review_template(
            project_name=run_record["project_name"],
            run_root=Path(run_record["run_root"]),
            candidates=run_record["candidates"],
            contacts=run_record["review_evidence"],
        ),
    )
    archive = review_root / "background-neutralization-review.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in (summary, brief, template):
            bundle.write(path, path.name)
        for record in run_record["review_evidence"][
            "contact_previews"
        ].values():
            path = Path(record["path"])
            bundle.write(path, path.name)
    return {
        "decision_summary": str(summary),
        "decision_brief": str(brief),
        "visual_review_template": str(template),
        "review_archive": str(archive),
        "review_archive_sha256": sha256_file(archive),
    }


def canonical_state(paths: dict[str, Path]) -> dict[str, Any]:
    if not paths["stable_manifest"].is_file():
        return {
            "status": "missing",
            "star_removal_permitted": False,
        }
    try:
        manifest = json.loads(
            paths["stable_manifest"].read_text(encoding="utf-8")
        )
    except Exception as exc:
        return {
            "status": "invalid",
            "error": str(exc),
            "star_removal_permitted": False,
        }
    if (
        manifest.get("helper_version") != VERSION
        or manifest.get("upstream_stage") != UPSTREAM_STAGE
    ):
        return {
            "status": "obsolete",
            "manifest_helper_version": manifest.get("helper_version"),
            "required_helper_version": VERSION,
            "upstream_stage": manifest.get("upstream_stage"),
            "reason": (
                "Existing background-neutralization output belongs to the "
                "earlier StarNet/linear-denoise-first pipeline."
            ),
            "star_removal_permitted": False,
        }
    return {
        "status": manifest.get("status", "invalid"),
        "manifest_helper_version": manifest.get("helper_version"),
        "star_removal_permitted": bool(
            manifest.get("star_removal_permitted")
        ),
    }


def run_project(
    *,
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
    fresh_run: bool,
    regions: list[Region] | None,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    existing_state = canonical_state(paths)
    if paths["stable"].exists() and not fresh_run:
        raise BackgroundNeutralizationError(
            f"Canonical directory exists: {paths['stable']}. "
            "Use --fresh-run to preserve it while creating candidates."
        )
    siril = siril_version()
    source_manifest, source_manifest_hash, source = validate_source(paths)

    if regions:
        if len(regions) != MAX_REGIONS:
            raise BackgroundNeutralizationError(
                f"Exactly {MAX_REGIONS} explicit regions are required."
            )
        if len({(r.width, r.height) for r in regions}) != 1:
            raise BackgroundNeutralizationError(
                "Explicit regions must have matching dimensions."
            )
        image, _ = read_rgb(paths["source"])
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
            MAX_REGIONS,
        )
        region_source = "automatic star-robust outer-field search"

    run_root = paths["runs"] / f"neutralize-{unique_id()}"
    run_root.mkdir(parents=True, exist_ok=False)

    candidates = [
        execute_candidate(
            source_path=paths["source"],
            run_root=run_root,
            candidate_name="candidate-00",
            region_record=None,
            timeout_seconds=timeout_seconds,
        )
    ]
    for index, record in enumerate(region_records, start=1):
        candidates.append(
            execute_candidate(
                source_path=paths["source"],
                run_root=run_root,
                candidate_name=f"candidate-{index:02d}",
                region_record=record,
                timeout_seconds=timeout_seconds,
            )
        )

    review_root = run_root / "compact-review"
    contacts = create_contact_previews(
        source_path=paths["source"],
        candidates=candidates,
        review_root=review_root,
        timeout_seconds=timeout_seconds,
    )
    recommended = recommended_candidate(candidates)
    satisfactory = [
        item["candidate"]
        for item in candidates
        if item["quality_assessment"]["satisfactory"]
    ]
    status = (
        "awaiting_visual_selection"
        if satisfactory
        else "blocked"
    )
    record = {
        "schema_version": 2,
        "status": status,
        "created_at": utc_now(),
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "project_name": project_name,
        "run_root": str(run_root),
        "fresh_run_requested": fresh_run,
        "existing_canonical_state_at_start": existing_state,
        "upstream_stage": UPSTREAM_STAGE,
        "source": asdict(source),
        "source_manifest": str(paths["source_manifest"]),
        "source_manifest_sha256": source_manifest_hash,
        "source_manifest_helper_version": source_manifest.get(
            "helper_version"
        ),
        "region_source": region_source,
        "candidates": candidates,
        "candidate_summaries": [
            compact_candidate(item) for item in candidates
        ],
        "satisfactory_candidates": satisfactory,
        "recommended_candidate": recommended["candidate"],
        "review_evidence": contacts,
        "siril": siril,
        "canonical_output_changed": False,
        "visual_review_recorded": False,
        "star_removal_permitted": False,
    }
    run_manifest = run_root / "run-manifest.json"
    json_dump_atomic(run_manifest, record)
    files = write_review_files(
        run_record=record,
        review_root=review_root,
    )
    record["review_files"] = files
    json_dump_atomic(run_manifest, record)

    return {
        "status": status,
        "helper_version": VERSION,
        "project": project_name,
        "run_root": str(run_root),
        "existing_canonical_state_at_start": existing_state,
        "source": asdict(source),
        "source_manifest": str(paths["source_manifest"]),
        "source_manifest_sha256": source_manifest_hash,
        "candidate_count": len(candidates),
        "satisfactory_candidates": satisfactory,
        "recommended_candidate": recommended["candidate"],
        "contact_preview_count": 3,
        **files,
        "canonical_output_changed": False,
        "visual_review_recorded": False,
        "star_removal_permitted": False,
        "next_action": (
            "CodeWarrior must open all three contact previews, complete "
            "the structured review JSON, record the review, publish one "
            "candidate, and run status in the same session."
        ),
    }


def validate_review_payload(
    *,
    project_name: str,
    run_root: Path,
    run_record: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    if payload.get("schema_version") != REVIEW_SCHEMA_VERSION:
        errors.append("schema_version is invalid.")
    if payload.get("project") != project_name:
        errors.append("project does not match.")
    if Path(str(payload.get("run_root", ""))).resolve() != run_root:
        errors.append("run_root does not match.")
    if payload.get("reviewer") != "CodeWarrior":
        errors.append("reviewer must be CodeWarrior.")
    if not str(payload.get("reviewed_at", "")).strip():
        errors.append("reviewed_at is required.")

    previews = payload.get("previews", {})
    expected_previews = run_record["review_evidence"]["contact_previews"]
    for kind in ("full", "background_regions", "detail"):
        supplied = previews.get(kind, {})
        expected = expected_previews[kind]
        if not supplied.get("inspected"):
            errors.append(f"{kind} preview is not marked inspected.")
        if (
            Path(str(supplied.get("path", ""))).resolve()
            != Path(expected["path"]).resolve()
        ):
            errors.append(f"{kind} preview path does not match.")
        if supplied.get("sha256") != expected["sha256"]:
            errors.append(f"{kind} preview checksum does not match.")

    supplied_candidates = payload.get("candidates", [])
    supplied_by_name = {
        item.get("candidate"): item
        for item in supplied_candidates
        if isinstance(item, dict)
    }
    expected_names = {
        item["candidate"] for item in run_record["candidates"]
    }
    if set(supplied_by_name) != expected_names:
        errors.append("Candidate review set is incomplete.")

    allowed_relationships = {"better", "same", "worse"}
    for candidate in run_record["candidates"]:
        name = candidate["candidate"]
        supplied = supplied_by_name.get(name, {})
        observations = str(supplied.get("observations", "")).strip()
        if len(observations) < 50:
            errors.append(
                f"{name} observations must contain at least 50 characters."
            )
        flags = supplied.get("artifact_flags", [])
        if not isinstance(flags, list) or not flags:
            errors.append(f"{name} artifact_flags are required.")
            flags = []
        unknown = set(flags) - ALLOWED_ARTIFACT_FLAGS
        if unknown:
            errors.append(f"{name} has unknown flags: {sorted(unknown)}")
        accepted = bool(supplied.get("accepted"))
        if accepted and set(flags) & SEVERE_ARTIFACT_FLAGS:
            errors.append(
                f"{name} cannot be accepted with severe artifact flags."
            )
        if (
            accepted
            and not candidate["quality_assessment"]["satisfactory"]
        ):
            errors.append(f"{name} is technically rejected.")
        for field in (
            "background_naturalness",
            "faint_nebula_preservation",
            "star_and_halo_impact",
        ):
            if supplied.get(field) not in allowed_relationships:
                errors.append(
                    f"{name} {field} must be better, same, or worse."
                )

    selected = str(payload.get("selected_candidate", "")).strip()
    if selected not in expected_names:
        errors.append("selected_candidate is invalid.")
    else:
        selected_review = supplied_by_name.get(selected, {})
        selected_record = next(
            item
            for item in run_record["candidates"]
            if item["candidate"] == selected
        )
        if not selected_review.get("accepted"):
            errors.append("Selected candidate was not accepted.")
        if not selected_record["quality_assessment"]["satisfactory"]:
            errors.append("Selected candidate is technically rejected.")
        if selected != "candidate-00":
            if (
                selected_review.get("background_naturalness")
                != "better"
            ):
                errors.append(
                    "A neutralized selection requires better background "
                    "naturalness."
                )
            if selected_review.get(
                "faint_nebula_preservation"
            ) == "worse":
                errors.append(
                    "A neutralized selection cannot worsen faint nebula."
                )
            if selected_review.get("star_and_halo_impact") == "worse":
                errors.append(
                    "A neutralized selection cannot worsen stars or halos."
                )

    rationale = str(payload.get("selection_rationale", "")).strip()
    if len(rationale) < 50:
        errors.append(
            "selection_rationale must contain at least 50 characters."
        )
    if errors:
        raise BackgroundNeutralizationError(
            "Visual review is invalid: " + " | ".join(errors)
        )

    validated = json.loads(json.dumps(payload))
    validated["validated_at"] = utc_now()
    validated["validated_by_helper_version"] = VERSION
    validated["visual_review_completed"] = True
    validated["selected_candidate"] = selected
    return validated


def record_review(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
    review_json: Path,
) -> dict[str, Any]:
    _ = workspace
    run_manifest = run_root / "run-manifest.json"
    if not run_manifest.is_file():
        raise BackgroundNeutralizationError(
            f"Run manifest is missing: {run_manifest}"
        )
    record = json.loads(run_manifest.read_text(encoding="utf-8"))
    if (
        record.get("helper_version") != VERSION
        or record.get("project_name") != project_name
    ):
        raise BackgroundNeutralizationError(
            "Candidate run is incompatible with helper 1.1.0."
        )
    payload = json.loads(review_json.read_text(encoding="utf-8"))
    validated = validate_review_payload(
        project_name=project_name,
        run_root=run_root,
        run_record=record,
        payload=payload,
    )
    review_record = run_root / "visual-review-record.json"
    json_dump_atomic(review_record, validated)
    record["visual_review_recorded"] = True
    record["visual_review_record"] = str(review_record)
    record["visual_review_record_sha256"] = sha256_file(review_record)
    record["selected_candidate"] = validated["selected_candidate"]
    json_dump_atomic(run_manifest, record)
    return {
        "status": "visual_review_recorded",
        "helper_version": VERSION,
        "project": project_name,
        "run_root": str(run_root),
        "selected_candidate": validated["selected_candidate"],
        "visual_review_record": str(review_record),
        "visual_review_record_sha256": sha256_file(review_record),
        "next_action": "Publish this exact validated review record.",
    }


def publish_project(
    *,
    workspace: Path,
    project_name: str,
    run_root: Path,
    review_record: Path,
    fresh_run: bool,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    run_manifest_path = run_root / "run-manifest.json"
    record = json.loads(
        run_manifest_path.read_text(encoding="utf-8")
    )
    if (
        record.get("helper_version") != VERSION
        or record.get("project_name") != project_name
    ):
        raise BackgroundNeutralizationError(
            "Candidate run is incompatible."
        )
    if not record.get("visual_review_recorded"):
        raise BackgroundNeutralizationError(
            "Structured visual review has not been recorded."
        )
    if (
        Path(str(record.get("visual_review_record", ""))).resolve()
        != review_record.resolve()
        or sha256_file(review_record)
        != record.get("visual_review_record_sha256")
    ):
        raise BackgroundNeutralizationError(
            "Review record path or checksum does not match."
        )
    review = json.loads(review_record.read_text(encoding="utf-8"))
    validated_review = validate_review_payload(
        project_name=project_name,
        run_root=run_root,
        run_record=record,
        payload=review,
    )
    selected_name = validated_review["selected_candidate"]
    selected = next(
        item
        for item in record["candidates"]
        if item["candidate"] == selected_name
    )

    source_manifest, source_manifest_hash, source = validate_source(paths)
    if (
        source.sha256 != record["source"]["sha256"]
        or source_manifest_hash != record["source_manifest_sha256"]
    ):
        raise BackgroundNeutralizationError(
            "SHO source evidence changed after candidate generation."
        )

    existing = paths["stable"].exists()
    if existing and not fresh_run:
        raise BackgroundNeutralizationError(
            f"Canonical directory exists: {paths['stable']}. "
            "Use --fresh-run for preservation-safe replacement."
        )

    staging = run_root / "publish-staging"
    staging.mkdir(parents=True, exist_ok=False)
    selected_output = (
        Path(selected["candidate_directory"])
        / "work"
        / "SHO-linear-neutralized.fit"
    )
    staged_output = staging / paths["stable_output"].name
    shutil.copy2(selected_output, staged_output)
    output = inspect_fits(staged_output)
    if output.sha256 != selected["output"]["sha256"]:
        raise BackgroundNeutralizationError(
            "Selected output checksum changed during staging."
        )

    selected_before = Path(selected["before_preview"]["path"])
    selected_after = Path(selected["after_preview"]["path"])
    staged_before = staging / paths["stable_before_preview"].name
    staged_after = staging / paths["stable_after_preview"].name
    shutil.copy2(selected_before, staged_before)
    shutil.copy2(selected_after, staged_after)

    contact_map = {
        "full": paths["stable_full_contact"].name,
        "background_regions": paths["stable_regions_contact"].name,
        "detail": paths["stable_detail_contact"].name,
    }
    for kind, stable_name in contact_map.items():
        shutil.copy2(
            Path(
                record["review_evidence"]["contact_previews"][kind]["path"]
            ),
            staging / stable_name,
        )
    shutil.copy2(
        review_record,
        staging / paths["stable_review"].name,
    )

    previous = (
        run_root / "previous-processing-background-neutralization"
        if existing
        else None
    )
    output_record = asdict(output)
    output_record["path"] = str(paths["stable_output"])
    manifest = {
        "schema_version": 2,
        "helper_version": VERSION,
        "created_at": utc_now(),
        "status": "ready",
        "project": project_name,
        "project_path": str(paths["project"]),
        "stage_order": {
            "upstream": UPSTREAM_STAGE,
            "current": CURRENT_STAGE,
            "downstream": NEXT_STAGE,
        },
        "upstream_stage": UPSTREAM_STAGE,
        "sho_combination_manifest": str(paths["source_manifest"]),
        "sho_combination_manifest_sha256": source_manifest_hash,
        "sho_combination_helper_version": source_manifest.get(
            "helper_version"
        ),
        "source": asdict(source),
        "candidate_count": len(record["candidates"]),
        "candidate_summaries": record["candidate_summaries"],
        "recommended_candidate": record["recommended_candidate"],
        "selected_candidate": selected_name,
        "selected_candidate_was_recommended": (
            selected_name == record["recommended_candidate"]
        ),
        "neutralization_applied": selected_name != "candidate-00",
        "selected_background_region": selected["background_region"],
        "selected_region_discovery": selected[
            "background_region_discovery"
        ],
        "selected_neutralization": selected["neutralization"],
        "selected_quality_assessment": selected[
            "quality_assessment"
        ],
        "output": output_record,
        "visual_review": {
            "required": True,
            "reviewer": "CodeWarrior",
            "record_path": str(paths["stable_review"]),
            "record_sha256": sha256_file(
                staging / paths["stable_review"].name
            ),
            "all_contact_previews_inspected": True,
        },
        "contact_previews": {
            kind: {
                "path": str(paths["stable"] / stable_name),
                "sha256": sha256_file(staging / stable_name),
            }
            for kind, stable_name in contact_map.items()
        },
        "before_preview": {
            "path": str(paths["stable_before_preview"]),
            "sha256": sha256_file(staged_before),
        },
        "after_preview": {
            "path": str(paths["stable_after_preview"]),
            "sha256": sha256_file(staged_after),
        },
        "previous_processing_background_neutralization_preserved_at": (
            str(previous) if previous is not None else None
        ),
        "visual_review_completed": True,
        "star_removal_permitted": True,
    }
    json_dump_atomic(
        staging / paths["stable_manifest"].name,
        manifest,
    )

    moved_existing = False
    try:
        if existing:
            if previous is None or previous.exists():
                raise BackgroundNeutralizationError(
                    f"Invalid preservation destination: {previous}"
                )
            paths["stable"].rename(previous)
            moved_existing = True
        staging.rename(paths["stable"])
    except Exception:
        if moved_existing and not paths["stable"].exists():
            previous.rename(paths["stable"])
        raise

    record["status"] = "published"
    record["published_at"] = utc_now()
    record["canonical_output_changed"] = True
    record["star_removal_permitted"] = True
    json_dump_atomic(run_manifest_path, record)

    status = status_project(workspace, project_name)
    if status.get("status") != "ready":
        raise BackgroundNeutralizationError(
            f"Post-publication status failed: {status}"
        )
    return {
        "status": "ready",
        "helper_version": VERSION,
        "project": project_name,
        "run_root": str(run_root),
        "selected_candidate": selected_name,
        "recommended_candidate": record["recommended_candidate"],
        "neutralization_applied": selected_name != "candidate-00",
        "output": output_record,
        "stable_manifest": str(paths["stable_manifest"]),
        "visual_review_record": str(paths["stable_review"]),
        "previous_processing_background_neutralization_preserved_at": (
            manifest[
                "previous_processing_background_neutralization_preserved_at"
            ]
        ),
        "star_removal_permitted": True,
        "post_publication_status_verified": True,
    }


def status_project(
    workspace: Path,
    project_name: str,
) -> dict[str, Any]:
    paths = project_paths(workspace, project_name)
    state = canonical_state(paths)
    if state["status"] in {"missing", "obsolete", "invalid"}:
        return {
            **state,
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "stable_directory": str(paths["stable"]),
            "manifest": str(paths["stable_manifest"]),
        }

    errors: list[str] = []
    manifest = json.loads(
        paths["stable_manifest"].read_text(encoding="utf-8")
    )
    try:
        source_manifest, source_manifest_hash, source = validate_source(paths)
        if (
            source_manifest_hash
            != manifest.get("sho_combination_manifest_sha256")
        ):
            errors.append("SHO manifest checksum changed.")
        recorded_source = manifest.get("source", {})
        if source.sha256 != recorded_source.get("sha256"):
            errors.append("SHO source checksum changed.")

        output = inspect_fits(paths["stable_output"])
        if output.sha256 != manifest.get("output", {}).get("sha256"):
            errors.append("Neutralized output checksum changed.")
        if (
            output.width != source.width
            or output.height != source.height
            or output.bitpix != -32
            or output.finite_fraction != 1.0
        ):
            errors.append("Neutralized output format changed.")

        order = manifest.get("stage_order", {})
        if order != {
            "upstream": UPSTREAM_STAGE,
            "current": CURRENT_STAGE,
            "downstream": NEXT_STAGE,
        }:
            errors.append("Background-neutralization stage order is invalid.")
        if manifest.get("visual_review_completed") is not True:
            errors.append("Visual review is not complete.")
        if manifest.get("star_removal_permitted") is not True:
            errors.append("Manifest does not permit StarNet.")

        review = paths["stable_review"]
        expected_review_hash = manifest.get(
            "visual_review", {}
        ).get("record_sha256")
        if (
            not review.is_file()
            or sha256_file(review) != expected_review_hash
        ):
            errors.append("Visual-review record is missing or changed.")
        for key, path in (
            ("before_preview", paths["stable_before_preview"]),
            ("after_preview", paths["stable_after_preview"]),
        ):
            expected = manifest.get(key, {}).get("sha256")
            if not path.is_file() or sha256_file(path) != expected:
                errors.append(f"{key} is missing or changed.")
        for kind, path in (
            ("full", paths["stable_full_contact"]),
            ("background_regions", paths["stable_regions_contact"]),
            ("detail", paths["stable_detail_contact"]),
        ):
            expected = (
                manifest.get("contact_previews", {})
                .get(kind, {})
                .get("sha256")
            )
            if not path.is_file() or sha256_file(path) != expected:
                errors.append(f"{kind} contact preview is missing or changed.")
    except Exception as exc:
        errors.append(str(exc))
        source = None
        output = None
        source_manifest_hash = None
        source_manifest = {}

    ready = not errors and manifest.get("status") == "ready"
    return {
        "status": "ready" if ready else "blocked",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "manifest": str(paths["stable_manifest"]),
        "upstream_summary": {
            "manifest": str(paths["source_manifest"]),
            "manifest_sha256": source_manifest_hash,
            "helper_version": source_manifest.get("helper_version"),
            "status": source_manifest.get("status"),
            "background_neutralization_permitted": source_manifest.get(
                "background_neutralization_permitted"
            ),
        },
        "source": asdict(source) if source else None,
        "output": asdict(output) if output else None,
        "selected_candidate": manifest.get("selected_candidate"),
        "recommended_candidate": manifest.get("recommended_candidate"),
        "neutralization_applied": manifest.get("neutralization_applied"),
        "next_stage": NEXT_STAGE,
        "errors": errors,
        "visual_review_completed": ready,
        "star_removal_permitted": ready,
    }


def write_synthetic_sho(path: Path) -> None:
    rng = np.random.default_rng(20260806)
    height = 720
    width = 760
    yy, xx = np.mgrid[0:height, 0:width]
    nebula = 0.015 * np.exp(
        -(
            ((xx - 395.0) / 150.0) ** 2
            + ((yy - 350.0) / 125.0) ** 2
        )
    )
    base = np.empty((3, height, width), dtype=np.float32)
    base[0] = 0.013 + 1.1 * nebula
    base[1] = 0.004 + 1.0 * nebula
    base[2] = 0.0015 + 0.8 * nebula
    for channel in range(3):
        base[channel] += rng.normal(
            0.0,
            0.00012,
            (height, width),
        )
    for y, x, amplitude in (
        (90, 120, 0.7),
        (610, 620, 0.5),
        (175, 590, 0.35),
        (520, 180, 0.25),
    ):
        profile = np.exp(
            -(
                (xx - x) ** 2 + (yy - y) ** 2
            ) / (2.0 * 2.0**2)
        )
        base[0] += amplitude * profile
        base[1] += 0.95 * amplitude * profile
        base[2] += 0.90 * amplitude * profile
    header = fits.Header()
    header["FILTER"] = "mixed"
    header["OBJECT"] = "Synthetic SHO background-neutralization self-test"
    fits.PrimaryHDU(data=base, header=header).writeto(
        path,
        overwrite=False,
        output_verify="fix",
    )


def complete_synthetic_review(
    template_path: Path,
    recommended: str,
) -> Path:
    payload = json.loads(template_path.read_text(encoding="utf-8"))
    payload["reviewed_at"] = utc_now()
    for preview in payload["previews"].values():
        preview["inspected"] = True
    for candidate in payload["candidates"]:
        name = candidate["candidate"]
        if name == "candidate-00":
            candidate["accepted"] = True
            candidate["artifact_flags"] = ["none"]
            candidate["background_naturalness"] = "same"
            candidate["faint_nebula_preservation"] = "same"
            candidate["star_and_halo_impact"] = "same"
            candidate["observations"] = (
                "Exact pass-through baseline preserves the synthetic source "
                "without correction and provides the comparison reference."
            )
        else:
            selected = name == recommended
            candidate["accepted"] = selected
            candidate["artifact_flags"] = (
                ["background_improved", "detail_preserved", "stars_acceptable"]
                if selected
                else ["minimal_change"]
            )
            candidate["background_naturalness"] = (
                "better" if selected else "same"
            )
            candidate["faint_nebula_preservation"] = "same"
            candidate["star_and_halo_impact"] = "same"
            candidate["observations"] = (
                "Synthetic contact-sheet inspection confirms that this "
                "candidate is technically valid; selection follows the "
                "known neutral synthetic background and preserved structure."
            )
    payload["selected_candidate"] = recommended
    payload["comparison_outcome"] = (
        "material_background_improvement"
        if recommended != "candidate-00"
        else "pass_through"
    )
    payload["selection_rationale"] = (
        "The selected synthetic candidate equalizes the deliberately biased "
        "background while preserving the nebula and star profiles in all "
        "required contact previews."
        if recommended != "candidate-00"
        else (
            "The synthetic candidates do not provide a clear improvement, "
            "so the exact pass-through baseline is retained safely."
        )
    )
    completed = template_path.with_name("completed-review.json")
    json_dump_atomic(completed, payload)
    return completed


def self_test(timeout_seconds: int) -> dict[str, Any]:
    siril = siril_version()
    root = (
        WORKSPACE
        / ".skill-self-tests"
        / "siril-background-neutralization"
        / unique_id()
    )
    synthetic_workspace = root / "workspace"
    project_name = "Synthetic Background Neutralization 1.1.0"
    project = synthetic_workspace / "Projects" / project_name
    sho_dir = project / "processing" / "sho"
    sho_dir.mkdir(parents=True, exist_ok=False)
    source = sho_dir / "SHO-linear.fit"
    write_synthetic_sho(source)
    source_evidence = inspect_fits(source)
    sho_manifest = sho_dir / "sho-combination-manifest.json"
    json_dump_atomic(
        sho_manifest,
        {
            "schema_version": 2,
            "helper_version": REQUIRED_SHO_HELPER_VERSION,
            "status": "ready",
            "project": project_name,
            "project_path": str(project),
            "stage_order": {
                "upstream": "siril-mono-linear-denoise",
                "current": UPSTREAM_STAGE,
                "downstream": CURRENT_STAGE,
            },
            "upstream_stage": "siril-mono-linear-denoise",
            "output": asdict(source_evidence),
            "background_neutralization_permitted": True,
            "star_removal_permitted": False,
        },
    )

    generated = run_project(
        workspace=synthetic_workspace,
        project_name=project_name,
        timeout_seconds=timeout_seconds,
        fresh_run=False,
        regions=None,
    )
    completed = complete_synthetic_review(
        Path(generated["visual_review_template"]),
        generated["recommended_candidate"],
    )
    recorded = record_review(
        workspace=synthetic_workspace,
        project_name=project_name,
        run_root=Path(generated["run_root"]),
        review_json=completed,
    )
    published = publish_project(
        workspace=synthetic_workspace,
        project_name=project_name,
        run_root=Path(generated["run_root"]),
        review_record=Path(recorded["visual_review_record"]),
        fresh_run=False,
    )
    checked = status_project(
        synthetic_workspace,
        project_name,
    )
    if (
        checked.get("status") != "ready"
        or checked.get("star_removal_permitted") is not True
    ):
        raise BackgroundNeutralizationError(
            f"Synthetic self-test failed: {checked}"
        )
    return {
        "status": "success",
        "helper_version": VERSION,
        "self_test_directory": str(root),
        "siril": siril,
        "candidate_count": generated["candidate_count"],
        "recommended_candidate": generated["recommended_candidate"],
        "selected_candidate": published["selected_candidate"],
        "neutralization_applied": published["neutralization_applied"],
        "contact_preview_count": generated["contact_preview_count"],
        "output": published["output"],
        "final_status": checked["status"],
        "next_stage": checked["next_stage"],
        "star_removal_permitted": checked["star_removal_permitted"],
        "tests": [
            "SHO-combination 1.1.1 upstream contract",
            "star-robust outer-field region discovery",
            "pass-through safety candidate",
            "three zero-sum additive neutralization candidates",
            "valid negative linear values are not treated as clipping",
            "constant per-channel offset validation",
            "luminance preservation",
            "full, region, and detail contact previews",
            "structured visual-review validation",
            "atomic publication and preservation logic",
            "post-publication status verification",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate, review, and publish linear background-neutralization "
            "candidates from canonical SHO-linear.fit."
        )
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command")

    self_test_parser = subparsers.add_parser("self-test")
    self_test_parser.add_argument("--timeout", type=int, default=1800)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--project", required=True)
    run_parser.add_argument("--timeout", type=int, default=7200)
    run_parser.add_argument("--fresh-run", action="store_true")
    run_parser.add_argument(
        "--region",
        action="append",
        type=parse_region,
        default=None,
    )

    review_parser = subparsers.add_parser("record-review")
    review_parser.add_argument("--project", required=True)
    review_parser.add_argument("--run-root", required=True, type=Path)
    review_parser.add_argument("--review-json", required=True, type=Path)

    publish_parser = subparsers.add_parser("publish")
    publish_parser.add_argument("--project", required=True)
    publish_parser.add_argument("--run-root", required=True, type=Path)
    publish_parser.add_argument(
        "--review-record",
        required=True,
        type=Path,
    )
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
        elif args.command == "record-review":
            payload = record_review(
                workspace=WORKSPACE,
                project_name=args.project,
                run_root=args.run_root.resolve(),
                review_json=args.review_json.resolve(),
            )
        elif args.command == "publish":
            payload = publish_project(
                workspace=WORKSPACE,
                project_name=args.project,
                run_root=args.run_root.resolve(),
                review_record=args.review_record.resolve(),
                fresh_run=args.fresh_run,
            )
        elif args.command == "status":
            payload = status_project(WORKSPACE, args.project)
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
        in {
            "success",
            "ready",
            "awaiting_visual_selection",
            "visual_review_recorded",
            "missing",
            "obsolete",
        }
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
