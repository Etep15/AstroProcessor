#!/usr/bin/env python3
"""Adaptive StarNet removal with deterministic image-quality review.

This orchestrator uses the proven low-level ``starnet_removal.py`` backend,
checks whether the stars layer contains diffuse nebular structure, and performs
at most three quality-driven retries with controlled temporary stretches.
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import math
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

VERSION = "1.2.0"
BACKEND_REQUIRED_VERSION = "1.0.4"
DEFAULT_TIMEOUT_SECONDS = 7200
MAX_RETRIES_LIMIT = 3
MAX_PROXY_DIMENSION = 1024

# These thresholds are deliberately conservative. A candidate must both remove
# point sources and avoid a broad residual correlated with the starless nebula.
QUALITY_THRESHOLDS = {
    "luma_nebula_background_ratio": 2.20,
    "worst_channel_nebula_background_ratio": 3.00,
    "luma_structure_correlation": 0.78,
    "worst_channel_structure_correlation": 0.88,
    "luma_relative_nebula_leakage": 0.022,
    "worst_channel_relative_nebula_leakage": 0.030,
    "remaining_star_peak_energy_ratio": 0.080,
}

# Initial run plus at most three retries. Lower target background values expose
# bright narrowband filaments less aggressively to StarNet. Upsampling is used
# on retries because it can help small stars in complex structures.
BASELINE_CONFIG = {
    "label": "baseline-auto-like",
    "target_background": 0.25,
    "upsample": False,
    "stride": 256,
    "protect_highlights": True,
}
LEAKAGE_RETRY_CONFIGS = (
    {
        "label": "retry-1-dimmer-upsampled",
        "target_background": 0.18,
        "upsample": True,
        "stride": 256,
        "protect_highlights": True,
    },
    {
        "label": "retry-2-dimmer-upsampled",
        "target_background": 0.12,
        "upsample": True,
        "stride": 256,
        "protect_highlights": True,
    },
    {
        "label": "retry-3-darkest-upsampled",
        "target_background": 0.08,
        "upsample": True,
        "stride": 256,
        "protect_highlights": True,
    },
)

# The third and final StarNet retry may be followed by one deterministic
# compact-star cleanup. This is not another StarNet retry. It estimates the
# broad positive residual after clipping star cores, transfers that diffuse
# component back to the starless image, and preserves exact reconstruction.
COMPACT_CLEANUP_CONFIG = {
    "label": "retry-3-compact-star-cleanup",
    "structure_radius_source_pixels": 32,
    "star_core_clip_percentile": 99.0,
    "local_background_percentile": 50.0,
    "subtraction_strength": 1.0,
    "minimum_star_core_energy_retention": 0.97,
    "maximum_positive_flux_removed_fraction": 0.12,
}


SCRIPT_DIR = Path(__file__).resolve().parent
BACKEND_PATH = SCRIPT_DIR / "starnet_removal.py"


class AdaptiveStarNetError(RuntimeError):
    pass


def load_backend():
    if not BACKEND_PATH.is_file():
        raise AdaptiveStarNetError(
            f"Required backend is missing: {BACKEND_PATH}"
        )
    module_name = "siril_starnet_removal_backend"
    spec = importlib.util.spec_from_file_location(module_name, BACKEND_PATH)
    if spec is None or spec.loader is None:
        raise AdaptiveStarNetError(
            f"Cannot load backend module: {BACKEND_PATH}"
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    if getattr(module, "VERSION", None) != BACKEND_REQUIRED_VERSION:
        raise AdaptiveStarNetError(
            "Adaptive orchestrator requires backend "
            f"{BACKEND_REQUIRED_VERSION}; found "
            f"{getattr(module, 'VERSION', None)}"
        )
    return module


base = load_backend()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _block_mean_proxy(array, max_dimension: int = MAX_PROXY_DIMENSION):
    import numpy as np

    if array.ndim != 3 or array.shape[0] != 3:
        raise AdaptiveStarNetError(
            f"Quality review requires RGB planar data; found {array.shape}"
        )
    height, width = array.shape[-2:]
    factor = max(1, int(math.ceil(max(height, width) / max_dimension)))
    usable_h = (height // factor) * factor
    usable_w = (width // factor) * factor
    cropped = array[:, :usable_h, :usable_w]
    if factor == 1:
        return np.asarray(cropped, dtype=np.float32), factor
    proxy = cropped.reshape(
        3,
        usable_h // factor,
        factor,
        usable_w // factor,
        factor,
    ).mean(axis=(2, 4), dtype=np.float64)
    return proxy.astype(np.float32), factor


def _box_blur(image, radius: int):
    import numpy as np

    radius = int(max(1, radius))
    padded = np.pad(
        np.asarray(image, dtype=np.float32),
        ((radius, radius), (radius, radius)),
        mode="reflect",
    )
    integral = np.pad(
        padded,
        ((1, 0), (1, 0)),
        mode="constant",
    ).cumsum(axis=0, dtype=np.float64).cumsum(axis=1, dtype=np.float64)
    kernel = 2 * radius + 1
    total = (
        integral[kernel:, kernel:]
        - integral[:-kernel, kernel:]
        - integral[kernel:, :-kernel]
        + integral[:-kernel, :-kernel]
    )
    return (total / float(kernel * kernel)).astype(np.float32)


def _safe_correlation(first, second) -> float:
    import numpy as np

    a = np.asarray(first, dtype=np.float64).ravel()
    b = np.asarray(second, dtype=np.float64).ravel()
    a -= float(np.mean(a))
    b -= float(np.mean(b))
    denominator = float(np.sqrt(np.sum(a * a) * np.sum(b * b)))
    if denominator <= 1e-30:
        return 0.0
    return float(np.sum(a * b) / denominator)


def _residual_structure_metrics(
    residual_map,
    nebula_map,
    nebula_region,
    background_region,
    radius: int,
) -> dict[str, float]:
    import numpy as np

    positive = np.maximum(np.asarray(residual_map, dtype=np.float32), 0.0)
    cap = float(np.percentile(positive, 99.0))
    if cap <= 0.0:
        cap = float(np.max(positive))
    clipped = np.minimum(positive, cap) if cap > 0.0 else positive
    smooth = _box_blur(clipped, radius)

    nebula_median = float(np.median(smooth[nebula_region]))
    background_median = float(np.median(smooth[background_region]))
    floor = max(1e-12, abs(background_median) * 1e-6)
    ratio = nebula_median / max(background_median, floor)

    starless_nebula = float(np.median(nebula_map[nebula_region]))
    starless_background = float(np.median(nebula_map[background_region]))
    starless_contrast = max(
        1e-12,
        starless_nebula - starless_background,
    )
    relative_leakage = max(
        0.0,
        nebula_median - background_median,
    ) / starless_contrast

    return {
        "nebula_median": nebula_median,
        "background_median": background_median,
        "nebula_background_ratio": float(ratio),
        "relative_nebula_leakage": float(relative_leakage),
        "structure_correlation": _safe_correlation(smooth, nebula_map),
        "star_core_clip_percentile": 99.0,
        "star_core_clip_value": cap,
    }


def quality_metrics_from_arrays(original, starless, stars) -> dict[str, Any]:
    """Measure star removal and broad nebular leakage deterministically."""
    import numpy as np

    original_proxy, factor = _block_mean_proxy(original)
    starless_proxy, factor_starless = _block_mean_proxy(starless)
    stars_proxy, factor_stars = _block_mean_proxy(stars)
    if factor_starless != factor or factor_stars != factor:
        raise AdaptiveStarNetError("Quality proxies used inconsistent scaling.")
    if (
        original_proxy.shape != starless_proxy.shape
        or original_proxy.shape != stars_proxy.shape
    ):
        raise AdaptiveStarNetError(
            "Quality review inputs have different proxy shapes."
        )

    weights = np.asarray([0.2126, 0.7152, 0.0722], dtype=np.float32)
    weights = weights[:, np.newaxis, np.newaxis]
    original_luma = np.sum(original_proxy * weights, axis=0)
    starless_luma = np.sum(starless_proxy * weights, axis=0)
    positive_stars = np.maximum(stars_proxy, 0.0)
    stars_luma = np.sum(positive_stars * weights, axis=0)

    # Approximately 32 original-image pixels of smoothing, clamped for small
    # images. This suppresses star cores and exposes broad residual structure.
    structure_radius = max(4, int(round(32.0 / factor)))
    structure_radius = min(
        structure_radius,
        max(4, min(starless_luma.shape) // 12),
    )
    nebula_map = _box_blur(starless_luma, structure_radius)
    high_threshold = float(np.percentile(nebula_map, 90.0))
    low_threshold = float(np.percentile(nebula_map, 50.0))
    nebula_region = nebula_map >= high_threshold
    background_region = nebula_map <= low_threshold
    if int(np.sum(nebula_region)) < 100 or int(np.sum(background_region)) < 100:
        raise AdaptiveStarNetError(
            "Quality review could not form representative nebula/background regions."
        )

    channel_names = ("red", "green", "blue")
    per_channel: dict[str, dict[str, float]] = {}
    for index, name in enumerate(channel_names):
        per_channel[name] = _residual_structure_metrics(
            positive_stars[index],
            nebula_map,
            nebula_region,
            background_region,
            structure_radius,
        )
    luma_structure = _residual_structure_metrics(
        stars_luma,
        nebula_map,
        nebula_region,
        background_region,
        structure_radius,
    )

    # Check whether bright point sources remain in the starless result. The
    # mask is defined by the top 0.5% of high-frequency energy in the original.
    peak_radius = max(1, int(round(6.0 / factor)))
    original_highpass = np.maximum(
        original_luma - _box_blur(original_luma, peak_radius),
        0.0,
    )
    starless_highpass = np.maximum(
        starless_luma - _box_blur(starless_luma, peak_radius),
        0.0,
    )
    peak_threshold = float(np.percentile(original_highpass, 99.5))
    peak_region = original_highpass >= peak_threshold
    original_peak_energy = float(np.sum(original_highpass[peak_region]))
    remaining_peak_energy = float(np.sum(starless_highpass[peak_region]))
    remaining_ratio = (
        remaining_peak_energy / original_peak_energy
        if original_peak_energy > 1e-20
        else 0.0
    )

    worst_ratio_channel = max(
        per_channel,
        key=lambda name: per_channel[name]["nebula_background_ratio"],
    )
    worst_corr_channel = max(
        per_channel,
        key=lambda name: per_channel[name]["structure_correlation"],
    )
    worst_relative_channel = max(
        per_channel,
        key=lambda name: per_channel[name]["relative_nebula_leakage"],
    )

    metrics = {
        "proxy_downsample_factor": factor,
        "proxy_shape": list(original_proxy.shape),
        "structure_radius_proxy_pixels": structure_radius,
        "structure_radius_source_pixels": structure_radius * factor,
        "nebula_region_percentile": 90.0,
        "background_region_percentile": 50.0,
        "luma_nebula_background_ratio": luma_structure[
            "nebula_background_ratio"
        ],
        "luma_structure_correlation": luma_structure[
            "structure_correlation"
        ],
        "luma_relative_nebula_leakage": luma_structure[
            "relative_nebula_leakage"
        ],
        "worst_channel_nebula_background_ratio": per_channel[
            worst_ratio_channel
        ]["nebula_background_ratio"],
        "worst_ratio_channel": worst_ratio_channel,
        "worst_channel_structure_correlation": per_channel[
            worst_corr_channel
        ]["structure_correlation"],
        "worst_correlation_channel": worst_corr_channel,
        "worst_channel_relative_nebula_leakage": per_channel[
            worst_relative_channel
        ]["relative_nebula_leakage"],
        "worst_relative_leakage_channel": worst_relative_channel,
        "remaining_star_peak_energy_ratio": float(remaining_ratio),
        "original_peak_energy": original_peak_energy,
        "remaining_peak_energy": remaining_peak_energy,
        "per_channel": per_channel,
        "luma_detail": luma_structure,
    }

    failed_checks: list[dict[str, Any]] = []
    for key, threshold in QUALITY_THRESHOLDS.items():
        value = float(metrics[key])
        if value > threshold:
            failed_checks.append(
                {
                    "metric": key,
                    "value": value,
                    "maximum_allowed": threshold,
                }
            )

    normalized_scores = {
        key: float(metrics[key]) / threshold
        for key, threshold in QUALITY_THRESHOLDS.items()
    }
    quality_score = max(normalized_scores.values())
    diffuse_failures = [
        item
        for item in failed_checks
        if item["metric"] != "remaining_star_peak_energy_ratio"
    ]
    remaining_star_failure = any(
        item["metric"] == "remaining_star_peak_energy_ratio"
        for item in failed_checks
    )

    return {
        "status": "satisfactory" if not failed_checks else "needs_retry",
        "satisfactory": not failed_checks,
        "quality_score": float(quality_score),
        "thresholds": dict(QUALITY_THRESHOLDS),
        "metrics": metrics,
        "failed_checks": failed_checks,
        "failure_classification": {
            "diffuse_nebula_leakage": bool(diffuse_failures),
            "remaining_stars": remaining_star_failure,
        },
        "interpretation": (
            "The stars layer is sufficiently sparse and not strongly correlated "
            "with broad starless nebulosity."
            if not failed_checks
            else "The candidate is technically valid but failed visual-quality "
            "proxies; another controlled StarNet attempt is required."
        ),
    }


def quality_metrics_from_paths(
    original_path: Path,
    starless_path: Path,
    stars_path: Path,
) -> dict[str, Any]:
    import gc

    original = base.read_fits_array(original_path)
    starless = base.read_fits_array(starless_path)
    stars = base.read_fits_array(stars_path)
    try:
        return quality_metrics_from_arrays(original, starless, stars)
    finally:
        del original, starless, stars
        gc.collect()


def _mtf_scalar(value: float, midtones: float) -> float:
    if value <= 0.0:
        return 0.0
    if value >= 1.0:
        return 1.0
    denominator = (2.0 * midtones - 1.0) * value - midtones
    return ((midtones - 1.0) * value) / denominator


def _compute_mtf_params(array, target_background: float):
    import numpy as np

    params = []
    for channel in range(3):
        data = array[channel]
        median = float(np.median(data))
        mad = float(np.median(np.abs(data - median))) * 1.4826
        if mad == 0.0:
            mad = 0.001
        shadows = max(0.0, median - 2.80 * mad)
        balance_input = max(0.0, median - shadows)
        midtones = _mtf_scalar(balance_input, target_background)
        params.append(
            {
                "shadows": shadows,
                "midtones": midtones,
                "highlights": 1.0,
                "median": median,
                "mad": mad,
            }
        )
    return params


def _apply_mtf(array, params):
    import numpy as np

    output = np.empty_like(array, dtype=np.float32)
    for channel, record in enumerate(params):
        shadows = float(record["shadows"])
        midtones = float(record["midtones"])
        highlights = float(record["highlights"])
        span = highlights - shadows
        if span <= 0.0:
            output[channel] = 0.0
            continue
        x = np.clip((array[channel] - shadows) / span, 0.0, 1.0)
        denominator = (2.0 * midtones - 1.0) * x - midtones
        with np.errstate(divide="ignore", invalid="ignore"):
            transformed = ((midtones - 1.0) * x) / denominator
        output[channel] = np.nan_to_num(
            transformed,
            nan=0.0,
            posinf=1.0,
            neginf=0.0,
        )
    return np.clip(output, 0.0, 1.0).astype(np.float32)


def _apply_inverse_mtf(array, params):
    import numpy as np

    output = np.empty_like(array, dtype=np.float32)
    for channel, record in enumerate(params):
        shadows = float(record["shadows"])
        midtones = float(record["midtones"])
        highlights = float(record["highlights"])
        y = np.asarray(array[channel], dtype=np.float32)
        a = (shadows + highlights) * midtones - shadows
        b = shadows * (1.0 - midtones)
        numerator = a * y + b
        denominator = (2.0 * midtones - 1.0) * y - midtones + 1.0
        with np.errstate(divide="ignore", invalid="ignore"):
            inverse = numerator / denominator
        output[channel] = np.nan_to_num(
            inverse,
            nan=0.0,
            posinf=1.0,
            neginf=0.0,
        )
    return np.clip(output, 0.0, 1.0).astype(np.float32)


def prepare_controlled_input(
    source_path: Path,
    destination: Path,
    target_background: float,
) -> dict[str, Any]:
    import numpy as np
    from astropy.io import fits

    if destination.exists():
        raise AdaptiveStarNetError(
            f"Refusing to overwrite controlled input: {destination}"
        )
    with fits.open(source_path, memmap=False, do_not_scale_image_data=False) as hdul:
        source = np.asarray(hdul[0].data, dtype=np.float32)
        header = hdul[0].header.copy()
    if source.ndim != 3 or source.shape[0] != 3:
        raise AdaptiveStarNetError("Controlled StarNet input must be RGB.")
    peak = float(np.max(source))
    scale = max(1.0, peak)
    normalized = np.clip(source / scale, 0.0, 1.0).astype(np.float32)
    params = _compute_mtf_params(normalized, target_background)
    stretched = _apply_mtf(normalized, params)
    header["FILTER"] = "mixed_StarNetInput"
    header["HISTORY"] = (
        f"Temporary StarNet input target background {target_background:.3f}."
    )
    header["HISTORY"] = (
        "Generated by adaptive siril-starnet-removal; not a stable product."
    )
    fits.PrimaryHDU(data=stretched, header=header).writeto(
        destination,
        overwrite=False,
        checksum=True,
    )
    return {
        "target_background": target_background,
        "source_scale": scale,
        "source_peak": peak,
        "mtf_parameters": params,
        "path": str(destination),
        "sha256": base.sha256_file(destination),
    }


def destretch_starless(
    stretched_path: Path,
    source_path: Path,
    destination: Path,
    stretch_record: dict[str, Any],
) -> Any:
    import numpy as np
    from astropy.io import fits

    if destination.exists():
        raise AdaptiveStarNetError(
            f"Refusing to overwrite linear starless output: {destination}"
        )
    with fits.open(
        stretched_path,
        memmap=False,
        do_not_scale_image_data=False,
    ) as hdul:
        stretched = np.asarray(hdul[0].data, dtype=np.float32)
    with fits.open(
        source_path,
        memmap=False,
        do_not_scale_image_data=False,
    ) as hdul:
        header = hdul[0].header.copy()
    linear_normalized = _apply_inverse_mtf(
        stretched,
        stretch_record["mtf_parameters"],
    )
    linear = (
        linear_normalized * float(stretch_record["source_scale"])
    ).astype(np.float32)
    if not np.all(np.isfinite(linear)):
        raise AdaptiveStarNetError(
            "Inverse-stretched starless data contains non-finite values."
        )
    header["FILTER"] = "mixed_Starless"
    header["HISTORY"] = (
        "StarNet starless image returned to the original linear domain."
    )
    header["HISTORY"] = (
        f"Adaptive siril-starnet-removal helper version {VERSION}."
    )
    fits.PrimaryHDU(data=linear, header=header).writeto(
        destination,
        overwrite=False,
        checksum=True,
    )
    return base.inspect_fits(
        destination,
        expected_channels=3,
        require_float32=True,
    )


def candidate_siril_script(executable: Path, config: dict[str, Any]) -> str:
    executable_text = str(executable)
    if '"' in executable_text or "\n" in executable_text:
        raise AdaptiveStarNetError(
            f"Unsafe StarNet executable path: {executable}"
        )
    upsample_option = "--upsample" if config["upsample"] else "--no-upsample"
    highlight_option = (
        "--protect-highlights"
        if config["protect_highlights"]
        else "--disable-highlights-protection"
    )
    return "\n".join(
        [
            f"requires {base.MINIMUM_SIRIL_VERSION}",
            "setext fit",
            'load "SHO_input_stretched.fit"',
            (
                f'pyscript StarNet.py --exe "{executable_text}" '
                f'--no-linear --stride {int(config["stride"])} '
                f"{upsample_option} {highlight_option} --masks subtract"
            ),
            "save SHO_starless_stretched -chksum",
            "close",
            "",
        ]
    )


def execute_candidate(
    source_path: Path,
    source_evidence,
    candidate_dir: Path,
    config: dict[str, Any],
    timeout_seconds: int,
) -> dict[str, Any]:
    workdir = candidate_dir / "work"
    logs = candidate_dir / "logs"
    previews = candidate_dir / "previews"
    for directory in (workdir, logs, previews):
        directory.mkdir(parents=True, exist_ok=False)

    staged_script, upstream = base.stage_vendored_starnet(workdir)
    cli = base.starnet_cli_version()
    stretched_input = workdir / "SHO_input_stretched.fit"
    stretch_record = prepare_controlled_input(
        source_path,
        stretched_input,
        float(config["target_background"]),
    )

    script_path = candidate_dir / "starnet-controlled.ssf"
    script_path.write_text(
        candidate_siril_script(Path(cli["path"]), config),
        encoding="utf-8",
    )
    run_record = base.run_siril(
        workdir=workdir,
        script=script_path,
        stdout_path=logs / "stdout.log",
        stderr_path=logs / "stderr.log",
        timeout_seconds=timeout_seconds,
    )
    combined_log = run_record.pop("combined_log_text")
    if run_record["timed_out"]:
        raise AdaptiveStarNetError(
            f"StarNet candidate timed out; preserved at {candidate_dir}"
        )
    if run_record["exit_status"] != 0 or run_record["fatal_log_markers"]:
        raise AdaptiveStarNetError(
            "StarNet candidate failed technically; preserved at "
            f"{candidate_dir}. Exit={run_record['exit_status']}, "
            f"markers={run_record['fatal_log_markers']}"
        )
    if "starnet: working: done!" not in combined_log.lower():
        raise AdaptiveStarNetError(
            "StarNet did not report successful neural-network completion; "
            f"candidate preserved at {candidate_dir}"
        )

    stretched_starless = workdir / "SHO_starless_stretched.fit"
    base.inspect_fits(
        stretched_starless,
        expected_channels=3,
        require_float32=True,
    )
    starless_path = workdir / "SHO_starless_linear.fit"
    starless_evidence = destretch_starless(
        stretched_starless,
        source_path,
        starless_path,
        stretch_record,
    )
    if (
        starless_evidence.width != source_evidence.width
        or starless_evidence.height != source_evidence.height
    ):
        raise AdaptiveStarNetError(
            "Candidate starless dimensions differ from the source."
        )

    stars_path = workdir / "SHO_stars_linear.fit"
    stars_evidence = base.write_stars_difference(
        source_path,
        starless_path,
        stars_path,
    )
    separation = base.separation_metrics(
        source_path,
        starless_path,
        stars_path,
    )

    repository_mask = base.find_repository_mask(workdir)
    if repository_mask is None:
        raise AdaptiveStarNetError(
            "Candidate did not produce exactly one repository subtraction mask; "
            f"preserved at {candidate_dir}"
        )
    repository_mask_evidence = asdict(
        base.inspect_fits(repository_mask, expected_channels=3)
    )

    quality = quality_metrics_from_paths(
        source_path,
        starless_path,
        stars_path,
    )

    clipped_stars = workdir / "SHO_stars_preview_clipped.fit"
    base.create_clipped_preview_stars(stars_path, clipped_stars)
    preview_script_path = candidate_dir / "preview.ssf"
    preview_script_path.write_text(
        base.preview_script(starless_path, clipped_stars, previews),
        encoding="utf-8",
    )
    preview_run = base.run_siril(
        workdir=candidate_dir,
        script=preview_script_path,
        stdout_path=logs / "preview-stdout.log",
        stderr_path=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 600),
    )
    preview_run.pop("combined_log_text", None)

    result = {
        "status": quality["status"],
        "created_at": utc_now(),
        "helper_version": VERSION,
        "backend_version": base.VERSION,
        "candidate": candidate_dir.name,
        "candidate_directory": str(candidate_dir),
        "config": dict(config),
        "controlled_stretch": stretch_record,
        "script": str(script_path),
        "script_sha256": base.sha256_file(script_path),
        "siril_run": run_record,
        "staged_starnet_script": str(staged_script),
        "upstream_starnet": upstream,
        "starnet_cli": cli,
        "starless_path": str(starless_path),
        "starless_evidence": asdict(starless_evidence),
        "stars_path": str(stars_path),
        "stars_evidence": asdict(stars_evidence),
        "repository_subtract_mask": str(repository_mask),
        "repository_subtract_mask_evidence": repository_mask_evidence,
        "separation_metrics": separation,
        "quality_assessment": quality,
        "preview_script": str(preview_script_path),
        "preview_run": preview_run,
        "previews": {
            "starless": str(
                previews / "SHO-starless-autostretch-preview.png"
            )
            if (
                previews / "SHO-starless-autostretch-preview.png"
            ).is_file()
            else None,
            "stars": str(previews / "SHO-stars-autostretch-preview.png")
            if (previews / "SHO-stars-autostretch-preview.png").is_file()
            else None,
        },
    }
    base.json_dump_atomic(candidate_dir / "candidate-result.json", result)
    return result


def _write_float32_rgb_like(
    source_path: Path,
    destination: Path,
    array,
    filter_name: str,
    histories: list[str],
):
    import numpy as np
    from astropy.io import fits

    if destination.exists():
        raise AdaptiveStarNetError(
            f"Refusing to overwrite compact-cleanup evidence: {destination}"
        )
    data = np.asarray(array, dtype=np.float32)
    if data.ndim != 3 or data.shape[0] != 3:
        raise AdaptiveStarNetError(
            f"Compact cleanup requires RGB planar data; found {data.shape}"
        )
    if not np.all(np.isfinite(data)):
        raise AdaptiveStarNetError(
            f"Compact-cleanup output contains non-finite values: {destination}"
        )
    with fits.open(
        source_path,
        memmap=False,
        do_not_scale_image_data=False,
    ) as hdul:
        header = hdul[0].header.copy()
    header["FILTER"] = filter_name
    for history in histories:
        header["HISTORY"] = history
    fits.PrimaryHDU(data=data, header=header).writeto(
        destination,
        overwrite=False,
        checksum=True,
    )
    return base.inspect_fits(
        destination,
        expected_channels=3,
        require_float32=True,
    )


def _star_core_energy_retention(original, raw_stars, cleaned_stars) -> float:
    import numpy as np

    original_proxy, factor = _block_mean_proxy(original)
    raw_proxy, raw_factor = _block_mean_proxy(raw_stars)
    cleaned_proxy, cleaned_factor = _block_mean_proxy(cleaned_stars)
    if raw_factor != factor or cleaned_factor != factor:
        raise AdaptiveStarNetError(
            "Compact-cleanup star-core proxies used inconsistent scaling."
        )
    weights = np.asarray([0.2126, 0.7152, 0.0722], dtype=np.float32)
    weights = weights[:, np.newaxis, np.newaxis]
    original_luma = np.sum(original_proxy * weights, axis=0)
    raw_luma = np.sum(np.maximum(raw_proxy, 0.0) * weights, axis=0)
    cleaned_luma = np.sum(
        np.maximum(cleaned_proxy, 0.0) * weights,
        axis=0,
    )
    peak_radius = max(1, int(round(6.0 / factor)))
    original_highpass = np.maximum(
        original_luma - _box_blur(original_luma, peak_radius),
        0.0,
    )
    peak_threshold = float(np.percentile(original_highpass, 99.5))
    peak_region = original_highpass >= peak_threshold
    raw_energy = float(np.sum(raw_luma[peak_region]))
    cleaned_energy = float(np.sum(cleaned_luma[peak_region]))
    if raw_energy <= 1e-20:
        return 1.0
    return cleaned_energy / raw_energy


def compact_cleanup_arrays(
    original,
    raw_starless,
    raw_stars,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transfer broad positive residuals back to the starless image.

    Each RGB channel is treated independently. Bright star cores are clipped
    before a 32-source-pixel box blur, so the estimate follows diffuse cloud
    structure instead of stellar peaks. The per-channel median of that broad
    estimate is retained as the normal star-field floor. Only the positive
    excess above that floor is transferable, and the transfer is capped by the
    raw positive residual at every pixel.
    """
    import numpy as np

    cfg = dict(COMPACT_CLEANUP_CONFIG)
    if config:
        cfg.update(config)
    original = np.asarray(original, dtype=np.float32)
    raw_starless = np.asarray(raw_starless, dtype=np.float32)
    raw_stars = np.asarray(raw_stars, dtype=np.float32)
    if original.shape != raw_starless.shape or original.shape != raw_stars.shape:
        raise AdaptiveStarNetError(
            "Compact-cleanup input arrays have different shapes."
        )
    if original.ndim != 3 or original.shape[0] != 3:
        raise AdaptiveStarNetError("Compact cleanup requires RGB planar data.")
    if not (
        np.all(np.isfinite(original))
        and np.all(np.isfinite(raw_starless))
        and np.all(np.isfinite(raw_stars))
    ):
        raise AdaptiveStarNetError(
            "Compact-cleanup input arrays contain non-finite values."
        )

    raw_reconstruction_error = float(
        np.max(np.abs(original.astype(np.float64) - (
            raw_starless.astype(np.float64) + raw_stars.astype(np.float64)
        )))
    )
    if raw_reconstruction_error > base.RECONSTRUCTION_TOLERANCE:
        raise AdaptiveStarNetError(
            "Raw candidate does not reconstruct the source before cleanup: "
            f"{raw_reconstruction_error}"
        )

    radius = int(cfg["structure_radius_source_pixels"])
    cap_percentile = float(cfg["star_core_clip_percentile"])
    background_percentile = float(cfg["local_background_percentile"])
    strength = float(cfg["subtraction_strength"])
    if radius < 4 or radius > 128:
        raise AdaptiveStarNetError("Compact-cleanup radius must be 4..128.")
    if not 95.0 <= cap_percentile <= 99.9:
        raise AdaptiveStarNetError(
            "Compact-cleanup star-core clip percentile must be 95..99.9."
        )
    if not 25.0 <= background_percentile <= 75.0:
        raise AdaptiveStarNetError(
            "Compact-cleanup background percentile must be 25..75."
        )
    if not 0.25 <= strength <= 2.0:
        raise AdaptiveStarNetError(
            "Compact-cleanup subtraction strength must be 0.25..2.0."
        )

    cleaned_stars = np.empty_like(raw_stars, dtype=np.float32)
    diffuse_transfer = np.empty_like(raw_stars, dtype=np.float32)
    support_mask = np.zeros_like(raw_stars, dtype=np.float32)
    channel_records = []
    channel_names = ("red", "green", "blue")
    for channel, name in enumerate(channel_names):
        positive = np.maximum(raw_stars[channel], 0.0)
        cap = float(np.percentile(positive, cap_percentile))
        clipped = np.minimum(positive, cap)
        broad = _box_blur(clipped, radius)
        floor = float(np.percentile(broad, background_percentile))
        transferable = np.maximum(broad - floor, 0.0) * strength
        transfer = np.minimum(positive, transferable).astype(np.float32)
        cleaned = (raw_stars[channel] - transfer).astype(np.float32)
        cleaned_stars[channel] = cleaned
        diffuse_transfer[channel] = transfer
        np.divide(
            np.maximum(cleaned, 0.0),
            positive,
            out=support_mask[channel],
            where=positive > 1e-12,
        )
        channel_records.append(
            {
                "channel": name,
                "star_core_clip_value": cap,
                "broad_floor": floor,
                "maximum_transfer": float(np.max(transfer)),
                "mean_transfer": float(np.mean(transfer)),
                "transferred_pixel_fraction": float(
                    np.mean(transfer > 1e-12)
                ),
            }
        )

    corrected_starless = (original - cleaned_stars).astype(np.float32)
    reconstruction_error = float(
        np.max(np.abs(original.astype(np.float64) - (
            corrected_starless.astype(np.float64)
            + cleaned_stars.astype(np.float64)
        )))
    )
    if reconstruction_error > base.RECONSTRUCTION_TOLERANCE:
        raise AdaptiveStarNetError(
            "Compact-cleanup pair failed exact reconstruction: "
            f"{reconstruction_error}"
        )
    if float(np.min(diffuse_transfer)) < 0.0:
        raise AdaptiveStarNetError(
            "Compact-cleanup transfer unexpectedly contains negatives."
        )
    positive_raw = np.maximum(raw_stars, 0.0)
    transfer_violation = int(
        np.count_nonzero(diffuse_transfer > positive_raw + 1e-7)
    )
    if transfer_violation:
        raise AdaptiveStarNetError(
            "Compact-cleanup transfer exceeded the raw positive residual."
        )

    raw_positive_flux = float(np.sum(positive_raw, dtype=np.float64))
    cleaned_positive_flux = float(
        np.sum(np.maximum(cleaned_stars, 0.0), dtype=np.float64)
    )
    removed_fraction = (
        max(0.0, 1.0 - cleaned_positive_flux / raw_positive_flux)
        if raw_positive_flux > 1e-20
        else 0.0
    )
    core_retention = _star_core_energy_retention(
        original,
        raw_stars,
        cleaned_stars,
    )
    quality = quality_metrics_from_arrays(
        original,
        corrected_starless,
        cleaned_stars,
    )
    cleanup_checks = {
        "minimum_star_core_energy_retention": float(
            cfg["minimum_star_core_energy_retention"]
        ),
        "maximum_positive_flux_removed_fraction": float(
            cfg["maximum_positive_flux_removed_fraction"]
        ),
    }
    cleanup_failures = []
    if core_retention < cleanup_checks[
        "minimum_star_core_energy_retention"
    ]:
        cleanup_failures.append(
            {
                "metric": "star_core_energy_retention",
                "value": core_retention,
                "minimum_required": cleanup_checks[
                    "minimum_star_core_energy_retention"
                ],
            }
        )
    if removed_fraction > cleanup_checks[
        "maximum_positive_flux_removed_fraction"
    ]:
        cleanup_failures.append(
            {
                "metric": "positive_flux_removed_fraction",
                "value": removed_fraction,
                "maximum_allowed": cleanup_checks[
                    "maximum_positive_flux_removed_fraction"
                ],
            }
        )
    satisfactory = bool(quality["satisfactory"] and not cleanup_failures)
    return {
        "status": "satisfactory" if satisfactory else "needs_review",
        "satisfactory": satisfactory,
        "config": cfg,
        "channel_records": channel_records,
        "quality_assessment": quality,
        "cleanup_thresholds": cleanup_checks,
        "cleanup_failed_checks": cleanup_failures,
        "raw_reconstruction_maximum_absolute_error": (
            raw_reconstruction_error
        ),
        "reconstruction_maximum_absolute_error": reconstruction_error,
        "star_core_energy_retention": float(core_retention),
        "raw_positive_flux": raw_positive_flux,
        "cleaned_positive_flux": cleaned_positive_flux,
        "positive_flux_removed_fraction": float(removed_fraction),
        "transfer_bounds_violation_count": transfer_violation,
        "corrected_starless": corrected_starless,
        "cleaned_stars": cleaned_stars,
        "diffuse_transfer": diffuse_transfer,
        "support_mask": support_mask,
    }


def _cleanup_candidate_eligible(candidate: dict[str, Any]) -> bool:
    classification = candidate.get("quality_assessment", {}).get(
        "failure_classification",
        {},
    )
    return bool(
        candidate.get("status") == "needs_retry"
        and classification.get("diffuse_nebula_leakage")
        and not classification.get("remaining_stars")
    )


def execute_compact_cleanup(
    source_path: Path,
    raw_candidate: dict[str, Any],
    cleanup_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    import gc

    if not _cleanup_candidate_eligible(raw_candidate):
        raise AdaptiveStarNetError(
            "Compact cleanup is permitted only when the final raw candidate "
            "fails for diffuse leakage and not for remaining stars."
        )
    workdir = cleanup_dir / "work"
    logs = cleanup_dir / "logs"
    previews = cleanup_dir / "previews"
    for directory in (workdir, logs, previews):
        directory.mkdir(parents=True, exist_ok=False)

    original = base.read_fits_array(source_path)
    raw_starless_path = Path(raw_candidate["starless_path"])
    raw_stars_path = Path(raw_candidate["stars_path"])
    raw_starless = base.read_fits_array(raw_starless_path)
    raw_stars = base.read_fits_array(raw_stars_path)
    try:
        cleanup = compact_cleanup_arrays(
            original,
            raw_starless,
            raw_stars,
        )
        corrected_starless = cleanup.pop("corrected_starless")
        cleaned_stars = cleanup.pop("cleaned_stars")
        diffuse_transfer = cleanup.pop("diffuse_transfer")
        support_mask = cleanup.pop("support_mask")

        starless_path = workdir / "SHO_starless_cleaned_linear.fit"
        stars_path = workdir / "SHO_stars_cleaned_linear.fit"
        transfer_path = workdir / "SHO_diffuse_transfer_linear.fit"
        support_path = workdir / "SHO_star_support_mask.fit"
        starless_evidence = _write_float32_rgb_like(
            source_path,
            starless_path,
            corrected_starless,
            "mixed_StarlessClean",
            [
                "Corrected starless = original minus cleaned compact stars.",
                f"Adaptive compact cleanup helper version {VERSION}.",
            ],
        )
        stars_evidence = _write_float32_rgb_like(
            source_path,
            stars_path,
            cleaned_stars,
            "mixed_StarsClean",
            [
                "Compact stars after broad residual transfer.",
                f"Adaptive compact cleanup helper version {VERSION}.",
            ],
        )
        transfer_evidence = _write_float32_rgb_like(
            source_path,
            transfer_path,
            diffuse_transfer,
            "mixed_NebulaTransfer",
            [
                "Positive diffuse residual transferred back to starless.",
                "Evidence only; not a stable deliverable.",
            ],
        )
        support_evidence = _write_float32_rgb_like(
            source_path,
            support_path,
            support_mask,
            "mixed_StarSupport",
            [
                "Soft per-channel compact-star support mask.",
                "Evidence only; values are constrained to zero through one.",
            ],
        )
    finally:
        del original, raw_starless, raw_stars
        gc.collect()

    technical = base.separation_metrics(
        source_path,
        starless_path,
        stars_path,
    )
    quality = quality_metrics_from_paths(
        source_path,
        starless_path,
        stars_path,
    )
    cleanup["quality_assessment"] = quality
    cleanup["satisfactory"] = bool(
        quality["satisfactory"] and not cleanup["cleanup_failed_checks"]
    )
    cleanup["status"] = (
        "satisfactory" if cleanup["satisfactory"] else "needs_review"
    )

    clipped_stars = workdir / "SHO_stars_preview_clipped.fit"
    base.create_clipped_preview_stars(stars_path, clipped_stars)
    preview_script_path = cleanup_dir / "preview.ssf"
    preview_script_path.write_text(
        base.preview_script(starless_path, clipped_stars, previews),
        encoding="utf-8",
    )
    preview_run = base.run_siril(
        workdir=cleanup_dir,
        script=preview_script_path,
        stdout_path=logs / "preview-stdout.log",
        stderr_path=logs / "preview-stderr.log",
        timeout_seconds=min(timeout_seconds, 600),
    )
    preview_run.pop("combined_log_text", None)

    result = {
        "status": cleanup["status"],
        "created_at": utc_now(),
        "helper_version": VERSION,
        "backend_version": base.VERSION,
        "operation": "compact-star-cleanup",
        "candidate": raw_candidate["candidate"] + "-compact-cleanup",
        "candidate_directory": str(cleanup_dir),
        "raw_candidate": raw_candidate["candidate"],
        "raw_candidate_directory": raw_candidate["candidate_directory"],
        "retry_number": raw_candidate.get("retry_number", 3),
        "config": {
            **dict(raw_candidate["config"]),
            "compact_cleanup": dict(COMPACT_CLEANUP_CONFIG),
        },
        "compact_star_cleanup": cleanup,
        "starless_path": str(starless_path),
        "starless_evidence": asdict(starless_evidence),
        "stars_path": str(stars_path),
        "stars_evidence": asdict(stars_evidence),
        "diffuse_transfer": str(transfer_path),
        "diffuse_transfer_evidence": asdict(transfer_evidence),
        "star_support_mask": str(support_path),
        "star_support_mask_evidence": asdict(support_evidence),
        "repository_subtract_mask": raw_candidate[
            "repository_subtract_mask"
        ],
        "repository_subtract_mask_evidence": raw_candidate[
            "repository_subtract_mask_evidence"
        ],
        "separation_metrics": technical,
        "quality_assessment": quality,
        "upstream_starnet": raw_candidate["upstream_starnet"],
        "starnet_cli": raw_candidate["starnet_cli"],
        "script": raw_candidate["script"],
        "script_sha256": raw_candidate["script_sha256"],
        "siril_run": raw_candidate["siril_run"],
        "preview_script": str(preview_script_path),
        "preview_run": preview_run,
        "previews": {
            "starless": str(
                previews / "SHO-starless-autostretch-preview.png"
            ) if (
                previews / "SHO-starless-autostretch-preview.png"
            ).is_file() else None,
            "stars": str(
                previews / "SHO-stars-autostretch-preview.png"
            ) if (
                previews / "SHO-stars-autostretch-preview.png"
            ).is_file() else None,
        },
    }
    base.json_dump_atomic(cleanup_dir / "cleanup-result.json", result)
    return result


def find_latest_cleanup_candidate(
    paths: dict[str, Path],
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    run_directories = sorted(
        (
            path for path in paths["runs"].glob("adaptive-*")
            if path.is_dir()
        ),
        reverse=True,
    )
    for run_root in run_directories:
        result_path = run_root / "adaptive-starnet-result.json"
        if not result_path.is_file():
            continue
        try:
            record = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if record.get("status") != "needs_review":
            continue
        best_name = record.get("best_candidate")
        for candidate in record.get("attempts", []):
            if candidate.get("candidate") != best_name:
                continue
            if not _cleanup_candidate_eligible(candidate):
                continue
            if not Path(candidate.get("starless_path", "")).is_file():
                continue
            if not Path(candidate.get("stars_path", "")).is_file():
                continue
            return run_root, record, candidate
    raise AdaptiveStarNetError(
        "No preserved adaptive needs-review run contains an eligible final "
        "candidate for compact cleanup."
    )


def cleanup_check_project(
    workspace: Path,
    project_name: str,
) -> dict[str, Any]:
    import gc

    paths = base.project_paths(workspace, project_name)
    prior_run, prior_result, candidate = find_latest_cleanup_candidate(paths)
    source = base.read_fits_array(paths["sho_input"])
    raw_starless = base.read_fits_array(Path(candidate["starless_path"]))
    raw_stars = base.read_fits_array(Path(candidate["stars_path"]))
    try:
        cleanup = compact_cleanup_arrays(
            source,
            raw_starless,
            raw_stars,
        )
        cleanup.pop("corrected_starless")
        cleanup.pop("cleaned_stars")
        cleanup.pop("diffuse_transfer")
        cleanup.pop("support_mask")
    finally:
        del source, raw_starless, raw_stars
        gc.collect()
    return {
        "status": cleanup["status"],
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "prior_run": str(prior_run),
        "raw_candidate": candidate["candidate"],
        "raw_stars": candidate["stars_path"],
        "raw_starless": candidate["starless_path"],
        "compact_star_cleanup": cleanup,
        "starless_background_processing_permitted": False,
        "message": (
            "Compact cleanup passes in-memory validation but has not been "
            "published. Use resume-cleanup to create and publish evidence."
            if cleanup["satisfactory"]
            else "Compact cleanup did not satisfy all gates."
        ),
    }


def resume_cleanup_project(
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    paths = base.project_paths(workspace, project_name)
    siril_version_output = base.siril_version()
    sho_manifest, sho_manifest_hash = base.load_sho_manifest(paths)
    source_evidence = base.validate_input(paths, sho_manifest)
    base.load_vendored_starnet()
    base.starnet_cli_version()
    prior_run, prior_result, raw_candidate = find_latest_cleanup_candidate(
        paths
    )
    run_root = paths["runs"] / ("adaptive-cleanup-resume-" + base.unique_id())
    run_root.mkdir(parents=True, exist_ok=False)
    cleanup = execute_compact_cleanup(
        Path(source_evidence.path),
        raw_candidate,
        run_root / "compact-cleanup",
        timeout_seconds,
    )
    attempts = list(prior_result.get("attempts", [])) + [cleanup]
    if not cleanup["quality_assessment"]["satisfactory"] or not cleanup[
        "compact_star_cleanup"
    ]["satisfactory"]:
        result = {
            "status": "needs_review",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "run_root": str(run_root),
            "prior_run": str(prior_run),
            "message": (
                "Compact cleanup failed one or more gates. Existing stable "
                "outputs were not replaced."
            ),
            "compact_cleanup": cleanup,
            "starless_background_processing_permitted": False,
        }
        base.json_dump_atomic(
            run_root / "adaptive-starnet-result.json",
            result,
        )
        return result
    final = publish_selected_candidate(
        paths,
        run_root,
        cleanup,
        attempts,
        source_evidence,
        sho_manifest_hash,
        siril_version_output,
        MAX_RETRIES_LIMIT,
    )
    final["resumed_from"] = str(prior_run)
    final["message"] = (
        "Published compact-cleanup result from the preserved third retry; "
        "no additional StarNet execution occurred."
    )
    base.json_dump_atomic(
        run_root / "adaptive-starnet-result.json",
        final,
    )
    return final


def validate_existing_stable(paths: dict[str, Path]) -> dict[str, Any]:
    backend_status = base.status_project(
        paths["workspace"],
        paths["project"].name,
    )
    if backend_status.get("status") != "ready":
        raise AdaptiveStarNetError(
            "Existing stable StarNet outputs failed technical validation: "
            f"{backend_status.get('errors')}"
        )
    quality = quality_metrics_from_paths(
        paths["sho_input"],
        paths["stable_starless"],
        paths["stable_stars"],
    )
    try:
        manifest = json.loads(
            paths["stable_manifest"].read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise AdaptiveStarNetError(
            f"Cannot read existing StarNet manifest: {exc}"
        ) from exc
    return {
        "status": quality["status"],
        "source": "existing-stable-output",
        "config": manifest.get("options", BASELINE_CONFIG),
        "starless_path": str(paths["stable_starless"]),
        "stars_path": str(paths["stable_stars"]),
        "starless_evidence": backend_status["starless"],
        "stars_evidence": backend_status["stars"],
        "separation_metrics": backend_status["separation_metrics"],
        "quality_assessment": quality,
        "manifest": str(paths["stable_manifest"]),
        "manifest_sha256": base.sha256_file(paths["stable_manifest"]),
    }


def choose_retry_config(
    retry_number: int,
    prior_quality: dict[str, Any],
) -> dict[str, Any]:
    if retry_number < 1 or retry_number > MAX_RETRIES_LIMIT:
        raise AdaptiveStarNetError(
            f"Retry number must be 1..{MAX_RETRIES_LIMIT}."
        )
    config = dict(LEAKAGE_RETRY_CONFIGS[retry_number - 1])
    classification = prior_quality.get("failure_classification", {})
    if (
        classification.get("remaining_stars")
        and not classification.get("diffuse_nebula_leakage")
    ):
        # A too-dark temporary stretch can miss stars. In that specific case,
        # retain upsampling but move back toward the standard target.
        config["target_background"] = min(
            0.25,
            0.18 + 0.035 * (retry_number - 1),
        )
        config["label"] = f"retry-{retry_number}-brighter-upsampled"
        config["decision_basis"] = "remaining stars dominated prior failure"
    else:
        config["decision_basis"] = (
            "diffuse nebular leakage dominated prior failure; use a dimmer "
            "temporary stretch and 2x upsampling"
        )
    return config


def publish_selected_candidate(
    paths: dict[str, Path],
    run_root: Path,
    selected: dict[str, Any],
    attempts: list[dict[str, Any]],
    source_evidence,
    sho_manifest_hash: str,
    siril_version_output: str,
    max_retries: int,
) -> dict[str, Any]:
    publish_dir = run_root / "publish-staging"
    publish_dir.mkdir(parents=True, exist_ok=False)
    staged_starless_path = publish_dir / base.STABLE_STARLESS_NAME
    staged_stars_path = publish_dir / base.STABLE_STARS_NAME
    staged_manifest_path = publish_dir / base.STABLE_MANIFEST_NAME

    base.copy_verified(
        Path(selected["starless_path"]),
        staged_starless_path,
        selected["starless_evidence"]["sha256"],
    )
    base.copy_verified(
        Path(selected["stars_path"]),
        staged_stars_path,
        selected["stars_evidence"]["sha256"],
    )
    staged_starless = base.inspect_fits(
        staged_starless_path,
        expected_channels=3,
        require_float32=True,
    )
    staged_stars = base.inspect_fits(
        staged_stars_path,
        expected_channels=3,
        require_float32=True,
    )
    technical = base.separation_metrics(
        paths["sho_input"],
        staged_starless_path,
        staged_stars_path,
    )
    quality = quality_metrics_from_paths(
        paths["sho_input"],
        staged_starless_path,
        staged_stars_path,
    )
    if not quality["satisfactory"]:
        raise AdaptiveStarNetError(
            "Selected candidate failed quality revalidation during publication."
        )

    final_starless = asdict(staged_starless)
    final_starless["path"] = str(paths["stable_starless"])
    final_stars = asdict(staged_stars)
    final_stars["path"] = str(paths["stable_stars"])
    retry_numbers = [
        int(record.get("retry_number", 0))
        for record in attempts
        if record.get("operation") in {"starnet", "compact-star-cleanup"}
    ]
    retries_used = max(retry_numbers, default=0)

    anticipated_previous_stable = (
        run_root / "superseded-stable-before-adaptive-review"
        if paths["starnet"].exists()
        else None
    )

    manifest = {
        "schema_version": 3,
        "status": "ready",
        "helper_version": VERSION,
        "backend_version": base.VERSION,
        "created_at": utc_now(),
        "project": paths["project"].name,
        "project_path": str(paths["project"]),
        "siril_version_output": siril_version_output,
        "sho_manifest": str(paths["sho_manifest"]),
        "sho_manifest_sha256": sho_manifest_hash,
        "source": asdict(source_evidence),
        "starless": final_starless,
        "stars": final_stars,
        "selected_candidate": selected["candidate"],
        "selected_config": selected["config"],
        "quality_assessment": quality,
        "separation_metrics": technical,
        "attempts": [
            {
                "candidate": record.get("candidate"),
                "source": record.get("source", "new-candidate"),
                "operation": record.get("operation"),
                "retry_number": record.get("retry_number"),
                "config": record.get("config"),
                "status": record.get("status"),
                "quality_assessment": record.get("quality_assessment"),
                "candidate_directory": record.get("candidate_directory"),
                "previews": record.get("previews"),
            }
            for record in attempts
        ],
        "maximum_retries": max_retries,
        "retries_used": retries_used,
        "repository_subtract_mask": selected[
            "repository_subtract_mask"
        ],
        "repository_subtract_mask_evidence": selected[
            "repository_subtract_mask_evidence"
        ],
        "upstream_starnet": selected["upstream_starnet"],
        "starnet_cli": selected["starnet_cli"],
        "script": selected["script"],
        "script_sha256": selected["script_sha256"],
        "siril_run": selected["siril_run"],
        "previews": selected["previews"],
        "compact_star_cleanup": selected.get("compact_star_cleanup"),
        "raw_candidate": selected.get("raw_candidate"),
        "diffuse_transfer": selected.get("diffuse_transfer"),
        "diffuse_transfer_evidence": selected.get(
            "diffuse_transfer_evidence"
        ),
        "star_support_mask": selected.get("star_support_mask"),
        "star_support_mask_evidence": selected.get(
            "star_support_mask_evidence"
        ),
        "publication_method": (
            "atomic replacement with previous stable directory preserved"
            if paths["starnet"].exists()
            else "atomic directory rename"
        ),
        "previous_stable_preserved_at": (
            str(anticipated_previous_stable)
            if anticipated_previous_stable is not None
            else None
        ),
        "starless_background_processing_permitted": True,
    }
    base.json_dump_atomic(staged_manifest_path, manifest)

    previous_stable = anticipated_previous_stable
    if paths["starnet"].exists():
        if previous_stable.exists():
            raise AdaptiveStarNetError(
                f"Previous-stable archive already exists: {previous_stable}"
            )
        os.replace(paths["starnet"], previous_stable)
    try:
        os.replace(publish_dir, paths["starnet"])
    except Exception:
        if previous_stable is not None and not paths["starnet"].exists():
            os.replace(previous_stable, paths["starnet"])
        raise

    return {
        "status": "ready",
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "run_root": str(run_root),
        "selected_candidate": selected["candidate"],
        "selected_config": selected["config"],
        "maximum_retries": max_retries,
        "retries_used": retries_used,
        "previous_stable_preserved_at": (
            str(previous_stable) if previous_stable is not None else None
        ),
        "starless": asdict(
            base.inspect_fits(
                paths["stable_starless"],
                expected_channels=3,
                require_float32=True,
            )
        ),
        "stars": asdict(
            base.inspect_fits(
                paths["stable_stars"],
                expected_channels=3,
                require_float32=True,
            )
        ),
        "quality_assessment": quality,
        "separation_metrics": technical,
        "stable_manifest": str(paths["stable_manifest"]),
        "previews": selected["previews"],
        "compact_star_cleanup": selected.get("compact_star_cleanup"),
        "raw_candidate": selected.get("raw_candidate"),
        "diffuse_transfer": selected.get("diffuse_transfer"),
        "star_support_mask": selected.get("star_support_mask"),
        "starless_background_processing_permitted": True,
    }


def adaptive_run_project(
    workspace: Path,
    project_name: str,
    timeout_seconds: int,
    max_retries: int,
) -> dict[str, Any]:
    if max_retries < 0 or max_retries > MAX_RETRIES_LIMIT:
        raise AdaptiveStarNetError(
            f"max_retries must be between 0 and {MAX_RETRIES_LIMIT}."
        )
    paths = base.project_paths(workspace, project_name)
    if not paths["project"].is_dir():
        raise AdaptiveStarNetError(
            f"Project does not exist: {paths['project']}"
        )
    siril_version_output = base.siril_version()
    sho_manifest, sho_manifest_hash = base.load_sho_manifest(paths)
    source_evidence = base.validate_input(paths, sho_manifest)
    base.load_vendored_starnet()
    base.starnet_cli_version()

    run_root = paths["runs"] / ("adaptive-" + base.unique_id())
    run_root.mkdir(parents=True, exist_ok=False)
    attempts: list[dict[str, Any]] = []
    selected = None

    if paths["starnet"].exists():
        existing = validate_existing_stable(paths)
        attempts.append(existing)
        base.json_dump_atomic(
            run_root / "existing-quality-assessment.json",
            existing,
        )
        if existing["quality_assessment"]["satisfactory"]:
            return {
                "status": "ready",
                "helper_version": VERSION,
                "project": str(paths["project"]),
                "run_root": str(run_root),
                "message": "Existing stable outputs passed adaptive quality review.",
                "quality_assessment": existing["quality_assessment"],
                "maximum_retries": max_retries,
                "retries_used": 0,
                "starless_background_processing_permitted": True,
            }
        prior_quality = existing["quality_assessment"]
    else:
        baseline_dir = run_root / "candidate-00-baseline"
        baseline = execute_candidate(
            Path(source_evidence.path),
            source_evidence,
            baseline_dir,
            dict(BASELINE_CONFIG),
            timeout_seconds,
        )
        baseline["operation"] = "starnet"
        baseline["retry_number"] = 0
        attempts.append(baseline)
        if baseline["quality_assessment"]["satisfactory"]:
            selected = baseline
        prior_quality = baseline["quality_assessment"]

    retry_number = 1
    while selected is None and retry_number <= max_retries:
        config = choose_retry_config(retry_number, prior_quality)
        candidate_dir = run_root / f"candidate-{retry_number:02d}"
        candidate = execute_candidate(
            Path(source_evidence.path),
            source_evidence,
            candidate_dir,
            config,
            timeout_seconds,
        )
        candidate["operation"] = "starnet"
        candidate["retry_number"] = retry_number
        attempts.append(candidate)
        if candidate["quality_assessment"]["satisfactory"]:
            selected = candidate
            break
        if (
            retry_number == MAX_RETRIES_LIMIT
            and retry_number == max_retries
            and _cleanup_candidate_eligible(candidate)
        ):
            cleanup = execute_compact_cleanup(
                Path(source_evidence.path),
                candidate,
                run_root / "candidate-03-compact-cleanup",
                timeout_seconds,
            )
            attempts.append(cleanup)
            if (
                cleanup["quality_assessment"]["satisfactory"]
                and cleanup["compact_star_cleanup"]["satisfactory"]
            ):
                selected = cleanup
                break
        prior_quality = candidate["quality_assessment"]
        retry_number += 1

    if selected is None:
        best = min(
            attempts,
            key=lambda record: record["quality_assessment"]["quality_score"],
        )
        result = {
            "status": "needs_review",
            "helper_version": VERSION,
            "project": str(paths["project"]),
            "run_root": str(run_root),
            "maximum_retries": max_retries,
            "attempts_completed": len(attempts),
            "message": (
                "No raw candidate or final compact-star cleanup satisfied "
                "the deterministic image-quality gate. No stable output was "
                "replaced or newly published."
            ),
            "best_candidate": best.get("candidate", best.get("source")),
            "best_quality_assessment": best["quality_assessment"],
            "attempts": attempts,
            "starless_background_processing_permitted": False,
        }
        base.json_dump_atomic(
            run_root / "adaptive-starnet-result.json",
            result,
        )
        return result

    if base.sha256_file(paths["sho_manifest"]) != sho_manifest_hash:
        raise AdaptiveStarNetError(
            "SHO-combination manifest changed during adaptive StarNet execution."
        )
    if base.sha256_file(paths["sho_input"]) != source_evidence.sha256:
        raise AdaptiveStarNetError(
            "SHO-linear source changed during adaptive StarNet execution."
        )

    final = publish_selected_candidate(
        paths,
        run_root,
        selected,
        attempts,
        source_evidence,
        sho_manifest_hash,
        siril_version_output,
        max_retries,
    )
    base.json_dump_atomic(
        run_root / "adaptive-starnet-result.json",
        final,
    )
    return final


def quality_check_project(workspace: Path, project_name: str) -> dict[str, Any]:
    paths = base.project_paths(workspace, project_name)
    if not paths["starnet"].is_dir():
        return {
            "status": "not_run",
            "project": str(paths["project"]),
            "message": "No stable StarNet output directory exists.",
            "starless_background_processing_permitted": False,
        }
    existing = validate_existing_stable(paths)
    return {
        "status": (
            "satisfactory"
            if existing["quality_assessment"]["satisfactory"]
            else "needs_retry"
        ),
        "helper_version": VERSION,
        "project": str(paths["project"]),
        "starless": existing["starless_path"],
        "stars": existing["stars_path"],
        "quality_assessment": existing["quality_assessment"],
        "starless_background_processing_permitted": existing[
            "quality_assessment"
        ]["satisfactory"],
    }


def status_project(workspace: Path, project_name: str) -> dict[str, Any]:
    technical = base.status_project(workspace, project_name)
    if technical.get("status") != "ready":
        return {
            **technical,
            "adaptive_helper_version": VERSION,
            "starless_background_processing_permitted": False,
        }
    quality = quality_check_project(workspace, project_name)
    return {
        **technical,
        "adaptive_helper_version": VERSION,
        "status": (
            "ready" if quality["status"] == "satisfactory" else "needs_review"
        ),
        "quality_assessment": quality.get("quality_assessment"),
        "starless_background_processing_permitted": (
            quality["status"] == "satisfactory"
        ),
    }


def quality_regression_self_test() -> dict[str, Any]:
    import numpy as np

    height = 384
    width = 384
    y, x = np.mgrid[0:height, 0:width]
    nebula = (
        0.004
        + 0.015
        * np.exp(
            -(
                ((x - 205.0) / 85.0) ** 2
                + ((y - 190.0) / 65.0) ** 2
            )
        )
    ).astype(np.float32)
    starless = np.stack(
        [nebula * 1.15, nebula, nebula * 0.72],
        axis=0,
    ).astype(np.float32)
    clean_stars = np.zeros_like(starless)
    for cy, cx, amplitude, sigma in (
        (50, 70, 0.8, 2.2),
        (95, 300, 0.6, 2.8),
        (180, 120, 0.9, 3.1),
        (245, 270, 0.7, 2.4),
        (330, 190, 0.85, 3.0),
    ):
        profile = amplitude * np.exp(
            -((x - cx) ** 2 + (y - cy) ** 2) / (2.0 * sigma * sigma)
        )
        clean_stars += np.stack(
            [profile, profile * 0.9, profile * 0.8],
            axis=0,
        ).astype(np.float32)
    original = starless + clean_stars
    clean = quality_metrics_from_arrays(original, starless, clean_stars)

    leakage = np.stack(
        [nebula * 0.012, nebula * 0.045, nebula * 0.012],
        axis=0,
    ).astype(np.float32)
    bad_starless = starless - leakage
    bad_stars = clean_stars + leakage
    bad = quality_metrics_from_arrays(original, bad_starless, bad_stars)
    if not clean["satisfactory"]:
        raise AdaptiveStarNetError(
            f"Clean quality regression unexpectedly failed: {clean}"
        )
    if bad["satisfactory"] or not bad["failure_classification"][
        "diffuse_nebula_leakage"
    ]:
        raise AdaptiveStarNetError(
            f"Nebula-leakage regression was not rejected: {bad}"
        )
    return {
        "clean_candidate": clean,
        "leaked_candidate": bad,
        "status": "success",
    }


def compact_cleanup_regression_self_test() -> dict[str, Any]:
    import numpy as np

    height = 384
    width = 384
    y, x = np.mgrid[0:height, 0:width]
    nebula = (
        0.004
        + 0.015
        * np.exp(
            -(
                ((x - 205.0) / 85.0) ** 2
                + ((y - 190.0) / 65.0) ** 2
            )
        )
    ).astype(np.float32)
    true_starless = np.stack(
        [nebula * 1.15, nebula, nebula * 0.72],
        axis=0,
    ).astype(np.float32)
    clean_stars = np.zeros_like(true_starless)
    for cy, cx, amplitude, sigma in (
        (50, 70, 0.8, 2.2),
        (95, 300, 0.6, 2.8),
        (180, 120, 0.9, 3.1),
        (245, 270, 0.7, 2.4),
        (330, 190, 0.85, 3.0),
    ):
        profile = amplitude * np.exp(
            -((x - cx) ** 2 + (y - cy) ** 2)
            / (2.0 * sigma * sigma)
        )
        clean_stars += np.stack(
            [profile, profile * 0.9, profile * 0.8],
            axis=0,
        ).astype(np.float32)
    original = true_starless + clean_stars
    leakage = np.stack(
        [nebula * 0.012, nebula * 0.045, nebula * 0.012],
        axis=0,
    ).astype(np.float32)
    raw_starless = true_starless - leakage
    raw_stars = clean_stars + leakage
    before = quality_metrics_from_arrays(
        original,
        raw_starless,
        raw_stars,
    )
    cleanup = compact_cleanup_arrays(
        original,
        raw_starless,
        raw_stars,
    )
    cleanup.pop("corrected_starless")
    cleanup.pop("cleaned_stars")
    cleanup.pop("diffuse_transfer")
    cleanup.pop("support_mask")
    if before["satisfactory"]:
        raise AdaptiveStarNetError(
            "Compact cleanup regression input was unexpectedly satisfactory."
        )
    if not cleanup["satisfactory"]:
        raise AdaptiveStarNetError(
            f"Compact cleanup regression failed: {cleanup}"
        )
    return {
        "status": "success",
        "before": before,
        "after": cleanup,
    }


def self_test(workspace: Path, timeout_seconds: int) -> dict[str, Any]:
    regression = quality_regression_self_test()
    cleanup_regression = compact_cleanup_regression_self_test()
    backend_test = base.self_test(workspace, timeout_seconds)
    result = {
        "status": "success",
        "helper_version": VERSION,
        "backend_version": base.VERSION,
        "created_at": utc_now(),
        "quality_regression": regression,
        "compact_cleanup_regression": cleanup_regression,
        "backend_starnet_self_test": backend_test,
        "maximum_retries": MAX_RETRIES_LIMIT,
        "tests": [
            "clean stars-layer quality acceptance",
            "diffuse nebula leakage rejection",
            "remaining-star metric",
            "controlled MTF functions",
            "real Siril/StarNet backend execution",
            "maximum retry limit of three",
            "final-retry compact-star cleanup",
            "star-core energy retention",
            "exact reconstruction after diffuse transfer",
        ],
    }
    test_root = (
        workspace
        / ".skill-self-tests"
        / "siril-starnet-removal-adaptive"
        / base.unique_id()
    )
    test_root.mkdir(parents=True, exist_ok=False)
    base.json_dump_atomic(test_root / "self-test-result.json", result)
    result["self_test_directory"] = str(test_root)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Adaptive StarNet removal with deterministic residual-quality "
            "analysis and at most three retries."
        )
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    self_parser = subparsers.add_parser("self-test")
    self_parser.add_argument("--timeout", type=int, default=1800)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--project", required=True)
    run_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )
    run_parser.add_argument(
        "--max-retries",
        type=int,
        default=MAX_RETRIES_LIMIT,
    )

    quality_parser = subparsers.add_parser("quality-check")
    quality_parser.add_argument("--project", required=True)

    cleanup_check_parser = subparsers.add_parser("cleanup-check")
    cleanup_check_parser.add_argument("--project", required=True)

    resume_parser = subparsers.add_parser("resume-cleanup")
    resume_parser.add_argument("--project", required=True)
    resume_parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
    )

    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("--project", required=True)

    args = parser.parse_args()
    try:
        workspace = base.derive_workspace()
        if args.command == "self-test":
            if args.timeout < 120 or args.timeout > 7200:
                raise AdaptiveStarNetError(
                    "Self-test timeout must be between 120 and 7200 seconds."
                )
            result = self_test(workspace, args.timeout)
        elif args.command == "run":
            if args.timeout < 300 or args.timeout > 14400:
                raise AdaptiveStarNetError(
                    "Run timeout must be between 300 and 14400 seconds."
                )
            result = adaptive_run_project(
                workspace,
                args.project,
                args.timeout,
                args.max_retries,
            )
        elif args.command == "quality-check":
            result = quality_check_project(workspace, args.project)
        elif args.command == "cleanup-check":
            result = cleanup_check_project(workspace, args.project)
        elif args.command == "resume-cleanup":
            if args.timeout < 300 or args.timeout > 14400:
                raise AdaptiveStarNetError(
                    "Resume timeout must be between 300 and 14400 seconds."
                )
            result = resume_cleanup_project(
                workspace,
                args.project,
                args.timeout,
            )
        else:
            result = status_project(workspace, args.project)

        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("status") in {
            "success",
            "ready",
            "satisfactory",
            "not_run",
        } else 2
    except (AdaptiveStarNetError, base.StarNetRemovalError) as exc:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "helper_version": VERSION,
                    "backend_version": getattr(base, "VERSION", None),
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
